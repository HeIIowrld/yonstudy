import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.run_job import job_environment


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


if __name__ == "__main__":
    unittest.main()
