import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from yonstudy.store import Store


class StoreMigrationTests(unittest.TestCase):
    def test_existing_course_table_gets_enrollment_columns(self):
        with tempfile.TemporaryDirectory() as root:
            database = sqlite3.connect(Path(root) / "db.sqlite")
            database.execute(
                """
                CREATE TABLE course (
                    course_id INTEGER PRIMARY KEY,
                    year TEXT, semester TEXT, kind TEXT,
                    title TEXT, name TEXT, code TEXT, section TEXT,
                    slug TEXT, archived_at TEXT
                )
                """
            )
            database.execute(
                "INSERT INTO course (course_id,name) VALUES (1,'기존 강좌')"
            )
            database.commit()
            database.close()

            store = Store(root)
            row = store.query(
                "SELECT enrolled,unenrolled_at,detail_synced_at "
                "FROM course WHERE course_id=1"
            )[0]

            self.assertEqual(row["enrolled"], 1)
            self.assertIsNone(row["unenrolled_at"])
            self.assertIsNone(row["detail_synced_at"])

    def test_existing_activity_table_gets_presence_columns(self):
        with tempfile.TemporaryDirectory() as root:
            database = sqlite3.connect(Path(root) / "db.sqlite")
            database.execute(
                """
                CREATE TABLE activity (
                    cmid INTEGER PRIMARY KEY,
                    course_id INTEGER, modname TEXT, title TEXT, url TEXT,
                    section_idx INTEGER, section_name TEXT,
                    indent INTEGER, completion TEXT,
                    open_from TEXT, open_to TEXT, late_until TEXT,
                    duration TEXT, restricted INTEGER, seen_at TEXT
                )
                """
            )
            database.execute(
                "INSERT INTO activity (cmid,course_id,title) VALUES (10,1,'기존 활동')"
            )
            database.commit()
            database.close()

            store = Store(root)
            row = store.query(
                "SELECT present,removed_at FROM activity WHERE cmid=10"
            )[0]

            self.assertEqual(row["present"], 1)
            self.assertIsNone(row["removed_at"])

    def test_existing_post_table_gets_checked_at_column(self):
        with tempfile.TemporaryDirectory() as root:
            database = sqlite3.connect(Path(root) / "db.sqlite")
            database.execute(
                """
                CREATE TABLE post (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER, cmid INTEGER, modname TEXT,
                    post_id TEXT, thread_id TEXT,
                    no TEXT, subject TEXT, writer TEXT, written_at TEXT, hits TEXT,
                    replies INTEGER, url TEXT, body TEXT, fetched_at TEXT,
                    UNIQUE(cmid, modname, post_id)
                )
                """
            )
            database.commit()
            database.close()

            store = Store(root)
            columns = {
                row[1] for row in store.db.execute("PRAGMA table_info(post)")
            }

            self.assertIn("checked_at", columns)


class VodCompletionEventTests(unittest.TestCase):
    def test_completion_event_waits_for_max_position(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_activity(
                {
                    "cmid": 10,
                    "course_id": 1,
                    "modname": "vod",
                    "title": "강의",
                    "completion": None,
                }
            )
            store.save_vod(
                {
                    "cmid": 10,
                    "course_id": 1,
                    "progress_pct": 100,
                    "watched_sec": 300,
                    "duration_sec": 600,
                }
            )
            self.assertEqual(
                store.query("SELECT * FROM change_event WHERE kind='vod_completed'"),
                [],
            )

            # 부분 갱신이어도 DB에 저장된 진도율·길이와 합쳐 완주를 판정한다.
            store.save_vod({"cmid": 10, "course_id": 1, "watched_sec": 600})
            events = store.query(
                "SELECT * FROM change_event WHERE kind='vod_completed'"
            )

            self.assertEqual(len(events), 1)
            self.assertEqual(json.loads(events[0]["details"])["watched_sec"], 600)
            self.assertEqual(store.summary()["vods_done"], 1)


if __name__ == "__main__":
    unittest.main()
