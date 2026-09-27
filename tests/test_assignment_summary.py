import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from yonstudy.assignment_summary import api_key_for_store, generate_summary, summarize_assignments
from yonstudy.automation import run_daily_automation
from yonstudy.daily import SEOUL, build_daily_report, current_term, render_email_text, render_report_html
from yonstudy.export import export_tree, term_folder
from yonstudy.store import Store


class AssignmentSummaryTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.output = tempfile.TemporaryDirectory()
        self.store = Store(self.data.name)
        self.today = datetime.now(SEOUL).date()
        self.year, self.semester = current_term(self.today)
        self.store.save_course({
            "course_id": 1, "year": self.year, "semester": self.semester,
            "name": "자료구조", "title": "자료구조", "slug": "DS",
        })
        self.store.save_activity({
            "cmid": 10, "course_id": 1, "modname": "assign", "title": "트리 과제",
            "url": "https://example.test/assignment/10", "restricted": 0,
        })
        self.store.save_submission({
            "cmid": 10, "course_id": 1, "modname": "assign", "title": "트리 과제",
            "instructions": "이진 탐색 트리를 구현하고 코드와 보고서를 제출하세요.",
            "due_at": f"{self.today.isoformat()} 23:59", "submitted": 0,
        })
        self.store.commit()

    def tearDown(self):
        self.data.cleanup()
        self.output.cleanup()

    @staticmethod
    def response():
        content = json.dumps({
            "one_line": "이진 탐색 트리를 구현하고 결과를 설명한다",
            "deliverables": ["소스 코드", "보고서"],
            "requirements": ["이진 탐색 트리 구현"],
        }, ensure_ascii=False)
        return io.BytesIO(json.dumps({
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": content}]}}],
        }, ensure_ascii=False).encode("utf-8"))

    def test_request_cache_and_source_change_visibility(self):
        with patch("yonstudy.assignment_summary.urlopen", return_value=self.response()) as request:
            first = summarize_assignments(
                self.store, api_key="test-key", year=self.year, semester=self.semester,
            )
        self.assertEqual(first.generated, 1)
        sent = request.call_args.args[0]
        self.assertNotIn("test-key", sent.full_url)
        self.assertEqual(sent.get_header("X-goog-api-key"), "test-key")
        self.assertEqual(json.loads(sent.data)["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(json.loads(sent.data)["contents"][0]["parts"][0]["text"].count("이진 탐색 트리"), 1)

        report = build_daily_report(self.store, target=self.today)
        self.assertIn("이진 탐색 트리를 구현", render_email_text(report))
        self.assertIn("이진 탐색 트리를 구현", render_report_html(report))
        export_tree(self.store, self.output.name, year=self.year, semester=self.semester)
        index = Path(self.output.name) / term_folder(self.year, self.semester) / "과제목록.html"
        self.assertIn("이진 탐색 트리를 구현", index.read_text(encoding="utf-8"))
        detail = next(Path(self.output.name).rglob("*cmid10.html"))
        self.assertIn("소스 코드", detail.read_text(encoding="utf-8"))
        standalone = next(Path(self.output.name).rglob("과제요약_자동생성.md"))
        self.assertIn("이진 탐색 트리를 구현", standalone.read_text(encoding="utf-8"))

        with patch("yonstudy.assignment_summary.urlopen") as request:
            cached = summarize_assignments(
                self.store, api_key="test-key", year=self.year, semester=self.semester,
            )
        self.assertEqual(cached.generated, 0)
        self.assertEqual(cached.skipped, 1)
        request.assert_not_called()

        self.store.save_submission({"cmid": 10, "instructions": "해시 테이블을 구현하세요."})
        self.store.commit()
        stale_report = build_daily_report(self.store, target=self.today)
        self.assertIsNone(stale_report.semester_assignments[0]["one_line"])
        export_tree(self.store, self.output.name, year=self.year, semester=self.semester)
        self.assertNotIn("이진 탐색 트리를 구현", index.read_text(encoding="utf-8"))
        self.assertEqual(summarize_assignments(self.store, api_key=None, dry_run=True).pending, 1)

    def test_dry_run_does_not_need_key_or_make_request(self):
        with patch("yonstudy.assignment_summary.urlopen") as request:
            result = summarize_assignments(self.store, api_key=None, dry_run=True)
        self.assertEqual(result.pending, 1)
        self.assertEqual(result.sample_cmids, [10])
        request.assert_not_called()

    def test_bad_or_incomplete_response_is_not_saved(self):
        with patch("yonstudy.assignment_summary.urlopen", return_value=io.BytesIO(b'{"candidates":[]}')):
            result = summarize_assignments(self.store, api_key="test-key")
        self.assertEqual(result.failed, 1)
        self.assertEqual(self.store.query("SELECT * FROM assignment_summary"), [])

    def test_store_key_file_requires_private_permissions(self):
        key_file = Path(self.data.name) / "gemini-api-key"
        key_file.write_text("example-only\n", encoding="utf-8")
        key_file.chmod(0o644)
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                api_key_for_store(self.data.name)
            key_file.chmod(0o600)
            self.assertEqual(api_key_for_store(self.data.name), "example-only")

    def test_daily_automation_generates_summary_when_key_file_is_present(self):
        key_file = Path(self.data.name) / "gemini-api-key"
        key_file.write_text("example-only\n", encoding="utf-8")
        key_file.chmod(0o600)
        with patch("yonstudy.assignment_summary.generate_summary", return_value={
            "one_line": "이진 탐색 트리를 구현한다",
            "deliverables": ["소스 코드"], "requirements": [],
        }) as generate:
            code, state = run_daily_automation(
                store_path=self.data.name, cookie_path="unused",
                export_dir=self.output.name, remote="unused:",
                sync=False, upload_remote=False, send_mail=False,
            )
        self.assertEqual(code, 0)
        self.assertEqual(state["summaries"]["generated"], 1)
        generate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
