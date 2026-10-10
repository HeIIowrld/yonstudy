import tempfile
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from yonstudy.archive import Archiver
from yonstudy.daily import SEOUL, build_daily_report, render_email_text, render_report_html
from yonstudy.exam_notices import collect_exam_notices
from yonstudy.parse import Post
from yonstudy.store import Store


class ExamNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.save_course({"course_id": 1, "name": "테스트과목", "year": "2026", "semester": "2학기"})
        for cmid, board in ((10, "과목공지"), (20, "질의응답")):
            self.store.save_activity({"cmid": cmid, "course_id": 1, "modname": "ubboard", "title": board, "restricted": 0})
        self.target = date(2026, 10, 10)

    def notice(self, post_id="1", *, title="중간시험 공지", body="일시: 2026년 10월 26일 11:00. 장소: D504. 범위: 1~4장.",
               published="2026-10-07T12:00:00", cmid=10):
        self.store.save_post({"course_id": 1, "cmid": cmid, "modname": "ubboard", "post_id": post_id,
                              "subject": title, "body": body, "url": f"https://example.test/post/{post_id}",
                              "written_at": published, "fetched_at": published, "checked_at": published})
        self.store.commit()

    def notices(self, target=None):
        return collect_exam_notices(self.store, target=target or self.target, year="2026", semester="2학기")

    def test_old_exam_notice_stays_visible_until_exam_date_in_both_email_formats(self):
        self.notice()
        report = build_daily_report(self.store, target=self.target)
        self.assertEqual(report.new_posts, [])
        self.assertEqual(report.exam_notices[0]["exam_date"], "2026-10-26")
        for body in (render_email_text(report), render_report_html(report)):
            self.assertIn("시험 일정·공지", body)
            self.assertIn("D-16", body)
            self.assertIn("11:00", body)
            self.assertIn("D504", body)
            self.assertIn("1~4장", body)
            self.assertLess(body.index("시험 일정·공지"), body.index("오늘 마감 과제"))
        self.assertEqual(self.notices(date(2026, 10, 26))[0]["days_until"], 0)
        self.assertEqual(self.notices(date(2026, 10, 27)), [])

    def test_english_dates_and_newer_midterm_location_keep_old_final_schedule(self):
        self.notice("1", title="Midterm / Final Exams", published="2026-09-01T12:00:00",
                    body="The midterm exam will be held on October 24 from 6PM. "
                         "The final exam will be held on December 12 from 6PM. Location to be announced.")
        self.notice("2", title="Midterm Exam", body="The midterm exam will take place on October 24 at 6PM. Location: ROOM 220.")
        rows = self.notices()
        self.assertEqual([(r["exam_kind"], r["exam_date"], r["post_id"]) for r in rows],
                         [("중간시험", "2026-10-24", "2"), ("기말시험", "2026-12-12", "1")])
        self.assertIn("ROOM 220", rows[0]["excerpt"])
        self.assertNotIn("October 24", rows[1]["excerpt"])
        self.assertIn("Location to be announced", rows[1]["excerpt"])

    def test_numeric_english_date_is_not_confused_with_chapter_range(self):
        self.notice(title="Midterm announcement",
                    body="Midterm 1. Date: 10/22 (Thursday) 10:00am ~ 12:00pm. Where: D504. Closed book: Chapter 1 ~ Chapter 4.")
        self.assertEqual(self.notices()[0]["exam_date"], "2026-10-22")

    def test_assignment_deadline_and_section_number_are_not_exam_dates(self):
        self.notice(title="Midterm Exam", body="The midterm exam will be held on October 22. "
                    "Assignment due October 15. 시험 범위: 10.20절.")
        self.assertEqual(self.notices()[0]["exam_date"], "2026-10-22")

    def test_date_range_is_shown_without_guessing_a_single_exam_day(self):
        self.notice(body="중간시험 일시: 10/22-10/23, 세부 시간은 추후 공지합니다.")
        self.assertIsNone(self.notices()[0]["exam_date"])
        self.assertTrue(self.notices()[0]["ambiguous_dates"])

    def test_questions_team_assignment_and_withdrawn_course_are_not_exam_schedules(self):
        self.notice(cmid=20, body="중간시험은 10월 26일인가요?")
        self.notice("2", title="중간고사 팀 배정 안내", body="중간고사 팀 배정 결과입니다.")
        self.assertEqual(self.notices(), [])
        self.notice("3")
        self.store.save_course({"course_id": 1, "enrolled": 0})
        self.store.commit()
        self.assertEqual(self.notices(), [])

    def test_updated_schedule_replaces_old_date_without_relabeling_unchanged_refresh(self):
        self.notice()
        original = dict(self.store.post_record(10, "ubboard", "1"))
        with patch("yonstudy.store._now", return_value="2026-10-10T06:00:00"):
            self.store.save_post({key: value for key, value in original.items() if key != "id"})
        self.assertEqual(self.store.post_record(10, "ubboard", "1")["updated_at"], original["fetched_at"])
        changed = {key: value for key, value in original.items() if key != "id"}
        changed["body"] = "중간시험 일시: 2026년 10월 28일 11:00. 장소: D505."
        with patch("yonstudy.store._now", return_value="2026-10-10T06:10:00"):
            self.store.save_post(changed)
        self.store.commit()
        report = build_daily_report(self.store, target=self.target)
        self.assertEqual(report.exam_notices[0]["exam_date"], "2026-10-28")
        self.assertTrue(report.exam_notices[0]["updated_today"])
        self.assertEqual(len(report.new_posts), 1)
        self.assertTrue(report.new_posts[0]["content_updated"])
        for body in (render_email_text(report), render_report_html(report)):
            self.assertIn("본문 수정 감지", body)
            self.assertIn("D505", body)
            self.assertEqual(body.count("https://example.test/post/1"), 1)

    def test_ambiguous_reschedule_and_cancellation_suppress_previous_schedule(self):
        self.notice()
        self.notice("2", title="중간시험 일정 변경", published="2026-10-09T12:00:00",
                    body="일시: 10/26 또는 10/28. 확정 일시는 추후 공지합니다.")
        rows = self.notices()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["exam_date"])
        self.assertTrue(rows[0]["ambiguous_dates"])
        self.notice("3", title="중간시험 취소", published="2026-10-10T12:00:00", body="중간시험은 취소되었습니다.")
        self.assertEqual(self.notices()[0]["post_id"], "3")
        self.assertEqual(self.notices(date(2026, 10, 27)), [])

    def test_undated_notice_does_not_guess_date_from_post_timestamp(self):
        self.notice(title="중간시험 범위 공지", body="시험 범위는 Chapter 1~4입니다.")
        self.assertIsNone(self.notices()[0]["exam_date"])
        self.assertEqual(self.notices(date(2026, 10, 23)), [])

    def test_plain_email_shows_body_of_ordinary_new_announcements(self):
        self.notice(title="강의실 변경", body="다음 수업은 D505에서 진행합니다.", published="2026-10-10T06:00:00")
        body = render_email_text(build_daily_report(self.store, target=self.target))
        self.assertIn("새 공지·Q&A", body)
        self.assertIn("다음 수업은 D505", body)

    def test_exam_body_is_refreshed_after_six_hours_even_if_listing_did_not_change(self):
        previous = (datetime.now(SEOUL) - timedelta(hours=7)).isoformat(timespec="seconds")
        self.notice(published=previous)
        listed = Post(post_id="1", subject="중간시험 공지", written_at=previous,
                      url="https://example.test/post/1")
        complete = Post(post_id="1", subject=listed.subject, written_at=previous, url=listed.url,
                        body="중간시험 일시: 2026년 10월 28일 11:00.")
        client = MagicMock()
        client.request.side_effect = ["listing", "article"]
        course = SimpleNamespace(course_id=1, url="https://example.test/course/1")
        activity = SimpleNamespace(cmid=10, title="과목공지", url="https://example.test/board/10")
        with patch("yonstudy.archive.P.parse_ubboard_list", return_value=([listed], 1)), \
             patch("yonstudy.archive.P.parse_ubboard_article", return_value=complete):
            Archiver(client, self.store, verbose=False)._sync_board(course, activity, "unused", 1, False)
        self.assertIn("28일", self.store.post_record(10, "ubboard", "1")["body"])
        self.assertEqual(client.request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
