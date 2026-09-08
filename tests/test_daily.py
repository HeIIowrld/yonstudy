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
            {
                "cmid": 10, "course_id": 1, "progress_pct": 50,
                "duration_sec": 600, "is_progress": 1,
                "can_log_progress": 1, "status": "ok",
            }
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

    def test_unenrolled_course_is_excluded_from_incomplete_counts(self):
        self.store.save_activity(self.activity("n"))
        self.store.save_vod(
            {
                "cmid": 10,
                "course_id": 1,
                "progress_pct": 0,
                "watched_sec": 0,
                "duration_sec": 600,
                "is_progress": 1,
                "can_log_progress": 1,
                "status": "ok",
            }
        )
        self.store.save_course({"course_id": 1, "enrolled": 0})
        self.store.commit()

        report = build_daily_report(self.store, target=self.today)

        self.assertEqual(report.todos, [])
        self.assertEqual(report.viewing_queue, [])
        self.assertEqual(report.attendance, [])
        self.assertEqual(report.completion_by_course, [])

    def test_freshness_requires_every_enrolled_course_to_be_current(self):
        day = self.today.isoformat()
        self.store.save_activity(self.activity("n"))
        self.store.save_course({
            "course_id": 2, "year": self.year, "semester": self.semester,
            "name": "동기화 누락 과목", "title": "동기화 누락 과목",
        })
        self.store.save_activity({
            "cmid": 20, "course_id": 2, "modname": "url", "title": "오래된 활동",
            "completion": None, "restricted": 0,
            "seen_at": "2026-01-01T00:00:00",
        })
        self.store.commit()

        report = build_daily_report(self.store, target=self.today)

        self.assertTrue(report.stale)
        self.assertEqual(report.source_updated_at, "2026-01-01T00:00:00")
        self.assertTrue(any(row["course_name"] == "동기화 누락 과목"
                            for row in report.completion_by_course))

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
                "is_progress": 1,
                "can_log_progress": 1,
                "status": "ok",
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
            "is_progress": 1, "can_log_progress": 1, "status": "ok",
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
        self.assertIn("이번 학기 강의별 수강 현황", body)
        html_body = render_report_html(report)
        self.assertIn("현재 남은 항목", html_body)
        self.assertIn("과목별 동영상 수강률", html_body)
        self.assertNotIn("ubboard", html_body)
        self.assertIn("현재 남은 항목", render_email_text(report))

    def test_report_does_not_count_percent_only_video_as_complete(self):
        self.store.save_activity(self.activity("n"))
        self.store.save_vod(
            {
                "cmid": 10,
                "course_id": 1,
                "progress_pct": 100,
                "duration_sec": 600,
                "watched_sec": 300,
                "is_progress": 1,
                "can_log_progress": 1,
                "status": "ok",
            }
        )
        self.store.commit()

        report = build_daily_report(self.store, target=self.today)

        self.assertFalse(report.attendance[0]["verified"])
        self.assertEqual([row["cmid"] for row in report.todos], [10])
        self.assertEqual(report.completion_by_course[0]["effective_done"], 0)
        self.assertEqual(report.completion_by_course[0]["effective_incomplete"], 1)
        self.assertIn("확인 필요", render_email_text(report))

    def test_email_lists_full_term_videos_but_excludes_future_from_rate(self):
        day = self.today.isoformat()
        tomorrow = (self.today + timedelta(days=1)).isoformat()
        videos = [
            (10, "완료한 강의", "y", day, 100, 600),
            (11, "듣는 중인 강의", "n", day, 50, 300),
            (12, "아직 안 본 강의", "n", day, 0, 0),
            (13, "내일 공개 강의", "n", tomorrow, 0, 0),
        ]
        for cmid, title, completion, opens, progress, watched in videos:
            row = self.activity(completion)
            row.update(
                cmid=cmid,
                title=title,
                url=f"https://example.test/vod/{cmid}",
                open_from=f"{opens} 09:00:00",
                open_to=f"{opens} 23:59:59",
                seen_at=f"{day}T10:00:00",
            )
            self.store.save_activity(row)
            self.store.save_vod(
                {
                    "cmid": cmid,
                    "course_id": 1,
                    "progress_pct": progress,
                    "duration_sec": 600,
                    "watched_sec": watched,
                    "is_progress": 1,
                    "can_log_progress": 1,
                    "status": "ok",
                }
            )
        self.store.commit()

        report = build_daily_report(self.store, target=self.today)
        self.assertEqual(len(report.attendance), 4)
        self.assertNotIn(13, {row["cmid"] for row in report.todos})
        self.assertEqual(report.completion_by_course[0]["effective_incomplete"], 2)
        self.assertEqual(report.completion_by_course[0]["upcoming"], 1)

        text_body = render_email_text(report)
        self.assertIn("과목별 동영상 수강률", text_body)
        self.assertIn("테스트과목: 공개분 1/3개 수강 완료 · 수강률 33%", text_body)
        self.assertIn("학기 전체 확인 4개 · 공개 예정 1개", text_body)
        self.assertIn("이번 학기 강의 목록 (4개)", text_body)
        self.assertIn("공개 예정 · 테스트과목 · 1주차 · 내일 공개 강의", text_body)
        self.assertNotIn("마지막 재생", text_body)

        html_body = render_report_html(report)
        self.assertIn("과목별 동영상 수강률", html_body)
        self.assertIn("1/3개", html_body)
        self.assertIn("width:33%", html_body)
        self.assertIn("학기 전체 4개 · 공개 예정 1개", html_body)
        self.assertIn("이번 학기 강의 목록 (4개)", html_body)
        self.assertIn("내일 공개 강의", html_body)
        self.assertNotIn("동영상 수강 현황", html_body)

        full_body = render_report(report)
        self.assertIn("[공개 예정] [테스트과목]", full_body)
        self.assertNotIn("내일 공개 강의 · 미완료", full_body)

    def test_report_lists_all_term_assignment_submission_states(self):
        day = self.today.isoformat()
        tomorrow = (self.today + timedelta(days=1)).isoformat()
        assignments = [
            (30, "제출한 과제", 1, day),
            (31, "남은 과제", 0, day),
            (32, "다음 과제", 0, tomorrow),
        ]
        for cmid, title, submitted, opens in assignments:
            self.store.save_activity({
                "cmid": cmid,
                "course_id": 1,
                "modname": "assign",
                "title": title,
                "url": f"https://example.test/assign/{cmid}",
                "completion": "y" if submitted else "n",
                "open_from": f"{opens} 09:00:00",
                "open_to": f"{tomorrow} 23:59:59",
                "restricted": 0,
                "seen_at": f"{day}T10:00:00",
            })
            self.store.save_submission({
                "cmid": cmid,
                "course_id": 1,
                "modname": "assign",
                "title": title,
                "submitted": submitted,
                "due_at": f"{tomorrow} 23:59:59",
                "seen_at": f"{day}T10:00:00",
            })
        self.store.commit()

        report = build_daily_report(self.store, target=self.today)
        self.assertEqual(len(report.semester_assignments), 3)
        self.assertNotIn(32, {row["cmid"] for row in report.assignments})
        self.assertNotIn(32, {row["cmid"] for row in report.todos})

        text_body = render_email_text(report)
        self.assertIn("이번 학기 과제·제출 목록 (3개)", text_body)
        self.assertIn("제출 완료 · 테스트과목 · 제출한 과제", text_body)
        self.assertIn("미제출 · 테스트과목 · 남은 과제", text_body)
        self.assertIn("공개 예정 · 테스트과목 · 다음 과제", text_body)
        self.assertIn("현재 남은 항목", text_body)

        html_body = render_report_html(report)
        self.assertIn("이번 학기 과제·제출 목록 (3개)", html_body)
        self.assertIn("제출 완료", html_body)
        self.assertIn("공개 예정", html_body)

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
