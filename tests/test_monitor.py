import tempfile
import unittest
from datetime import datetime

from yonstudy.daily import SEOUL
from yonstudy.monitor import attendance_snapshot, viewing_queue
from yonstudy.store import Store


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.save_course({
            "course_id": 1, "year": "2026", "semester": "2학기",
            "name": "테스트과목", "title": "테스트과목", "slug": "TST_테스트",
        })

    def tearDown(self):
        self.tmp.cleanup()

    def add_vod(
        self, cmid, title, section, progress, watched, duration,
        completion="n", is_progress=1,
    ):
        self.store.save_activity({
            "cmid": cmid, "course_id": 1, "modname": "vod", "title": title,
            "section_idx": section, "section_name": f"{section}주차",
            "url": f"https://example/{cmid}", "completion": completion,
            "open_from": "2026-09-01 00:00:00", "open_to": "2026-09-07 23:59:59",
            "restricted": 0,
        })
        self.store.save_vod({
            "cmid": cmid, "course_id": 1, "progress_pct": progress,
            "watched_sec": watched, "duration_sec": duration,
            "is_progress": is_progress,
            "can_log_progress": 1 if is_progress else 0,
            "status": "ok",
        })

    def test_failed_metadata_is_visible_but_not_actionable(self):
        self.add_vod(21, "확인 실패 강의", 1, 0, 0, 600)
        self.store.save_vod({
            "cmid": 21, "course_id": 1, "is_progress": 1,
            "can_log_progress": 0, "status": "error",
        })
        self.store.commit()
        now = datetime(2026, 9, 2, 2, 0, tzinfo=SEOUL)

        self.assertEqual(
            viewing_queue(self.store, year="2026", semester="2학기", now=now),
            [],
        )
        snapshot = attendance_snapshot(
            self.store, year="2026", semester="2학기"
        )
        self.assertEqual(snapshot[0]["status"], "error")
        self.assertFalse(snapshot[0]["verified"])

    def test_queue_is_sequential_and_requires_both_attendance_signals(self):
        self.add_vod(12, "2주차 1차시", 2, 0, 0, 600)
        self.add_vod(11, "1주차 2차시", 1, 50, 300, 600)
        self.add_vod(10, "1주차 1차시", 1, 100, 600, 600, "y")
        self.store.commit()
        now = datetime(2026, 9, 2, 2, 0, tzinfo=SEOUL)
        queue = viewing_queue(self.store, year="2026", semester="2학기", now=now)
        self.assertEqual([row["cmid"] for row in queue], [11, 12])
        snapshot = attendance_snapshot(self.store, year="2026", semester="2학기")
        done = next(row for row in snapshot if row["cmid"] == 10)
        self.assertTrue(done["progress_ok"])
        self.assertTrue(done["position_ok"])
        self.assertTrue(done["verified"])

    def test_untracked_vod_is_reported_but_not_put_in_attendance_queue(self):
        self.add_vod(
            20, "녹화본", 1, None, None, None,
            completion=None, is_progress=0,
        )
        self.store.commit()
        now = datetime(2026, 9, 2, 2, 0, tzinfo=SEOUL)
        self.assertEqual(
            viewing_queue(self.store, year="2026", semester="2학기", now=now),
            [],
        )
        snapshot = attendance_snapshot(
            self.store, year="2026", semester="2학기"
        )
        self.assertEqual([row["cmid"] for row in snapshot], [20])
        self.assertFalse(snapshot[0]["played_once"])
        self.assertFalse(snapshot[0]["verified"])

        self.store.log("playback_once", "20", True, "ended")
        self.store.commit()
        snapshot = attendance_snapshot(
            self.store, year="2026", semester="2학기"
        )
        self.assertTrue(snapshot[0]["played_once"])
        self.assertTrue(snapshot[0]["verified"])

    def test_unenrolled_course_is_removed_from_attendance_and_queue(self):
        self.add_vod(30, "철회 전 강의", 1, 0, 0, 600)
        self.store.save_course(
            {"course_id": 1, "enrolled": 0, "unenrolled_at": "2026-09-02T01:00:00"}
        )
        self.store.commit()
        now = datetime(2026, 9, 2, 2, 0, tzinfo=SEOUL)

        self.assertEqual(
            attendance_snapshot(self.store, year="2026", semester="2학기"), []
        )
        self.assertEqual(
            viewing_queue(
                self.store, year="2026", semester="2학기", now=now
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
