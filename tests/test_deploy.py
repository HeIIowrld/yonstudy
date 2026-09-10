import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.run_job import WATCH_JITTER_SECONDS, job_environment, scheduled_delay_seconds


class ScheduledJobEnvironmentTests(unittest.TestCase):
    def test_bundled_playwright_path_is_restored_for_clean_cron_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "yonstudy.env"
            config.write_text("TZ=Asia/Seoul\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                env = job_environment(config)

        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], "/ms-playwright")
        self.assertEqual(env["YONSTUDY_BROWSER_CHANNEL"], "chrome")

    def test_explicit_browser_settings_override_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "yonstudy.env"
            config.write_text(
                "PLAYWRIGHT_BROWSERS_PATH=/custom/browser-cache\n"
                "YONSTUDY_BROWSER_CHANNEL=chromium\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                env = job_environment(config)

        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], "/custom/browser-cache")
        self.assertEqual(env["YONSTUDY_BROWSER_CHANNEL"], "chromium")


class ScheduledJobDelayTests(unittest.TestCase):
    def test_watch_uses_delay_within_configured_window(self):
        with patch("deploy.run_job.random.randint", return_value=731) as randint:
            delay = scheduled_delay_seconds("watch")

        self.assertEqual(delay, 731)
        randint.assert_called_once_with(0, WATCH_JITTER_SECONDS)

    def test_other_jobs_are_not_delayed(self):
        with patch("deploy.run_job.random.randint") as randint:
            delay = scheduled_delay_seconds("monitor")

        self.assertEqual(delay, 0)
        randint.assert_not_called()

    def test_watch_delay_can_be_configured(self):
        with patch.dict(os.environ, {"YONSTUDY_WATCH_JITTER_SECONDS": "3600"}):
            with patch("deploy.run_job.random.randint", return_value=1234) as randint:
                delay = scheduled_delay_seconds("watch")

        self.assertEqual(delay, 1234)
        randint.assert_called_once_with(0, 3600)

    def test_invalid_watch_delay_uses_default(self):
        with patch.dict(os.environ, {"YONSTUDY_WATCH_JITTER_SECONDS": "invalid"}):
            with patch("deploy.run_job.random.randint", return_value=321) as randint:
                delay = scheduled_delay_seconds("watch")

        self.assertEqual(delay, 321)
        randint.assert_called_once_with(0, WATCH_JITTER_SECONDS)


if __name__ == "__main__":
    unittest.main()
