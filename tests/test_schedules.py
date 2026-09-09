import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ArchiveScheduleTests(unittest.TestCase):
    def test_systemd_archives_every_six_hours(self):
        timer = (ROOT / "systemd/yonstudy-daily.timer").read_text(encoding="utf-8")

        self.assertEqual(
            [
                line
                for line in timer.splitlines()
                if line.startswith("OnCalendar=")
            ],
            [
                "OnCalendar=*-*-* 00:10:00 Asia/Seoul",
                "OnCalendar=*-*-* 06:10:00 Asia/Seoul",
                "OnCalendar=*-*-* 12:10:00 Asia/Seoul",
                "OnCalendar=*-*-* 18:10:00 Asia/Seoul",
            ],
        )

    def test_container_archives_every_six_hours(self):
        cron = (ROOT / "deploy/yonstudy.cron").read_text(encoding="utf-8")

        self.assertIn("10 */6 * * * root /app/deploy/run-job.sh daily", cron)

    def test_lecture_playback_stays_in_early_morning(self):
        cron = (ROOT / "deploy/yonstudy.cron").read_text(encoding="utf-8")
        timer = (ROOT / "systemd/yonstudy-watch.timer").read_text(encoding="utf-8")

        for schedule in ("30 2", "0 4", "30 5"):
            self.assertIn(f"{schedule} * * * root /app/deploy/run-job.sh watch", cron)
        for clock in ("02:30:00", "04:00:00", "05:30:00"):
            self.assertIn(f"OnCalendar=*-*-* {clock} Asia/Seoul", timer)


if __name__ == "__main__":
    unittest.main()
