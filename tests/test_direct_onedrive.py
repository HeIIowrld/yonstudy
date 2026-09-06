import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from yonstudy.archive import Archiver
from yonstudy.flat_layout import resource_filename
from yonstudy.onedrive import RcloneOneDrive
from yonstudy.store import Store


class FakeSink:
    def __init__(self):
        self.files = {}

    def file_path(self, **values):
        filename = resource_filename(
            section_idx=values.get("section_idx"),
            section_name=values.get("section_name"),
            activity_title=values.get("activity_title"),
            name=values["name"], file_id=values["file_id"],
            open_from=values.get("open_from"), saved_at=values.get("saved_at"),
        )
        return f"2026-2/{values['course_slug']}/{filename}"

    def exists(self, relative, size=None):
        return relative in self.files and (size is None or len(self.files[relative]) == size)

    def upload_bytes(self, relative, body, force=False):
        if not force and self.exists(relative, len(body)):
            return False
        self.files[relative] = body
        return True


class FailingSink(FakeSink):
    def upload_bytes(self, relative, body, force=False):
        raise RuntimeError("remote unavailable")


class DirectOneDriveArchiveTests(unittest.TestCase):
    def test_real_sink_puts_week_prefixed_material_in_course_root(self):
        sink = object.__new__(RcloneOneDrive)
        path = sink.file_path(
            year="2026", semester="2학기", course_slug="TST1000_테스트",
            activity_title="Lecture 1-2", file_id=1407, name="Lecture02.pdf",
            role="resource", section_idx=1, section_name="1주차",
        )
        self.assertEqual(
            path,
            "2026-2/TST1000_테스트/W01-L02__강의자료__Lecture02__f1407.pdf",
        )

    def test_real_sink_puts_archive_only_video_in_course_root(self):
        sink = object.__new__(RcloneOneDrive)
        path = sink.video_path(
            year="2026", semester="2학기", course_slug="CAS3116_컴퓨터비젼",
            title="Lec 1 - 9/1", cmid=4538981,
            section_idx=1, section_name="1주차 [9월01일 - 9월07일]",
        )
        self.assertEqual(
            path,
            "2026-2/CAS3116_컴퓨터비젼/"
            "W01-L00__강의영상__Lec 1 - 9_1__cmid4538981.mp4",
        )

    def test_file_goes_to_sink_without_local_blob_or_course_copy(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            sink = FakeSink()
            arc = Archiver(None, store, verbose=False, file_sink=sink)
            course = SimpleNamespace(
                course_id=1, year="2026", semester="2학기", slug="TST1000_테스트"
            )
            activity = SimpleNamespace(
                cmid=10, title="Lecture 1-2", section_idx=1,
                section_name="1주차", open_from=None,
            )

            arc._save_bytes(
                course, activity, b"lecture material", "https://example.test/1",
                "강의안.pdf", "resource", "unused", "materials",
            )
            record = store.file_record("https://example.test/1", "resource")

            self.assertEqual(record["remote_status"], "ok")
            self.assertNotIn("/강의자료/", record["remote_path"])
            self.assertIn("/W01-L02__강의자료__", record["remote_path"])
            self.assertEqual(record["bytes"], len(b"lecture material"))
            self.assertEqual(sink.files[record["remote_path"]], b"lecture material")
            self.assertEqual(list(Path(root, "blobs").rglob("*")), [])
            self.assertEqual(list(Path(root, "courses").rglob("*")), [])

    def test_file_id_is_stable_when_remote_status_is_updated(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            row = {
                "course_id": 1, "cmid": 10, "role": "resource", "name": "a.pdf",
                "url": "https://example.test/a", "sha256": "abc", "bytes": 3,
            }
            store.save_file(row)
            first_id = store.file_record(row["url"], row["role"])["id"]
            store.save_file({**row, "bytes": 4})
            second_id = store.file_record(row["url"], row["role"])["id"]
            self.assertEqual(first_id, second_id)

    def test_failed_direct_upload_is_marked_for_retry(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            arc = Archiver(None, store, verbose=False, file_sink=FailingSink())
            course = SimpleNamespace(
                course_id=1, year="2026", semester="2학기", slug="TST1000_테스트"
            )
            activity = SimpleNamespace(cmid=10, title="공지")
            with self.assertRaisesRegex(RuntimeError, "remote unavailable"):
                arc._save_bytes(
                    course, activity, b"attachment", "https://example.test/post-file",
                    "첨부.pdf", "post", "unused", "boards",
                )
            self.assertTrue(store.has_missing_file(10, "post"))


if __name__ == "__main__":
    unittest.main()
