import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from yonstudy.store import Store
from yonstudy.video_archive import archive_course_vods, candidates


class FakeSink:
    def __init__(self):
        self.files = {}

    def video_path(self, **values):
        return f"2026-2/{values['course_slug']}/{values['cmid']}.mp4"

    def exists(self, relative, size=None):
        return relative in self.files and (
            size is None or len(self.files[relative]) == size
        )

    def upload_file(self, relative, source, force=False):
        self.files[relative] = Path(source).read_bytes()
        return True


class CourseVideoArchiveTests(unittest.TestCase):
    def test_withdrawn_course_video_is_not_an_archive_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({
                "course_id": 1, "year": "2026", "semester": "2학기",
                "name": "철회 강좌", "title": "철회 강좌", "slug": "OLD",
                "enrolled": 0,
            })
            store.save_activity({
                "cmid": 10, "course_id": 1, "modname": "vod",
                "title": "옛 영상", "url": "https://example.test/vod/10",
                "restricted": 0,
            })
            store.save_vod({
                "cmid": 10, "course_id": 1,
                "hls_url": "https://cdn/10.m3u8",
                "is_progress": 0, "status": "ok",
            })
            store.commit()

            self.assertEqual(
                candidates(store, year="2026", semester="2학기"),
                [],
            )

    def test_tracked_and_untracked_videos_are_archived_once(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({
                "course_id": 1, "year": "2026", "semester": "2학기",
                "name": "컴퓨터비젼", "title": "컴퓨터비젼",
                "slug": "CAS3116_컴퓨터비젼",
            })
            for cmid, is_progress in ((10, 0), (11, 1)):
                store.save_activity({
                    "cmid": cmid, "course_id": 1, "modname": "vod",
                    "title": f"Lec {cmid}",
                    "url": f"https://example.test/vod/{cmid}",
                    "section_idx": 1, "section_name": "1주차",
                    "duration": "15:12", "restricted": 0,
                })
                store.save_vod({
                    "cmid": cmid, "course_id": 1,
                    "hls_url": f"https://cdn/{cmid}.m3u8",
                    "is_progress": is_progress, "status": "ok",
                })
            store.commit()
            sink = FakeSink()
            downloads = []

            def fake_download(url, outputs, cmid=0):
                downloads.append(cmid)
                outputs.video.write_bytes(b"private lecture video")
                return SimpleNamespace(ok=True, error="")

            first = archive_course_vods(
                store, sink, year="2026", semester="2학기",
                download_fn=fake_download,
            )
            second = archive_course_vods(
                store, sink, year="2026", semester="2학기",
                download_fn=fake_download,
            )

            self.assertEqual(downloads, [10, 11])
            self.assertEqual(first.uploaded_files, 2)
            self.assertEqual(second.skipped_files, 2)
            for cmid in (10, 11):
                record = store.file_record(
                    f"https://example.test/vod/{cmid}", "video"
                )
                self.assertEqual(record["remote_status"], "ok")
                self.assertEqual(record["bytes"], len(b"private lecture video"))
            self.assertEqual(list((Path(root) / "tmp").glob("vod-*")), [])
            duration = store.query("SELECT duration_sec FROM vod WHERE cmid=10")[0]
            self.assertEqual(duration["duration_sec"], 15 * 60 + 12)


if __name__ == "__main__":
    unittest.main()
