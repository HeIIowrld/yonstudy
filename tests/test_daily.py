import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from yonstudy.daily import (
    SEOUL, build_daily_report, current_term, render_email_text,
    render_report, render_report_html, send_report,
)
from yonstudy.store import Store


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        today = datetime.now(SEOUL).date()
        self.today = today
        self.year, self.semester = current_term(today)
        self.store.save_course(
            {
                "course_id": 1,
                "year": self.year,
                "semester": self.semester,
                "name": "테스트과목",
                "title": "테스트과목 (TST1000)",
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def activity(self, completion="n"):
        day = self.today.isoformat()
        return {
            "cmid": 10,
            "course_id": 1,
            "modname": "vod",
            "title": "오늘 강의",
            "url": "https://example.test/vod/10",
            "section_idx": 1,
            "section_name": "1주차",
            "indent": 0,
            "completion": completion,
            "open_from": f"{day} 09:00:00",
            "open_to": f"{day} 23:59:59",
            "late_until": None,
            "duration": "10:00",
            "restricted": 0,
            "seen_at": f"{day}T10:00:00",
        }

    def test_opened_completion_transition_and_deduplication(self):
        self.store.save_activity(self.activity("n"))
        self.store.save_vod(
            {"cmid": 10, "course_id": 1, "progress_pct": 50, "duration_sec": 600}
        )
        self.store.save_activity(self.activity("y"))
        self.store.save_vod(
            {"cmid": 10, "course_id": 1, "progress_pct": 100, "duration_sec": 600}
        )
        self.store.commit()

        report = build_daily_report(self.store, target=self.today)
        self.assertFalse(report.stale)
        self.assertEqual(len(report.opened), 1)
        self.assertEqual(len(report.completed), 1)
        self.assertEqual(report.completed[0]["kind"], "vod_completed")
        self.assertEqual(report.today_schedule, [])
        self.assertIn("오늘 강의", render_report(report))

    def test_untracked_is_reported_separately(self):
        row = self.activity(None)
        row["cmid"] = 11
        row["modname"] = "ubfile"
        row["title"] = "강의자료"
        self.store.save_activity(row)
        self.store.commit()

        report = build_daily_report(self.store, target=self.today)
        self.assertEqual(report.completion_by_course[0]["incomplete"], 0)
        self.assertEqual(report.completion_by_course[0]["untracked"], 1)
        self.assertEqual(report.untracked[0]["modname"], "ubfile")

    def test_new_qna_and_material_are_separate_sections(self):
        day = self.today.isoformat()
        self.store.save_activity(
            {
                "cmid": 20,
                "course_id": 1,
                "modname": "ubboard",
                "title": "Anonymous Q&A Board",
                "url": "https://example.test/board/20",
                "completion": None,
                "restricted": 0,
                "seen_at": f"{day}T10:00:00",
            }
        )
        self.store.save_post(
            {
                "course_id": 1,
                "cmid": 20,
                "modname": "ubboard",
                "post_id": "42",
                "subject": "과제 문의드립니다",
                "writer": "학생",
                "written_at": f"{day} 12:00",
                "url": "https://example.test/post/42",
                "body": "과제의 제출 형식에 대해 질문드립니다.",
            }
        )
        self.store.save_file(
            {
                "course_id": 1,
                "cmid": 21,
                "role": "resource",
                "name": "1주차 강의안.pdf",
                "url": "https://example.test/file/21",
                "sha256": "abc",
                "bytes": 1024,
            }
        )
        self.store.commit()

        report = build_daily_report(self.store, target=self.today)
        self.assertEqual(report.new_posts[0]["category"], "Q&A")
        self.assertEqual(report.new_files[0]["name"], "1주차 강의안.pdf")
        body = render_report(report)
        self.assertIn("새 Q&A·공지·게시글", body)
        self.assertIn("새 강의자료·첨부", body)
        self.assertIn("[테스트과목] [Q&A · Anonymous Q&A Board]", body)
        self.assertIn("[테스트과목] [강의자료 · 활동명 없음] 1주차 강의안.pdf", body)

    def test_normal_and_late_deadlines_are_distinguished(self):
        row = self.activity("n")
        row["open_to"] = f"{self.today.isoformat()} 23:59:59"
        late = self.today + timedelta(days=7)
        row["late_until"] = f"{late.isoformat()} 23:59:59"
        self.store.save_activity(row)
        self.store.save_vod(
            {
                "cmid": 10,
                "course_id": 1,
                "progress_pct": 0,
                "duration_sec": 600,
                "watched_sec": 120,
                "max_rate": 2.0,
            }
        )
        self.store.commit()

        todo = build_daily_report(self.store, target=self.today).todos[0]
        self.assertEqual(todo["normal_deadline"], self.today.isoformat())
        self.assertEqual(todo["final_deadline"], late.isoformat())
        self.assertEqual(todo["remaining_minutes"], 8)
        self.assertEqual(todo["eta_minutes"], 4)
        html_body = render_report_html(build_daily_report(self.store, target=self.today))
        self.assertIn("출석 인정 마감", html_body)
        self.assertIn("지각 인정", html_body)

    def test_report_groups_schedule_assignments_queue_and_attendance(self):
        day = self.today.isoformat()
        self.store.save_activity(self.activity("n"))
        self.store.save_vod({
            "cmid": 10, "course_id": 1, "progress_pct": 50,
            "duration_sec": 600, "watched_sec": 300,
        })
        self.store.save_activity({
            "cmid": 30, "course_id": 1, "modname": "assign", "title": "오늘 과제",
            "url": "https://example.test/assign/30", "completion": "n",
            "restricted": 0, "seen_at": f"{day}T08:00:00",
        })
        self.store.save_submission({
            "cmid": 30, "course_id": 1, "modname": "assign", "title": "오늘 과제",
            "submitted": 0, "due_at": f"{day} 23:59:59", "seen_at": f"{day}T08:00:00",
        })
        self.store.commit()

        report = build_daily_report(self.store, target=self.today)
        self.assertEqual([row["title"] for row in report.assignments], ["오늘 과제"])
        self.assertEqual([row["cmid"] for row in report.viewing_queue], [10])
        self.assertFalse(report.attendance[0]["verified"])
        body = render_report(report)
        self.assertIn("오늘 일정", body)
        self.assertIn("제출할 과제", body)
        self.assertIn("과목별 순차 시청 목록", body)
        self.assertIn("온라인출석부 확인", body)
        html_body = render_report_html(report)
        self.assertIn("해야 할 일", html_body)
        self.assertIn("동영상 수강 현황", html_body)
        self.assertNotIn("ubboard", html_body)
        self.assertIn("할 일", render_email_text(report))

    @patch("yonstudy.daily.subprocess.run")
    def test_sendmail_transport(self, run):
        run.return_value = Mock(returncode=0, stderr=b"")
        with patch.dict(os.environ, {"YONSTUDY_SMTP_HOST": ""}):
            send_report(
                "테스트 본문",
                to="student@example.com",
                subject="LearnUs 리포트",
                sender="yonstudy@example.com",
                html_body="<strong>테스트 HTML</strong>",
            )
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/sbin/sendmail", "-t", "-oi"])
        self.assertIn(b"student@example.com", kwargs["input"])
        self.assertIn(b"multipart/alternative", kwargs["input"])
        self.assertIn(b"text/html", kwargs["input"])


if __name__ == "__main__":
    unittest.main()
