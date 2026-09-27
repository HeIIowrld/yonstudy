import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.remote_transcribe_loop import _read_reprocess, pull_archive, push_results


class RemoteTranscriptionTests(unittest.TestCase):
    def test_reprocess_list_ignores_comments_and_blanks(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "paths.txt"
            source.write_text("# small outputs\ncourse/a.m4a\n\ncourse/b.mp4\n", encoding="utf-8")
            self.assertEqual(_read_reprocess(source), {"course/a.m4a", "course/b.mp4"})

    @patch("deploy.remote_transcribe_loop._run")
    def test_archive_pull_is_limited_to_media_and_subtitles(self, run):
        with tempfile.TemporaryDirectory() as root:
            mirror = Path(root) / "mirror"
            pull_archive("nas:semester", mirror)
        args = run.call_args.args
        self.assertEqual(args[:3], ("rclone", "sync", "nas:semester"))
        self.assertEqual(args[3], str(mirror))
        self.assertIn("+ *.m4a", args)
        self.assertIn("+ *.srt", args)
        self.assertIn("- *", args)
        self.assertIn("--delete-after", args)
        self.assertEqual(args[-2:], ("--transfers", "2"))

    @patch("deploy.remote_transcribe_loop.subprocess.run")
    @patch("deploy.remote_transcribe_loop._run")
    def test_only_current_model_outputs_are_published(self, run, listing):
        listing.return_value.stdout = "fresh.m4a\n"
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            archive, state = base / "archive", base / "state"
            archive.mkdir(); state.mkdir()
            (archive / "fresh.en.srt").write_text("new")
            (archive / "old.en.srt").write_text("old")
            (state / "status.md").write_text("status")
            (state / "state.json").write_text(
                '{"items":{"fresh":{"status":"completed","completed_model":"large",'
                '"media":"fresh.m4a","subtitle":"fresh.en.srt"},"old":{"status":"completed",'
                '"completed_model":"small","media":"old.m4a","subtitle":"old.en.srt"}}}'
            )
            push_results("nas:term", "nas:state", archive, state, "large")
            calls = [call.args for call in run.call_args_list]
            self.assertTrue(any("nas:term/fresh.en.srt" in call for call in calls))
            self.assertFalse(any("nas:term/old.en.srt" in call for call in calls))
            self.assertTrue(any("nas:term/전사_현황.md" in call for call in calls))

    @patch("deploy.remote_transcribe_loop.subprocess.run")
    @patch("deploy.remote_transcribe_loop._run")
    def test_moved_media_does_not_republish_subtitle_at_old_path(self, run, listing):
        listing.return_value.stdout = "강의미디어/lecture.m4a\n"
        with tempfile.TemporaryDirectory() as root:
            base = Path(root); archive = base / "archive"; state = base / "state"
            archive.mkdir(); state.mkdir()
            (archive / "lecture.ko.srt").write_text("old", encoding="utf-8")
            (state / "state.json").write_text(
                '{"items":{"old":{"status":"completed","completed_model":"large",'
                '"media":"lecture.m4a","subtitle":"lecture.ko.srt"}}}', encoding="utf-8",
            )
            push_results("nas:term", "nas:state", archive, state, "large")
        self.assertFalse(any("nas:term/lecture.ko.srt" in call.args for call in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
