import tempfile
import unittest
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from yonstudy.assignment_mail import (
    SUBJECT_MARKER, apply_action_message, prepare_assignment_actions, process_assignment_mail,
)
from yonstudy.daily import assignments_due_today, build_daily_report, render_email_text, render_report_html
from yonstudy.deadline_reminder import render_reminder
from yonstudy.store import Store


class AssignmentMailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.save_course({"course_id": 1, "name": "과목", "year": "2026", "semester": "2학기"})
        for cmid in (10, 11):
            self.store.save_activity({"cmid": cmid, "course_id": 1, "modname": "assign", "title": f"과제 {cmid}", "completion": "n", "restricted": 0})
            self.store.save_submission({"cmid": cmid, "course_id": 1, "modname": "assign", "title": f"과제 {cmid}",
                                        "submitted": 0, "due_at": "2026-10-09 23:59"})
        self.store.commit()
        self.env = patch.dict("os.environ", {
            "YONSTUDY_MAIL_ACTIONS": "1", "YONSTUDY_SMTP_HOST": "smtp.gmail.com",
            "YONSTUDY_SMTP_USER": "me@example.test", "YONSTUDY_SMTP_PASSWORD": "test-password",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def report(self):
        return build_daily_report(self.store, target=date(2026, 10, 9))

    def links(self):
        return prepare_assignment_actions(self.store, self.report().semester_assignments,
                                          recipient="Me <me@example.test>")

    def message(self, link, sender="me@example.test"):
        fields = parse_qs(urlsplit(link).query)
        message = EmailMessage()
        message["From"] = sender
        message["To"] = "me@example.test"
        message["Subject"] = fields["subject"][0]
        message.set_content(fields["body"][0])
        return message.as_bytes()

    def test_email_request_changes_only_chosen_assignment_and_can_be_restored(self):
        links = self.links()
        self.assertNotIn("+", links[10])  # mailto uses %20, not form-encoded spaces.
        original = self.message(links[10])
        self.assertEqual(self.store.query("SELECT * FROM assignment_preference"), [])
        self.assertEqual(apply_action_message(self.store, original), {"cmid": 10, "requirement": "not_required"})
        self.assertEqual([r["cmid"] for r in assignments_due_today(self.report())], [11])
        self.assertIsNone(apply_action_message(self.store, original))
        self.store.save_submission({"cmid": 10, "submitted": 0})
        self.store.commit()
        self.assertEqual([r["cmid"] for r in self.report().todos], [11])
        new_links = self.links()
        html = render_report_html(self.report(), assignment_actions=new_links)
        self.assertIn("알림 다시 받기", html)
        self.assertEqual(apply_action_message(self.store, self.message(new_links[10])),
                         {"cmid": 10, "requirement": "auto"})
        self.assertEqual({r["cmid"] for r in self.report().todos}, {10, 11})
        self.assertIsNone(apply_action_message(self.store, original))

    def test_unknown_sender_invalid_expired_code_and_auto_reply_are_ignored(self):
        raw = self.message(self.links()[10])
        self.assertIsNone(apply_action_message(self.store, self.message(self.links()[10], "stranger@example.test")))
        self.assertIsNone(apply_action_message(self.store, raw.replace(b"Subject:", b"Auto-Submitted: auto-replied\nSubject:")))
        self.assertIsNone(apply_action_message(self.store, raw.replace(SUBJECT_MARKER.encode(), b"other-marker")))
        self.assertIsNone(apply_action_message(self.store, raw, now=10**12))
        self.assertEqual(self.store.query("SELECT * FROM assignment_preference"), [])

    def test_dry_run_does_not_consume_code_or_change_settings(self):
        raw = self.message(self.links()[10])
        self.assertEqual(apply_action_message(self.store, raw, dry_run=True)["cmid"], 10)
        self.assertEqual(self.store.query("SELECT * FROM assignment_preference"), [])
        self.assertIsNone(self.store.query("SELECT used_at FROM assignment_mail_action")[0]["used_at"])
        self.assertIsNotNone(apply_action_message(self.store, raw))

    def test_both_daily_formats_and_deadline_reminder_offer_actions(self):
        report = self.report()
        links = self.links()
        for body in (render_email_text(report, assignment_actions=links),
                     render_report_html(report, assignment_actions=links),
                     *render_reminder(report.semester_assignments, checked_at="now", errors=[], assignment_actions=links)):
            self.assertIn("mailto:", body)
            self.assertIn("알림 제외", body)
        self.assertNotIn("mailto:", render_email_text(report))

    def test_withdrawn_courses_are_not_offered_actions_or_reported(self):
        self.store.save_course({"course_id": 1, "enrolled": 0})
        self.store.commit()
        report = self.report()
        self.assertEqual(report.semester_assignments, [])
        self.assertEqual(self.links(), {})
        self.assertEqual(assignments_due_today(report), [])

    def test_imap_peeks_only_request_messages_and_retries_failed_fetch(self):
        raw = self.message(self.links()[10])
        client = MagicMock()
        client.__enter__.return_value = client
        client.login.return_value = ("OK", [b"ok"])
        client.select.return_value = ("OK", [b"1"])
        client.response.return_value = ("UIDVALIDITY", [b"123"])
        def uid(command, *args):
            if command == "search":
                return "OK", [b"5"]
            if args[-1] == "(RFC822.SIZE)":
                return "OK", [b"1 (UID 5 RFC822.SIZE 1024)"]
            return "OK", [(b"1 (BODY[] {1024}", raw), b")"]
        client.uid.side_effect = uid
        with patch("yonstudy.assignment_mail.imaplib.IMAP4_SSL", return_value=client):
            preview = process_assignment_mail(self.store, dry_run=True)
            self.assertEqual(preview["applied"], [{"cmid": 10, "requirement": "not_required"}])
            self.assertFalse((Path(self.tmp.name) / "assignment_mail_state.json").exists())
            result = process_assignment_mail(self.store)
            self.assertEqual(result["applied"], preview["applied"])
            self.assertEqual(process_assignment_mail(self.store)["applied"], [])
            client.select.assert_called_with("INBOX", readonly=True)
            self.assertTrue(any(call.args[-1] == "(BODY.PEEK[])" for call in client.uid.call_args_list))
            client.store.assert_not_called()
            client.expunge.assert_not_called()

    def test_failed_imap_fetch_is_retried_without_advancing_cursor(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.login.return_value = client.select.return_value = ("OK", [b"ok"])
        client.response.return_value = ("UIDVALIDITY", [b"123"])
        client.uid.side_effect = [("OK", [b"5"]), ("NO", [b"failed"])]
        with patch("yonstudy.assignment_mail.imaplib.IMAP4_SSL", return_value=client):
            with self.assertRaises(RuntimeError):
                process_assignment_mail(self.store)
        self.assertFalse((Path(self.tmp.name) / "assignment_mail_state.json").exists())


if __name__ == "__main__":
    unittest.main()
