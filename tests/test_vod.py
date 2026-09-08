import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from yonstudy.vod import Outputs, download, plan_outputs


class VodOutputTests(unittest.TestCase):
    def test_failed_frame_extraction_does_not_leave_completed_frames(self):
        with tempfile.TemporaryDirectory() as root:
            frames = Path(root) / "frames"

            def fail_after_one_frame(command, **_kwargs):
                pattern = Path(command[-1])
                pattern.parent.mkdir(parents=True, exist_ok=True)
                (pattern.parent / "0001.jpg").write_bytes(b"partial")
                return SimpleNamespace(returncode=1, stderr="network failure")

            with (
                patch("yonstudy.vod.ensure_ffmpeg", return_value="ffmpeg"),
                patch("yonstudy.vod.subprocess.run", side_effect=fail_after_one_frame),
            ):
                result = download("https://cdn.test/video.m3u8", Outputs(frames_dir=frames))

            self.assertFalse(result.ok)
            self.assertFalse(frames.exists())
            pending = plan_outputs(
                {"audio": Path(root) / "a.opus", "frames": frames, "video": Path(root) / "v.mp4"},
                want_audio=False,
                want_frames=True,
                want_video=False,
            )
            self.assertEqual(pending.frames_dir, frames)

    def test_success_without_requested_output_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as root:
            frames = Path(root) / "frames"
            frames.mkdir()
            original = frames / "0001.jpg"
            original.write_bytes(b"existing")

            with (
                patch("yonstudy.vod.ensure_ffmpeg", return_value="ffmpeg"),
                patch(
                    "yonstudy.vod.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stderr=""),
                ),
            ):
                result = download(
                    "https://cdn.test/video.m3u8", Outputs(frames_dir=frames)
                )

            self.assertFalse(result.ok)
            self.assertEqual(original.read_bytes(), b"existing")

    def test_frame_filter_always_selects_the_first_frame(self):
        with tempfile.TemporaryDirectory() as root:
            frames = Path(root) / "frames"
            captured = {}

            def make_first_frame(command, **_kwargs):
                captured["command"] = command
                pattern = Path(command[-1])
                (pattern.parent / "0001.jpg").write_bytes(b"frame")
                return SimpleNamespace(returncode=0, stderr="")

            with (
                patch("yonstudy.vod.ensure_ffmpeg", return_value="ffmpeg"),
                patch("yonstudy.vod.subprocess.run", side_effect=make_first_frame),
            ):
                result = download(
                    "https://cdn.test/video.m3u8", Outputs(frames_dir=frames)
                )

            frame_filter = captured["command"][captured["command"].index("-vf") + 1]
            self.assertTrue(result.ok)
            self.assertIn("isnan(prev_selected_t)+gt(scene", frame_filter)
            self.assertTrue((frames / "0001.jpg").is_file())


if __name__ == "__main__":
    unittest.main()
