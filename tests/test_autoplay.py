import tempfile
import unittest
from datetime import datetime, timezone

from yonstudy.autoplay import build_plan
from yonstudy.store import Store


class AutoplayPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        for course_id in (1, 2):
            self.store.save_course(
                {
                    "course_id": course_id,
                    "year": "2026",
                    "semester": "2학기",
                    "name": f"과목{course_id}",
                    "title": f"과목{course_id}",
                }
            )

    def tearDown(self):
        self.tmp.cleanup()

    def add_vod(self, cmid, course_id, open_from, open_to, late_until=None):
        self.store.save_activity(
            {
                "cmid": cmid,
                "course_id": course_id,
                "modname": "vod",
                "title": f"강의{cmid}",
                "url": f"https://example.test/vod/{cmid}",
                "completion": "n",
                "open_from": open_from,
                "open_to": open_to,
                "late_until": late_until,
                "restricted": 0,
            }
        )
        self.store.save_vod(
            {
                "cmid": cmid,
                "course_id": course_id,
                "duration_sec": 600,
                "watched_sec": 0,
                "progress_pct": 0,
                "can_log_progress": 1,
                "max_rate": 2.0,
            }
        )
        self.store.commit()

    def test_aware_utc_now_is_compared_as_seoul_time(self):
        self.add_vod(
            10,
            1,
            "2026-09-02 00:00:00",
            "2026-09-02 23:59:59",
        )
        now = datetime(2026, 9, 1, 16, 0, tzinfo=timezone.utc)  # 09-02 01:00 KST
        self.assertEqual([job.cmid for job in build_plan(self.store, now=now)], [10])

    def test_course_filter_and_normal_deadline_guard(self):
        self.add_vod(
            10,
            1,
            "2026-09-01 00:00:00",
            "2026-09-01 23:59:59",
            "2026-09-07 23:59:59",
        )
        self.add_vod(
            20,
            2,
            "2026-09-01 00:00:00",
            "2026-09-07 23:59:59",
        )
        now = datetime(2026, 9, 2, 12, 0)
        self.assertEqual(
            [job.cmid for job in build_plan(self.store, now=now, course_ids={1})],
            [],
        )
        self.assertEqual(
            [job.cmid for job in build_plan(
                self.store, now=now, course_ids={1}, include_late=True
            )],
            [10],
        )
        self.assertEqual(
            [job.cmid for job in build_plan(self.store, now=now, course_ids={2})],
            [20],
        )

    def test_future_video_is_not_selected(self):
        self.add_vod(
            30,
            1,
            "2026-11-10 00:00:00",
            "2026-11-16 23:59:59",
        )
        now = datetime(2026, 9, 3, 9, 0)
        self.assertEqual(build_plan(self.store, now=now), [])

    def test_untracked_video_is_played_once_and_recorded_locally(self):
        self.store.save_activity(
            {
                "cmid": 40,
                "course_id": 1,
                "modname": "vod",
                "title": "진도 표시 없는 강의",
                "url": "https://example.test/vod/40",
                "completion": None,
                "open_from": None,
                "open_to": None,
                "restricted": 0,
            }
        )
        self.store.save_vod(
            {
                "cmid": 40,
                "course_id": 1,
                "duration_sec": 600,
                "watched_sec": None,
                "progress_pct": None,
                "is_progress": 0,
                "can_log_progress": 0,
                "max_rate": 2.0,
                "status": "ok",
            }
        )
        self.store.commit()

        self.assertEqual(build_plan(self.store), [])
        plan = build_plan(self.store, include_untracked_once=True)
        self.assertEqual([job.cmid for job in plan], [40])
        self.assertFalse(plan[0].tracks_progress)

        self.store.log("playback_once", "40", True, "ended")
        self.store.commit()
        self.assertEqual(
            build_plan(self.store, include_untracked_once=True),
            [],
        )


if __name__ == "__main__":
    unittest.main()
