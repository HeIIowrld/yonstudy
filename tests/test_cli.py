import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cli
from yonstudy.archive import SessionExpired
from yonstudy.autoplay import Job
from yonstudy.store import Store


class TranscriptionReviewCommandTests(unittest.TestCase):
    def test_dry_run_reviews_without_model_or_learnus_and_preserves_subtitle(self):
        with tempfile.TemporaryDirectory() as root:
            media = Path(root) / "media"
            media.mkdir()
            (media / "lecture.m4a").write_bytes(b"recording")
            subtitle = media / "lecture.en.srt"
            subtitle.write_text("broken subtitle", encoding="utf-8")
            state = Path(root) / "state"
            with (
                patch("sys.argv", ["yonstudy", "transcribe-review", str(media),
                                    "--state-dir", str(state), "--dry-run", "--stable-seconds", "0"]),
                patch("yonstudy.transcription_worker.probe_duration", return_value=300),
                patch("yonstudy.transcription_worker.FasterWhisperTranscriber") as backend,
                patch("cli.get_client") as client,
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                self.assertEqual(cli.main(), 0)
            backend.assert_not_called()
            client.assert_not_called()
            self.assertIn('"suspect_files": 1', out.getvalue())
            self.assertEqual(subtitle.read_text(), "broken subtitle")
            self.assertTrue((state / "status.md").is_file())


class AssignmentStatusCommandTests(unittest.TestCase):
    def test_set_list_and_reset_requirement_without_connecting_to_learnus(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({"course_id": 1, "name": "AI비즈니스", "year": "2026", "semester": "2학기"})
            store.save_activity({"cmid": 10, "course_id": 1, "modname": "assign", "title": "팀과제"})
            store.save_submission({"cmid": 10, "course_id": 1, "modname": "assign", "submitted": 0, "status": "제출 안 함"})
            store.commit()
            args = SimpleNamespace(store=root, cmid=10, requirement="not_required", reason="대표자 제출", year=None, semester=None, all_terms=False)
            with patch("cli.get_client") as client, contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.cmd_assignment_status(args), 0)
                self.assertIn("본인 제출 불필요", out.getvalue())
                self.assertIn("사이트 기록: 제출 안 함", out.getvalue())
                args.requirement = None
                args.reason = None
                self.assertEqual(cli.cmd_assignment_status(args), 0)
                args.requirement = "auto"
                self.assertEqual(cli.cmd_assignment_status(args), 0)
                client.assert_not_called()
            self.assertEqual(store.query("SELECT * FROM assignment_preference"), [])

    def test_changing_all_assignments_accidentally_is_rejected(self):
        args = SimpleNamespace(cmid=None, requirement="not_required", reason=None)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_assignment_status(args), 2)

    def test_submission_without_activity_is_reported_after_setting(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_submission({"cmid": 10, "course_id": 1, "modname": "assign", "title": "Imported task", "submitted": 0})
            store.commit()
            args = SimpleNamespace(store=root, cmid=10, requirement="not_required", reason="대표자 제출", year=None, semester=None, all_terms=False)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.cmd_assignment_status(args), 0)
            self.assertIn("Imported task", out.getvalue())
            self.assertIn("본인 제출 불필요", out.getvalue())


class WatchCommandTests(unittest.TestCase):
    def test_watch_refreshes_server_progress_after_playback(self):
        with tempfile.TemporaryDirectory() as root:
            seed = Store(root)
            seed.save_course(
                {
                    "course_id": 1,
                    "year": "2026",
                    "semester": "2학기",
                    "name": "네트워크최적화",
                    "title": "네트워크최적화",
                    "slug": "NET",
                }
            )
            seed.save_activity(
                {
                    "cmid": 10,
                    "course_id": 1,
                    "modname": "vod",
                    "title": "1주차",
                    "completion": "n",
                    "restricted": 0,
                }
            )
            seed.save_vod(
                {
                    "cmid": 10,
                    "course_id": 1,
                    "duration_sec": 600,
                    "watched_sec": 0,
                    "progress_pct": 0,
                    "is_progress": 1,
                    "status": "ok",
                }
            )
            seed.commit()

            job = Job(
                cmid=10,
                course_id=1,
                course_name="네트워크최적화",
                title="1주차",
                duration_sec=600,
                watched_sec=0,
                open_from=None,
                open_to=None,
                late_until=None,
                rate=2.0,
                urgency="상시",
                deadline=None,
            )
            course = SimpleNamespace(
                course_id=1,
                year="2026",
                semester="2학기",
                title="네트워크최적화",
            )
            fake_archiver = MagicMock()
            fake_archiver.sync_courses.return_value = [course]

            def refresh_progress(*_args, **_kwargs):
                seed.save_vod(
                    {
                        "cmid": 10,
                        "course_id": 1,
                        "progress_pct": 100,
                        "watched_sec": 600,
                    }
                )
                seed.commit()

            fake_archiver.sync_course.side_effect = refresh_progress

            def finish_playback(selected, _cookies, **kwargs):
                kwargs["on_result"](selected[0], True, "ended")
                return 0

            args = SimpleNamespace(
                store=root,
                cookies="unused",
                interval=0.0,
                per_minute=100,
                limit=None,
                dry_run=False,
                rate=None,
            )
            with (
                patch("yonstudy.daily.current_term", return_value=("2026", "2학기")),
                patch("yonstudy.autoplay.build_plan", return_value=[job]),
                patch("yonstudy.autoplay.run_plan", side_effect=finish_playback),
                patch("cli.get_client", return_value=MagicMock()),
                patch("cli.Archiver", return_value=fake_archiver),
            ):
                code = cli.cmd_watch(args)

            self.assertEqual(code, 0)
            fake_archiver.sync_course.assert_called_once()
            progress = seed.query("SELECT progress_pct FROM vod WHERE cmid=10")
            self.assertEqual(progress[0]["progress_pct"], 100)


class ArchiveCommandTests(unittest.TestCase):
    def test_course_failure_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as root:
            course = SimpleNamespace(
                course_id=1,
                year="2026",
                semester="2학기",
                title="테스트",
            )
            archiver = MagicMock()
            archiver.sync_courses.return_value = [course]
            archiver.sync_course.side_effect = RuntimeError("server failure")
            args = SimpleNamespace(
                store=root,
                year=None,
                semester=None,
                course=None,
                limit=None,
                no_vod=False,
                no_subtitles=False,
                no_files=False,
                no_boards=False,
                board_pages=1,
            )

            with (
                patch("cli.get_client", return_value=MagicMock()),
                patch("cli.Archiver", return_value=archiver),
            ):
                code = cli.cmd_archive(args)

            self.assertEqual(code, 1)

    def test_session_expiry_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as root:
            course = SimpleNamespace(
                course_id=1,
                year="2026",
                semester="2학기",
                title="테스트",
            )
            archiver = MagicMock()
            archiver.sync_courses.return_value = [course]
            archiver.sync_course.side_effect = SessionExpired("expired")
            args = SimpleNamespace(
                store=root,
                year=None,
                semester=None,
                course=None,
                limit=None,
                no_vod=False,
                no_subtitles=False,
                no_files=False,
                no_boards=False,
                board_pages=1,
            )

            with (
                patch("cli.get_client", return_value=MagicMock()),
                patch("cli.Archiver", return_value=archiver),
            ):
                code = cli.cmd_archive(args)

            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
