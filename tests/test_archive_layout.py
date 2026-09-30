import unittest
import tempfile

from yonstudy.archive_layout import move_conflicts, plan_course_moves
from yonstudy.remote import organize_remote_archive
from yonstudy.store import Store


class ArchiveLayoutTests(unittest.TestCase):
    def setUp(self):
        self.course = {
            "year": "2026", "semester": "2학기", "slug": "CAS3135_클라우드컴퓨팅",
            "name": "클라우드컴퓨팅", "title": "클라우드컴퓨팅",
        }
        self.base = "2026-2/CAS3135_클라우드컴퓨팅/"

    def test_plan_keeps_week_files_flat_in_type_shelves(self):
        inventory = {self.base + path: 10 for path in (
            "W01-L01__강의자료__Intro__f1.pdf",
            "W02-L00__강의자료__dataset__f2.csv",
            "01-01 - Course Orientation.pdf",
            "강의자료/02주차/02-01 - Cloud.pdf",
            "강의자료/03주차/example.py",
            "클컴 1-1.m4a", "클컴 1-1.ko.srt",
            "W01-00__주차학습_자동생성.md", "강좌정보.md", "과제자료/a.pdf",
        )}
        moves = plan_course_moves(self.course, inventory, include_media=True)
        by_source = {move.source.removeprefix(self.base): move for move in moves}
        self.assertEqual(by_source["01-01 - Course Orientation.pdf"].target,
                         self.base + "강의자료/01-01 - Course Orientation.pdf")
        self.assertEqual(by_source["강의자료/02주차/02-01 - Cloud.pdf"].target,
                         self.base + "강의자료/02주차__02-01 - Cloud.pdf")
        self.assertEqual(by_source["W02-L00__강의자료__dataset__f2.csv"].target,
                         self.base + "강의자료/W02-L00__강의자료__dataset__f2.csv")
        self.assertEqual(by_source["강의자료/03주차/example.py"].target,
                         self.base + "강의자료/03주차__example.py")
        self.assertEqual(by_source["클컴 1-1.m4a"].target, self.base + "강의미디어/클컴 1-1.m4a")
        self.assertEqual(by_source["클컴 1-1.ko.srt"].target, self.base + "강의미디어/클컴 1-1.ko.srt")
        self.assertEqual(by_source["W01-00__주차학습_자동생성.md"].kind, "generated-backup")
        self.assertNotIn("강좌정보.md", by_source)
        self.assertNotIn("과제자료/a.pdf", by_source)

    def test_existing_destination_is_conflict_not_overwrite(self):
        inventory = {
            self.base + "slides.pdf": 10,
            self.base + "강의자료/slides.pdf": 10,
        }
        moves = plan_course_moves(self.course, inventory)
        self.assertEqual(len(move_conflicts(moves, inventory)), 1)

    def test_apply_aborts_entire_term_if_a_target_conflicts(self):
        class Sink:
            def __init__(self, files): self.files = files
            def _load_term(self, term):
                return {path: len(body) for path, body in self.files.items() if path.startswith(term + "/")}

        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({"course_id": 1, **self.course})
            store.commit()
            files = {
                self.base + "slides.pdf": b"source",
                self.base + "강의자료/slides.pdf": b"other",
                self.base + "notes.pdf": b"notes",
            }
            sink = Sink(files)
            result = organize_remote_archive(store, sink, year="2026", semester="2학기", dry_run=False)
            self.assertEqual(result["conflicts"], 1)
            self.assertEqual(result["moved"], 0)
            self.assertEqual(sink.files, files)

    def test_withdrawn_course_files_are_included_in_layout_plan(self):
        class Sink:
            def _load_term(self, term):
                return {self_file: 10, self_generated: 10}

        self_file = self.base + "lecture.pdf"
        self_generated = self.base + "W01-00__주차학습_자동생성.md"
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({"course_id": 1, **self.course, "enrolled": 0})
            result = organize_remote_archive(
                store, Sink(), year="2026", semester="2학기",
            )
            self.assertEqual(result["planned"], {"material": 1})

    def test_apply_updates_db_and_backs_up_generated_markdown(self):
        class Sink:
            def __init__(self, files):
                self.files = files

            def _load_term(self, term):
                return {path: len(body) for path, body in self.files.items() if path.startswith(term + "/")}

            def move(self, source, target, *, size=None):
                if source not in self.files or target in self.files:
                    return False
                if size is not None and len(self.files[source]) != size:
                    return False
                self.files[target] = self.files.pop(source)
                return True

            def upload_bytes(self, path, body, force=False):
                self.files[path] = body
                return True

        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({"course_id": 1, **self.course})
            source = self.base + "W01-L01__강의자료__Intro__f1.pdf"
            store.save_file({"course_id": 1, "cmid": 10, "role": "resource", "name": "Intro.pdf",
                             "url": "https://example.test/intro", "bytes": 3, "remote_path": source})
            store.commit()
            sink = Sink({source: b"pdf", self.base + "00_학습목차_자동생성.md": b"old"})
            preview = organize_remote_archive(store, sink, year="2026", semester="2학기")
            self.assertEqual(preview["planned"], {"generated-backup": 1, "material": 1})
            self.assertIn(source, sink.files)
            result = organize_remote_archive(store, sink, year="2026", semester="2학기", dry_run=False)
            self.assertEqual((result["moved"], result["backed_up"]), (1, 1))
            target = self.base + "강의자료/W01-L01__강의자료__Intro__f1.pdf"
            self.assertEqual(store.file_record("https://example.test/intro", "resource")["remote_path"], target)
            self.assertNotIn(source, sink.files)
            self.assertIn("2026-2/.yonstudy-layout-backup/CAS3135_클라우드컴퓨팅/00_학습목차_자동생성.md", sink.files)

    def test_media_move_keeps_recording_path_and_subtitle_together(self):
        class Sink:
            def __init__(self, files): self.files = files
            def _load_term(self, term):
                return {path: len(body) for path, body in self.files.items() if path.startswith(term + "/")}
            def move(self, source, target, *, size=None):
                if source not in self.files or target in self.files: return False
                self.files[target] = self.files.pop(source)
                return True
            def upload_bytes(self, path, body, force=False):
                self.files[path] = body
                return True

        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({"course_id": 1, **self.course})
            old = self.base + "클컴 1-1.m4a"
            store.db.execute(
                "INSERT INTO recording (sha256,course_id,match_status,path) VALUES (?,?,?,?)",
                ("abc", 1, "matched", "/archive/" + old),
            )
            store.commit()
            sink = Sink({old: b"audio", self.base + "클컴 1-1.ko.srt": b"subtitle"})
            result = organize_remote_archive(store, sink, year="2026", semester="2학기",
                                             dry_run=False, include_media=True)
            self.assertEqual(result["moved"], 2)
            self.assertIn(self.base + "강의미디어/클컴 1-1.ko.srt", sink.files)
            path = store.query("SELECT path FROM recording WHERE sha256='abc'")[0]["path"]
            self.assertEqual(path, "/archive/" + self.base + "강의미디어/클컴 1-1.m4a")


if __name__ == "__main__":
    unittest.main()
