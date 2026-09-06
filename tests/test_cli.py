import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cli
from yonstudy.autoplay import Job
from yonstudy.store import Store


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


if __name__ == "__main__":
    unittest.main()
