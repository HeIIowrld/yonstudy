import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from yonstudy.automation import run_scheduled_watch
from yonstudy.store import Store


class ScheduledWatchTests(unittest.TestCase):
    def test_refresh_that_closes_period_finishes_as_nothing_to_watch(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course(
                {
                    "course_id": 1, "year": "2026", "semester": "2학기",
                    "name": "테스트", "title": "테스트", "slug": "TST_테스트",
                }
            )
            store.commit()
            course = SimpleNamespace(
                course_id=1, year="2026", semester="2학기", title="테스트"
            )
            initial_job = SimpleNamespace(course_id=1)
            fake_client = MagicMock()
            fake_client.session_info.return_value = (True, "session")
            fake_archiver = MagicMock()
            fake_archiver.sync_courses.return_value = [course]

            with (
                patch("yonstudy.automation.current_term", return_value=("2026", "2학기")),
                patch("yonstudy.automation.LearnUsClient", return_value=fake_client),
                patch("yonstudy.automation.Archiver", return_value=fake_archiver),
                patch("yonstudy.autoplay.build_plan", side_effect=[[initial_job], []]),
                patch("yonstudy.autoplay.run_plan") as run_plan,
            ):
                code, state = run_scheduled_watch(
                    store_path=root, cookie_path="unused", limit=1
                )

            self.assertEqual(code, 0)
            self.assertEqual(state["status"], "nothing_to_watch")
            run_plan.assert_not_called()

    def test_untracked_video_is_verified_from_local_playback_record(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course(
                {
                    "course_id": 1, "year": "2026", "semester": "2학기",
                    "name": "컴퓨터비젼", "title": "컴퓨터비젼", "slug": "CV",
                }
            )
            store.save_activity(
                {
                    "cmid": 20, "course_id": 1, "modname": "vod",
                    "title": "진도 없는 강의", "completion": None,
                    "restricted": 0,
                }
            )
            store.save_vod(
                {
                    "cmid": 20, "course_id": 1, "duration_sec": 600,
                    "is_progress": 0, "status": "ok",
                }
            )
            store.commit()
            course = SimpleNamespace(
                course_id=1, year="2026", semester="2학기", title="컴퓨터비젼"
            )
            job = SimpleNamespace(
                cmid=20, course_id=1, course_name="컴퓨터비젼",
                title="진도 없는 강의", remaining_sec=600, rate=2.0,
                open_to=None, tracks_progress=False,
            )
            fake_client = MagicMock()
            fake_client.session_info.return_value = (True, "session")
            fake_archiver = MagicMock()
            fake_archiver.sync_courses.return_value = [course]

            def complete_playback(selected, cookie_path, on_result=None):
                on_result(selected[0], True, "ended")
                return 0

            with (
                patch("yonstudy.automation.current_term", return_value=("2026", "2학기")),
                patch("yonstudy.automation.LearnUsClient", return_value=fake_client),
                patch("yonstudy.automation.Archiver", return_value=fake_archiver),
                patch("yonstudy.autoplay.build_plan", side_effect=[[job], [job]]),
                patch("yonstudy.autoplay.run_plan", side_effect=complete_playback),
            ):
                code, state = run_scheduled_watch(
                    store_path=root, cookie_path="unused", limit=1
                )

            self.assertEqual(code, 0)
            self.assertEqual(state["status"], "verified")
            self.assertEqual(
                state["verification"][0]["source"], "local_playback_once"
            )
            log = store.query(
                "SELECT ok FROM crawl_log WHERE kind='playback_once' AND ref='20'"
            )
            self.assertEqual(log[0]["ok"], 1)

    def test_tracked_video_requires_progress_and_max_position(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course(
                {
                    "course_id": 1, "year": "2026", "semester": "2학기",
                    "name": "컴퓨터비젼", "title": "컴퓨터비젼", "slug": "CV",
                }
            )
            store.save_activity(
                {
                    "cmid": 30, "course_id": 1, "modname": "vod",
                    "title": "부분 시청 강의", "completion": "n", "restricted": 0,
                }
            )
            store.save_vod(
                {
                    "cmid": 30, "course_id": 1, "duration_sec": 600,
                    "watched_sec": 300, "progress_pct": 100,
                    "is_progress": 1, "status": "ok",
                }
            )
            store.commit()
            course = SimpleNamespace(
                course_id=1, year="2026", semester="2학기", title="컴퓨터비젼"
            )
            job = SimpleNamespace(
                cmid=30, course_id=1, course_name="컴퓨터비젼",
                title="부분 시청 강의", remaining_sec=300, rate=2.0,
                open_to=None, tracks_progress=True,
            )
            fake_client = MagicMock()
            fake_client.session_info.return_value = (True, "session")
            fake_archiver = MagicMock()
            fake_archiver.sync_courses.return_value = [course]

            def complete_playback(selected, cookie_path, on_result=None):
                on_result(selected[0], True, "ended")
                return 0

            with (
                patch("yonstudy.automation.current_term", return_value=("2026", "2학기")),
                patch("yonstudy.automation.LearnUsClient", return_value=fake_client),
                patch("yonstudy.automation.Archiver", return_value=fake_archiver),
                patch("yonstudy.autoplay.build_plan", side_effect=[[job], [job]]),
                patch("yonstudy.autoplay.run_plan", side_effect=complete_playback),
            ):
                code, state = run_scheduled_watch(
                    store_path=root, cookie_path="unused", limit=1
                )

            self.assertEqual(code, 1)
            self.assertEqual(state["status"], "verification_failed")
            self.assertFalse(state["verification"][0]["ok"])


if __name__ == "__main__":
    unittest.main()
