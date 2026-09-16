import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from yonstudy.daily import SEOUL, build_daily_report, render_email_text, render_report_html
from yonstudy.deadline_reminder import run_deadline_reminder
from yonstudy.store import Store


class DeadlineReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.save_course({
            "course_id": 1, "year": "2026", "semester": "2학기", "name": "테스트",
        })
        self.now = datetime(2026, 9, 16, 22, 0, tzinfo=SEOUL)
        clock = patch("yonstudy.deadline_reminder.datetime")
        self.clock = clock.start()
        self.clock.now.return_value = self.now
        self.addCleanup(clock.stop)
        auth = patch("yonstudy.deadline_reminder.ensure_session")
        auth.start()
        self.addCleanup(auth.stop)
        env = patch.dict(os.environ, {"YONSTUDY_REPORT_TO": "me@example.test"})
        env.start()
        self.addCleanup(env.stop)

    def assignment(self, cmid=10, due="2026-09-16 23:59", submitted=0):
        self.store.save_activity({
            "cmid": cmid, "course_id": 1, "modname": "assign",
            "title": f"과제 {cmid}", "url": f"https://ys.learnus.org/mod/assign/view.php?id={cmid}",
            "completion": "n",
        })
        self.store.save_submission({
            "cmid": cmid, "course_id": 1, "title": f"과제 {cmid}", "modname": "assign",
            "due_at": due, "submitted": submitted,
        })
        self.store.commit()

    def run_reminder(self, **kwargs):
        return run_deadline_reminder(store_path=self.tmp.name, cookie_path="unused", **kwargs)

    def test_today_formats_render_in_both_morning_email_bodies(self):
        self.assignment(due="2026- 9월-16 23:59")
        self.assignment(11, submitted=1)
        report = build_daily_report(self.store, target=self.now.date(), year="2026", semester="2학기")
        for body in (render_email_text(report), render_report_html(report)):
            self.assertIn("오늘 마감 과제 (2개)", body)
            self.assertIn("제출 완료", body)
            self.assertIn("미제출", body)
            self.assertIn("22시", body)

    def test_mail_contains_only_todays_pending_and_is_sent_once(self):
        self.assignment(due="2026- 9월-16 20:00")  # 이미 시간이 지난 오늘 과제도 포함
        self.assignment(11, submitted=1)
        self.assignment(12, due="2026-09-15 23:59")
        self.assignment(13, due="2026-09-17 23:59")
        with patch("yonstudy.deadline_reminder.refresh_assignments", return_value=[]) as refresh, \
             patch("yonstudy.deadline_reminder.send_report") as send:
            code, state = self.run_reminder()
            self.assertEqual((code, state["status"]), (0, "sent"))
            self.assertEqual([r["cmid"] for r in state["pending"]], [10])
            self.assertIn("과제 10", send.call_args.args[0])
            self.assertNotIn("과제 11", send.call_args.args[0])
            self.assertEqual(self.run_reminder()[1]["status"], "already_sent")
            refresh.assert_called_once()
            send.assert_called_once()

    def test_submitted_during_day_is_refreshed_before_decision(self):
        self.assignment()

        def refreshed(*args, **kwargs):
            self.assignment(submitted=1)
            return []

        with patch("yonstudy.deadline_reminder.refresh_assignments", side_effect=refreshed), \
             patch("yonstudy.deadline_reminder.send_report") as send:
            self.assertEqual(self.run_reminder()[1]["status"], "nothing_pending")
            send.assert_not_called()

    def test_new_deadline_is_discovered_by_evening_refresh(self):
        def refreshed(*args, **kwargs):
            self.assignment()
            return []
        with patch("yonstudy.deadline_reminder.refresh_assignments", side_effect=refreshed), \
             patch("yonstudy.deadline_reminder.send_report") as send:
            self.assertEqual(self.run_reminder()[1]["status"], "sent")
            send.assert_called_once()

    def test_no_due_assignments_sends_nothing(self):
        with patch("yonstudy.deadline_reminder.refresh_assignments", return_value=[]), \
             patch("yonstudy.deadline_reminder.send_report") as send:
            self.assertEqual(self.run_reminder()[1]["status"], "nothing_pending")
            send.assert_not_called()

    def test_changed_due_date_is_not_notified(self):
        self.assignment()
        def refreshed(*args, **kwargs):
            self.assignment(due="2026-09-18 23:59")
            return []
        with patch("yonstudy.deadline_reminder.refresh_assignments", side_effect=refreshed), \
             patch("yonstudy.deadline_reminder.send_report") as send:
            self.assertEqual(self.run_reminder()[1]["status"], "nothing_pending")
            send.assert_not_called()

    def test_failed_refresh_does_not_send_cached_pending(self):
        self.assignment()
        with patch("yonstudy.deadline_reminder.refresh_assignments", return_value=[
            {"course_id": 1, "error": "HTTP error"}
        ]), patch("yonstudy.deadline_reminder.send_report") as send:
            code, state = self.run_reminder()
            self.assertEqual((code, state["status"]), (1, "check_incomplete"))
            send.assert_not_called()

    def test_unknown_submission_is_not_called_pending(self):
        self.assignment(submitted=None)
        with patch("yonstudy.deadline_reminder.refresh_assignments", return_value=[]), \
             patch("yonstudy.deadline_reminder.send_report") as send:
            self.assertEqual(self.run_reminder()[1]["status"], "check_incomplete")
            send.assert_not_called()

    def test_failed_mail_can_retry(self):
        self.assignment()
        with patch("yonstudy.deadline_reminder.refresh_assignments", return_value=[]), \
             patch("yonstudy.deadline_reminder.send_report", side_effect=[RuntimeError("SMTP down"), None]):
            self.assertEqual(self.run_reminder()[1]["status"], "error")
            self.assertEqual(self.run_reminder()[1]["status"], "sent")

    def test_preview_does_not_send_or_mark_sent(self):
        self.assignment()
        with patch("yonstudy.deadline_reminder.refresh_assignments", return_value=[]), \
             patch("yonstudy.deadline_reminder.send_report") as send:
            self.assertEqual(self.run_reminder(dry_run=True)[1]["status"], "dry_run")
            self.assertFalse((Path(self.tmp.name) / "deadline_reminder_state.json").exists())
            send.assert_not_called()

    def test_previous_day_send_does_not_suppress_today(self):
        self.assignment()
        self.store.write_json("deadline_reminder_state.json", {"sent_date": "2026-09-15"})
        with patch("yonstudy.deadline_reminder.refresh_assignments", return_value=[]), \
             patch("yonstudy.deadline_reminder.send_report") as send:
            self.assertEqual(self.run_reminder()[1]["status"], "sent")
            send.assert_called_once()
