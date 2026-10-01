import tempfile
import unittest

from yonstudy.deadlines import (
    assignment_number,
    extract_deadline,
    extract_deadline_table,
    infer_course_assignment_deadlines,
)
from yonstudy.store import Store


class DeadlineParsingTests(unittest.TestCase):
    def test_korean_and_english_deadlines_are_normalized(self):
        korean = extract_deadline(
            "제출 기한: 2026년 10월 7일(수) 오후 11시 59분",
            year_hint=2026,
        )
        english = extract_deadline(
            "Assignment 1 is due next Friday, September 18, at 11:59 PM.",
            year_hint=2026,
        )
        numeric = extract_deadline(
            "Deadline: 10/11 Sun 11:59pm", year_hint=2026,
            reference_date="2026-09-27 19:54",
        )

        self.assertEqual(korean.due_at, "2026-10-07 23:59")
        self.assertEqual(english.due_at, "2026-09-18 23:59")
        self.assertEqual(numeric.due_at, "2026-10-11 23:59")

    def test_assignment_numbers_and_flat_deadline_table(self):
        self.assertEqual(assignment_number("3. Queues and Stacks Coding Assignment"), 3)
        self.assertEqual(assignment_number('Early Release of Assignments "4. Trees"'), 4)
        table = extract_deadline_table(
            "Submission name point START END LATE "
            "1. Recursion 75 2026-09-08 2026-09-18 2026-09-20 "
            "2. Arrays and Linked Lists 75 2026-09-15 2026-09-25 2026-09-27"
        )

        self.assertEqual(table[1].due_at, "2026-09-18 23:59")
        self.assertEqual(table[1].late_until, "2026-09-20 23:59")
        self.assertEqual(table[2].due_at, "2026-09-25 23:59")

    def test_course_inference_links_announcement_to_lti_assignment(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({
                "course_id": 1, "year": "2026", "semester": "2학기",
                "name": "자료구조",
            })
            store.save_activity({
                "cmid": 10, "course_id": 1, "modname": "lti",
                "title": "3. Queues and Stacks Coding Assignment",
            })
            store.save_submission({
                "cmid": 10, "course_id": 1, "modname": "lti",
                "title": "3. Queues and Stacks Coding Assignment",
                "submitted": None, "status": "제출 상태 미확인",
            })
            store.save_post({
                "course_id": 1, "cmid": 20, "modname": "ubboard",
                "post_id": "42", "written_at": "2026-09-18 13:40",
                "subject": 'Early Release of Assignments "3. Queues and Stacks"',
                "body": "The deadline is October 2.",
                "url": "https://example.test/post/42",
            })
            store.commit()

            self.assertEqual(infer_course_assignment_deadlines(store, 1), 1)
            row = store.query("SELECT * FROM assignment_deadline WHERE cmid=10")[0]

            self.assertEqual(row["due_at"], "2026-10-02 23:59")
            self.assertEqual(row["source_kind"], "post")
            self.assertEqual(row["source_url"], "https://example.test/post/42")


if __name__ == "__main__":
    unittest.main()
