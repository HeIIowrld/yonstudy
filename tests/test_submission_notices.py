import tempfile
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from yonstudy.archive import Archiver
from yonstudy.daily import SEOUL, assignments_due_today, build_daily_report, render_email_text, render_report_html
from yonstudy.parse import Post
from yonstudy.store import Store
from yonstudy.submission_notices import submission_notice_label


class SubmissionNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.save_course({"course_id": 1, "name": "자료구조", "year": "2026", "semester": "2학기"})
        for cmid, title in ((10, "과목공지"), (20, "과제 질문 Q&A")):
            self.store.save_activity({"cmid": cmid, "course_id": 1, "modname": "ubboard", "title": title})
        self.target = date(2026, 10, 10)

    def notice(self, post_id="1", *, title="HW2 안내", body="제출 기한: 2026년 10월 14일 23:59",
               published="2026-09-30T19:00:00", cmid=10, modname="ubboard"):
        self.store.save_post({"course_id": 1, "cmid": cmid, "modname": modname, "post_id": post_id,
                              "subject": title, "body": body, "url": f"https://example.test/post/{post_id}",
                              "written_at": published, "writer": "", "replies": 0,
                              "fetched_at": published, "checked_at": published})
        self.store.commit()

    def assignment(self, cmid=30, *, title="HW2", submitted=0, requirement=None, due="2026-10-14 23:59"):
        self.store.save_activity({"cmid": cmid, "course_id": 1, "modname": "assign", "title": title,
                                  "url": f"https://example.test/assign/{cmid}", "restricted": 0})
        self.store.save_submission({"cmid": cmid, "course_id": 1, "modname": "assign", "title": title,
                                    "submitted": submitted, "due_at": due})
        if requirement:
            self.store.set_assignment_requirement(cmid, requirement, reason="대표자 제출")
        self.store.commit()

    def report(self, target=None):
        return build_daily_report(self.store, target=target or self.target)

    def test_deadline_persists_and_links_submission_state_in_both_mail_formats(self):
        self.notice()
        self.assignment()
        report = self.report()
        self.assertEqual(report.new_posts, [])
        row = report.submission_notices[0]
        self.assertEqual(row["notice_due_at"], "2026-10-14 23:59")
        self.assertEqual(row["linked_assignments"][0]["cmid"], 30)
        for body in (render_email_text(report), render_report_html(report)):
            self.assertIn("과제·제출 일정·공지", body)
            self.assertIn("D-4", body)
            self.assertIn("공지 일정 2026-10-14 23:59", body)
            self.assertIn("미제출", body)
            self.assertLess(body.index("과제·제출 일정·공지"), body.index("오늘 마감 과제"))
        self.assertEqual(self.report(date(2026, 10, 15)).submission_notices, [])

    def test_submitted_and_exempt_assignments_are_labeled_without_becoming_pending(self):
        self.notice()
        self.assignment(submitted=1)
        self.assertIn("제출 완료", submission_notice_label(self.report().submission_notices[0]))
        self.assignment(submitted=0, requirement="not_required")
        report = self.report(date(2026, 10, 14))
        self.assertIn("본인 제출 불필요", submission_notice_label(report.submission_notices[0]))
        self.assertEqual(assignments_due_today(report), [])
        self.assertFalse(any(row["cmid"] == 30 for row in report.todos))

    def test_external_journal_deadline_before_submission_verb_does_not_invent_pending_state(self):
        self.notice(title="2차 활동 일지 등록기간 마감일(~10/14(수) 23:59)",
                    body="활동기간: 9월 28일~10월 11일. 10/14(수) 23:59까지 등록해 주세요. 커리어연세에 제출합니다.")
        report = self.report()
        row = report.submission_notices[0]
        self.assertEqual(row["notice_due_at"], "2026-10-14 23:59")
        self.assertIn("제출 여부는 별도 확인", submission_notice_label(row))
        self.assertEqual(assignments_due_today(self.report(date(2026, 10, 14))), [])

    def test_past_journal_is_not_confused_with_activity_period_or_registration_instructions(self):
        self.notice(title="1차 활동 일지 등록기간 마감일(~9/28(월) 23:59)",
                    published="2026-09-28T16:17:00",
                    body="9월 11일(금)~9월 27 일(일)까지 활동한 내용을 활동 일지 등록기간에 등록해 주세요. "
                         "9/28 (월) 23:59은 [1차 활동일지 등록 기간 마감일] 입니다.")
        self.assertEqual(self.report().submission_notices, [])

    def test_schedule_table_is_split_by_task_and_keeps_late_window_without_guessing_times(self):
        self.notice(title='Early Release of Assignments "6. Hash" (deadline is October 23)',
                    body="Submission name point START END LATE "
                         "4. Trees 65 2026-09-23 2026-10-09 2026-10-11 "
                         "5. Heaps and Priority Queues 85 2026-10-02 2026-10-16 2026-10-18 "
                         "6. Hash 80 2026-10-08 2026-10-23 2026-10-25")
        self.assignment(title="4. Trees Coding Assignment", submitted=1, due="2026-10-09 23:59")
        self.assignment(31, title="5. Heaps and Priority Queues Coding Assignment", due="2026-10-16 23:59")
        rows = self.report().submission_notices
        self.assertEqual([row["notice_due_at"] for row in rows], ["2026-10-09", "2026-10-16", "2026-10-23"])
        self.assertEqual([row["late_until"] for row in rows], ["2026-10-11", "2026-10-18", "2026-10-25"])
        self.assertIn("제출 완료", submission_notice_label(rows[0]))
        self.assertIn("미제출", submission_notice_label(rows[1]))
        self.assertEqual(rows[2]["linked_assignments"], [])
        self.assertEqual(len(self.report(date(2026, 10, 12)).submission_notices), 2)

    def test_latest_table_overrides_older_table_one_assignment_at_a_time(self):
        header = "Submission name point START END LATE "
        self.notice(title="Assignment schedule", body=header + "4. Trees 65 2026-09-23 2026-10-09 2026-10-11")
        self.notice("2", title="Assignments schedule updated", published="2026-10-08T12:00:00",
                    body=header + "4. Trees 65 2026-09-23 2026-10-16 2026-10-18 "
                                  "5. Heaps 65 2026-10-02 2026-10-23 2026-10-25")
        rows = self.report().submission_notices
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["notice_due_at"], "2026-10-16")
        self.assertEqual(rows[0]["post_id"], "2")

    def test_date_only_deadline_has_no_invented_time(self):
        self.notice(body="The deadline is October 14.")
        row = self.report().submission_notices[0]
        self.assertEqual(row["notice_due_at"], "2026-10-14")
        self.assertNotIn("23:59", submission_notice_label(row))

    def test_inferred_deadline_is_not_labeled_as_an_explicit_site_time(self):
        self.notice(body="The deadline is October 14.")
        self.assignment(due=None)
        self.store.save_assignment_deadline({"cmid": 30, "due_at": "2026-10-14 23:59", "source_kind": "post"})
        row = self.report().submission_notices[0]
        self.assertEqual(row["notice_due_at"], "2026-10-14")
        self.assertNotIn("사이트 마감", submission_notice_label(row))
        self.assertNotIn("23:59", submission_notice_label(row))

    def test_submission_times_without_minutes_or_meridiem_are_preserved(self):
        for body, expected in (("제출 마감: 10/14 18시", "2026-10-14 18:00"),
                               ("Due: October 14 at 6PM", "2026-10-14 18:00")):
            with self.subTest(body=body):
                self.notice(body=body)
                self.assertEqual(self.report().submission_notices[0]["notice_due_at"], expected)

    def test_title_date_and_body_time_are_combined_without_false_ambiguity(self):
        self.notice(title="보고서 제출 마감(~10/14)", body="10/14(수) 오후 6시까지 제출해 주세요.")
        row = self.report().submission_notices[0]
        self.assertEqual(row["notice_due_at"], "2026-10-14 18:00")
        self.assertFalse(row["ambiguous_dates"])

    def test_chapter_numbers_and_posted_dates_are_not_deadlines(self):
        self.notice(body="마감은 추후 공지합니다. 과제 범위: Chapter 10.20절. 게시일: 10/10.")
        self.assertIsNone(self.report().submission_notices[0]["notice_due_at"])

    def test_presentation_date_is_a_schedule_without_claiming_submission_is_due(self):
        self.notice(title="프로젝트 발표 안내", body="발표 일시: 10/10 18시. 장소: D504.")
        row = self.report().submission_notices[0]
        self.assertEqual(row["notice_due_at"], "2026-10-10 18:00")
        self.assertIn("오늘 일정", submission_notice_label(row))
        self.assertNotIn("오늘 마감", submission_notice_label(row))

    def test_midterm_project_is_a_submission_notice_rather_than_an_exam(self):
        self.notice(title="Midterm Project Report", body="Submission deadline: October 26 at 6PM")
        report = self.report()
        self.assertEqual(report.exam_notices, [])
        self.assertEqual(report.submission_notices[0]["notice_due_at"], "2026-10-26 18:00")

    def test_distinct_deadlines_and_relative_dates_require_original_notice(self):
        self.notice(title="프로젝트 보고서 안내", body="초안 마감: 10/14. 최종 보고서 마감: 10/21.")
        row = self.report().submission_notices[0]
        self.assertIsNone(row["notice_due_at"])
        self.assertTrue(row["ambiguous_dates"])
        self.notice("2", title="발표 자료 제출 안내", body="다음 수업 전까지 제출해 주세요.")
        self.assertTrue(all(row["notice_due_at"] is None for row in self.report().submission_notices))

    def test_explicit_deadline_change_replaces_original_and_unknown_change_suppresses_old_date(self):
        self.notice()
        self.notice("2", title="HW2 마감 변경", body="제출 기한을 기존 10/14에서 10/12로 변경합니다.",
                    published="2026-10-08T12:00:00")
        rows = self.report().submission_notices
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["notice_due_at"], "2026-10-12")
        self.notice("3", title="HW2 제출 기한 변경", body="새 제출 기한은 추후 공지합니다.", published="2026-10-09T12:00:00")
        rows = self.report().submission_notices
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["notice_due_at"])

    def test_preparation_is_not_a_submission_and_exam_or_question_posts_are_excluded(self):
        self.notice(title="과제 (제출하지 않음) - Node.js 준비", body="10/14 실습 전에 코드를 읽어 오세요.")
        self.notice("2", title="중간시험 안내", body="10/26에 답안지를 제출합니다.")
        self.notice("3", title="HW2 마감 질문", cmid=20)
        rows = self.report().submission_notices
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["notice_due_at"])
        self.assertIn("준비 안내 · 제출 없음", submission_notice_label(rows[0]))
        self.assertNotIn("미제출", submission_notice_label(rows[0]))

    def test_ambiguous_assignment_numbers_do_not_assign_another_tasks_status(self):
        self.notice()
        self.assignment(title="HW2 개인 과제", submitted=1)
        self.assignment(31, title="HW2 팀 과제", submitted=0, requirement="not_required")
        row = self.report().submission_notices[0]
        self.assertEqual(row["linked_assignments"], [])
        self.assertIn("제출 여부는 별도 확인", submission_notice_label(row))

    def test_undated_announcements_expire_and_withdrawn_or_removed_boards_are_excluded(self):
        self.notice(body="과제 작성 시 PDF 파일로 업로드해 주세요.")
        self.assertEqual(len(self.report().submission_notices), 1)
        self.assertEqual(self.report(date(2026, 10, 15)).submission_notices, [])
        self.store.query("UPDATE activity SET present=0 WHERE cmid=10")
        self.assertEqual(self.report().submission_notices, [])
        self.store.query("UPDATE activity SET present=1 WHERE cmid=10")
        self.store.save_course({"course_id": 1, "enrolled": 0})
        self.assertEqual(self.report().submission_notices, [])

    def test_body_update_changes_deadline_and_deduplicates_new_post_in_email(self):
        self.notice()
        original = dict(self.store.post_record(10, "ubboard", "1"))
        changed = {key: value for key, value in original.items() if key != "id"}
        changed["body"] = "제출 마감: 2026년 10월 16일 오후 11시 59분"
        with patch("yonstudy.store._now", return_value="2026-10-10T06:00:00"):
            self.store.save_post(changed)
        report = self.report()
        row = report.submission_notices[0]
        self.assertEqual(row["notice_due_at"], "2026-10-16 23:59")
        for body in (render_email_text(report), render_report_html(report)):
            self.assertIn("본문 수정 감지", body)
            self.assertEqual(body.count("https://example.test/post/1"), 1)

    def test_assignment_notice_body_is_refreshed_after_six_hours(self):
        previous = (datetime.now(SEOUL) - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%S")
        self.notice(published=previous)
        listed = Post(post_id="1", subject="HW2 안내", written_at=previous, url="https://example.test/post/1")
        article = Post(post_id="1", subject=listed.subject, written_at=previous, url=listed.url,
                       body="제출 마감: 10/16 23:59")
        client = MagicMock()
        client.request.side_effect = ["listing", "article"]
        course = SimpleNamespace(course_id=1, url="https://example.test/course/1")
        activity = SimpleNamespace(cmid=10, title="과목공지", url="https://example.test/board/10")
        with patch("yonstudy.archive.P.parse_ubboard_list", return_value=([listed], 1)), \
             patch("yonstudy.archive.P.parse_ubboard_article", return_value=article):
            Archiver(client, self.store, verbose=False)._sync_board(course, activity, "unused", 1, False)
        self.assertIn("10/16", self.store.post_record(10, "ubboard", "1")["body"])
        self.assertEqual(client.request.call_count, 2)

    def test_official_forum_schedule_refreshes_without_showing_thread_marker(self):
        previous = (datetime.now(SEOUL) - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%S")
        self.store.save_activity({"cmid": 10, "course_id": 1, "modname": "forum", "title": "Announcements"})
        self.notice("t42", title="HW2 안내", body="", published=previous, modname="forum")
        self.notice("42", published=previous, modname="forum")
        article = Post(post_id="42", subject="HW2 안내", written_at=previous, body="제출 마감: 10/16 23:59")
        client = MagicMock()
        client.request.side_effect = ["listing", "discussion"]
        course = SimpleNamespace(course_id=1, url="https://example.test/course/1")
        activity = SimpleNamespace(cmid=10, title="Announcements", url="https://example.test/forum/10")
        with patch("yonstudy.archive.P.parse_forum_discussions", return_value=[("42", "HW2 안내")]), \
             patch("yonstudy.archive.P.parse_forum_discussion", return_value=[article]):
            Archiver(client, self.store, verbose=False)._sync_forum(course, activity, "unused", False)
        rows = self.report().submission_notices
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["post_id"], "42")
        self.assertEqual(rows[0]["notice_due_at"], "2026-10-16 23:59")


if __name__ == "__main__":
    unittest.main()
