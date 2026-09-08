import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from yonstudy.recordings import (
    classify_recordings,
    import_timetable,
    match_recording,
    reclassify_unmatched,
    timestamp_from_filename,
)
from yonstudy.store import Store


SEOUL = ZoneInfo("Asia/Seoul")


class RecordingClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "store")
        for course_id, name, code in (
            (1, "데이터베이스", "CSI2102"),
            (2, "인공지능", "CSI3101"),
        ):
            self.store.save_course({
                "course_id": course_id,
                "year": "2026",
                "semester": "2학기",
                "name": name,
                "title": f"{name} ({code})",
                "code": code,
                "slug": f"{code}_{name}",
            })
        self.store.commit()

    def tearDown(self):
        self.temporary.cleanup()

    def _write_timetable(self, classes):
        path = self.root / "timetable.json"
        path.write_text(json.dumps({
            "year": "2026",
            "semester": "2학기",
            "valid_from": "2026-09-01",
            "valid_to": "2026-12-20",
            "classes": classes,
        }, ensure_ascii=False), encoding="utf-8")
        return path

    def test_import_expands_multiple_weekdays_and_resolves_course_name(self):
        path = self._write_timetable([{
            "course": "데이터베이스",
            "days": ["월", "수"],
            "start": "09:00",
            "end": "10:50",
            "location": "공학관 101",
        }])

        result = import_timetable(self.store, path)
        rows = self.store.query(
            "SELECT * FROM timetable_slot ORDER BY weekday"
        )

        self.assertEqual(result.slots, 2)
        self.assertEqual([row["weekday"] for row in rows], [0, 2])
        self.assertTrue(all(row["course_id"] == 1 for row in rows))
        self.assertEqual(rows[0]["valid_from"], "2026-09-01")

    def test_overlapping_slots_are_disambiguated_by_filename(self):
        path = self._write_timetable([
            {"course_id": 1, "weekday": "월", "start": "09:00", "end": "10:50"},
            {"course_id": 2, "weekday": "월", "start": "09:00", "end": "10:50"},
        ])
        import_timetable(self.store, path)
        captured = datetime(2026, 9, 7, 9, 20, tzinfo=SEOUL)  # Monday

        ambiguous = match_recording(self.store, captured, "새로운 녹음.m4a")
        matched = match_recording(self.store, captured, "인공지능 수업.m4a")

        self.assertEqual(ambiguous.status, "ambiguous")
        self.assertEqual(matched.status, "matched")
        self.assertEqual(matched.course_id, 2)
        self.assertEqual(matched.method, "timetable+filename")

    def test_timestamp_filename_precedes_file_mtime_and_file_is_classified(self):
        path = self._write_timetable([{
            "course": "데이터베이스", "weekday": "월",
            "start": "09:00", "end": "10:50",
        }])
        import_timetable(self.store, path)
        inbox = self.root / "inbox"
        inbox.mkdir()
        recording = inbox / "녹음_20260907_091500.m4a"
        recording.write_bytes(b"recording bytes")
        # mtime은 화요일이지만 파일명의 월요일 녹음 시각이 우선한다.
        wrong_mtime = datetime(2026, 9, 8, 15, 0, tzinfo=SEOUL).timestamp()
        os.utime(recording, (wrong_mtime, wrong_mtime))

        with patch(
            "yonstudy.recordings.probe_media",
            return_value=(None, 3600.0, "데이터베이스 1주차"),
        ):
            result = classify_recordings(self.store, [inbox])

        self.assertEqual(result.matched, 1)
        self.assertEqual(result.imported, 1)
        row = self.store.query("SELECT * FROM recording")[0]
        self.assertEqual(row["course_id"], 1)
        self.assertEqual(row["timestamp_source"], "filename")
        self.assertEqual(row["metadata_title"], "데이터베이스 1주차")
        self.assertEqual((row["week"], row["lesson"]), (1, 1))
        target = self.store.root / row["path"]
        self.assertEqual(target.read_bytes(), b"recording bytes")
        self.assertTrue(target.name.startswith("W01-L01__강의녹음__20260907_0915__r"))
        self.assertTrue(recording.exists(), "기본 동작은 입력 원본을 보존해야 한다")

    def test_dry_run_does_not_import_or_copy(self):
        path = self._write_timetable([{
            "course_id": 1, "weekday": "월", "start": "09:00", "end": "10:50",
        }])
        import_timetable(self.store, path)
        recording = self.root / "20260907_093000.wav"
        recording.write_bytes(b"wave")

        with patch("yonstudy.recordings.probe_media", return_value=(None, 60.0, None)):
            result = classify_recordings(self.store, [recording], dry_run=True)

        self.assertEqual(result.matched, 1)
        self.assertEqual(result.imported, 0)
        self.assertEqual(self.store.query("SELECT * FROM recording"), [])
        self.assertEqual(list((self.store.root / "courses").rglob("*.wav")), [])

    def test_filename_timestamp_accepts_korean_am_pm(self):
        value = timestamp_from_filename("2026년 9월 7일 오후 2시 05분.m4a", SEOUL)

        self.assertIsNotNone(value)
        self.assertEqual((value.hour, value.minute), (14, 5))

    def test_embedded_creation_time_and_title_take_priority(self):
        path = self._write_timetable([{
            "course_id": 1, "weekday": "월", "start": "09:00", "end": "10:50",
        }])
        import_timetable(self.store, path)
        # 파일명은 화요일이지만 메타데이터 UTC 시각은 월요일 09:15 KST다.
        recording = self.root / "20260908_150000.m4a"
        recording.write_bytes(b"metadata recording")

        with patch(
            "yonstudy.recordings.probe_media",
            return_value=("2026-09-07T00:15:00Z", 1800.0, "데이터베이스 강의"),
        ):
            result = classify_recordings(self.store, [recording], dry_run=True)

        item = result.items[0]
        self.assertEqual(item["timestamp_source"], "metadata")
        self.assertEqual(item["captured_at"], "2026-09-07T09:15:00+09:00")
        self.assertEqual(item["course_id"], 1)
        self.assertEqual(item["metadata_title"], "데이터베이스 강의")

    def test_toml_timetable_and_reimport_preserve_slot_id(self):
        path = self.root / "timetable.toml"
        path.write_text(
            """
year = "2026"
semester = "2학기"
valid_from = "2026-09-01"
valid_to = "2026-12-20"

[[class]]
course_id = 1
days = ["mon", "wed"]
start = "09:00"
end = "10:50"
""".strip(),
            encoding="utf-8",
        )
        import_timetable(self.store, path)
        before = {
            row["weekday"]: row["id"]
            for row in self.store.query("SELECT id,weekday FROM timetable_slot")
        }

        import_timetable(self.store, path)
        after = {
            row["weekday"]: row["id"]
            for row in self.store.query("SELECT id,weekday FROM timetable_slot")
        }

        self.assertEqual(before, after)

    def test_hotfolder_waits_for_two_stable_scans_and_then_skips_processed_file(self):
        path = self._write_timetable([{
            "course_id": 1, "weekday": "월", "start": "09:00", "end": "10:50",
        }])
        import_timetable(self.store, path)
        inbox = self.root / "inbox"
        inbox.mkdir()
        recording = inbox / "20260907_093000.m4a"
        recording.write_bytes(b"stable recording")
        state = self.store.root / "recording_scan_state.json"

        with patch("yonstudy.recordings.time_module.time", return_value=1000.0):
            first = classify_recordings(
                self.store, [inbox], stable_seconds=120, scan_state_path=state
            )
        with (
            patch("yonstudy.recordings.time_module.time", return_value=1121.0),
            patch("yonstudy.recordings.probe_media", return_value=(None, 60.0, None)),
        ):
            second = classify_recordings(
                self.store, [inbox], stable_seconds=120, scan_state_path=state
            )
        with patch("yonstudy.recordings.time_module.time", return_value=1300.0):
            third = classify_recordings(
                self.store, [inbox], stable_seconds=120, scan_state_path=state
            )

        self.assertEqual((first.pending, first.imported), (1, 0))
        self.assertEqual((second.pending, second.imported), (0, 1))
        self.assertEqual((third.skipped, third.imported), (1, 0))

    def test_external_archive_uses_flat_canonical_name_and_consume(self):
        path = self._write_timetable([{
            "course_id": 1, "weekday": "화", "start": "10:00", "end": "11:50",
        }])
        import_timetable(self.store, path)
        recording = self.root / "20260908_101500.m4a"
        recording.write_bytes(b"consume me")
        archive = self.root / "archive"

        with patch(
            "yonstudy.recordings.probe_media", return_value=(None, 60.0, None)
        ):
            result = classify_recordings(
                self.store, [recording], destination=archive, move=True
            )

        self.assertEqual((result.matched, result.moved), (1, 1))
        self.assertFalse(recording.exists())
        row = self.store.query("SELECT path FROM recording")[0]
        target = Path(row["path"])
        self.assertTrue(target.is_absolute())
        self.assertEqual(target.parts[-3:-1], ("2026-2", "CSI2102_데이터베이스"))
        self.assertTrue(target.name.startswith("W02-L01__강의녹음__20260908_1015__r"))
        self.assertEqual(target.read_bytes(), b"consume me")

    def test_stored_unmatched_blob_is_reclassified_after_timetable_is_added(self):
        recording = self.root / "20260907_091500.m4a"
        recording.write_bytes(b"late timetable")
        archive = self.root / "archive"
        with patch(
            "yonstudy.recordings.probe_media", return_value=(None, 60.0, None)
        ):
            initial = classify_recordings(
                self.store, [recording], destination=archive, move=True
            )
        old_path = Path(self.store.query("SELECT path FROM recording")[0]["path"])
        self.assertEqual(initial.unclassified, 1)
        self.assertTrue(old_path.is_file())

        timetable = self._write_timetable([{
            "course_id": 1, "weekday": "월", "start": "09:00", "end": "10:50",
        }])
        import_timetable(self.store, timetable)
        changed = reclassify_unmatched(self.store, destination=archive)

        row = self.store.query("SELECT * FROM recording")[0]
        self.assertEqual(changed, 1)
        self.assertEqual(row["match_status"], "matched")
        self.assertFalse(old_path.exists())
        self.assertTrue(Path(row["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
