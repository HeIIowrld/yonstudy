import tempfile
import unittest
from pathlib import Path

from yonstudy.flat_layout import (
    MAX_FILENAME_BYTES,
    build_flat_plan,
    canonical_filename,
    lesson_number,
    resource_filename,
    week_number,
)
from yonstudy.store import Store


class FlatLayoutTests(unittest.TestCase):
    def test_resource_filename_uses_week_then_date_fallback(self):
        weekly = resource_filename(
            section_idx=3, section_name="3주차", activity_title="Lecture 3-2",
            name="slides.pdf", file_id=17,
        )
        self.assertEqual(weekly, "W03-L02__강의자료__slides__f17.pdf")

        dated = resource_filename(
            section_idx=None, section_name=None, activity_title="오리엔테이션",
            name="orientation.pdf", file_id=18, saved_at="2026-09-03T08:00:00",
        )
        self.assertEqual(dated, "20260903__강의자료__orientation__f18.pdf")

    def test_week_and_lesson_normalization(self):
        self.assertEqual(week_number(1, "1주차 [9월01일 - 9월07일]", "Week 1-2"), 1)
        self.assertEqual(week_number(11, "", "11주차 동영상 강의"), 11)
        self.assertEqual(lesson_number("Week 1-2"), 2)
        self.assertEqual(lesson_number("01-01 Course Orientation"), 1)
        self.assertEqual(lesson_number("Lecture02"), 2)

    def test_filename_is_flat_stable_and_bounded(self):
        name = canonical_filename(
            week=1, lesson=2, kind="강의영상", title="아주 긴 제목" * 80,
            stable_id="cmid4529931", extension=".mp4",
        )
        self.assertTrue(name.startswith("W01-L02__강의영상__"))
        self.assertTrue(name.endswith("__cmid4529931.mp4"))
        self.assertLessEqual(len(name.encode("utf-8")), MAX_FILENAME_BYTES)

    def test_plan_keeps_only_term_and_course_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.save_course({
                "course_id": 1, "year": "2026", "semester": "2학기",
                "name": "테스트", "title": "테스트", "slug": "TST1000_테스트",
            })
            store.save_activity({
                "cmid": 10, "course_id": 1, "modname": "ubfile", "title": "Lecture02",
                "section_idx": 1, "section_name": "1주차", "url": "https://example/10",
            })
            digest, size = store.put_blob(b"pdf")
            store.save_file({
                "course_id": 1, "cmid": 10, "role": "resource", "name": "자료.pdf",
                "url": "https://example/file", "sha256": digest, "bytes": size,
            })
            store.commit()
            plan = build_flat_plan(store, destination="/archive", year="2026", semester="2학기")
            self.assertEqual(len(plan), 1)
            path = Path(plan[0].relative_path)
            self.assertEqual(len(path.parts), 3)
            self.assertEqual(path.parts[:2], ("2026-2", "TST1000_테스트"))
            self.assertTrue(path.name.startswith("W01-L02__강의자료__"))


if __name__ == "__main__":
    unittest.main()
