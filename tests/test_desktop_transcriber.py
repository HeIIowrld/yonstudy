import importlib.util
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "desktop" / "강의_자막_생성.py"
SPEC = importlib.util.spec_from_file_location("desktop_transcriber", SCRIPT)
desktop = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = desktop
SPEC.loader.exec_module(desktop)


class DesktopTranscriberTests(unittest.TestCase):
    def _old_file(self, path: Path, body: bytes = b"media") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        os.utime(path, (100.0, 100.0))

    def test_defaults_to_quantized_medium_with_automatic_language_detection(self):
        self.assertEqual(desktop.MODEL, "medium-q5_0")
        self.assertEqual(desktop.LANGUAGE, "auto")

    def test_backend_order_prefers_cuda_then_vulkan_then_cpu(self):
        self.assertEqual(
            desktop.backend_order("auto", has_nvidia=True, has_vulkan=True),
            ["cuda", "vulkan", "cpu"],
        )
        self.assertEqual(
            desktop.backend_order("auto", has_nvidia=False, has_vulkan=True),
            ["vulkan", "cpu"],
        )
        self.assertEqual(
            desktop.backend_order("auto", has_nvidia=False, has_vulkan=False),
            ["cpu"],
        )

    def test_explicit_backend_does_not_add_fallbacks(self):
        self.assertEqual(desktop.backend_order("vulkan"), ["vulkan"])

    def test_scan_uses_script_style_sibling_subtitles_as_state(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            missing = root / "과목A" / "lecture-1.mp4"
            complete = root / "과목B" / "lecture-2.mp4"
            self._old_file(missing)
            self._old_file(complete)
            (complete.parent / "lecture-2.en.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\ntext\n",
                encoding="utf-8",
            )

            candidates, counts = desktop.scan(
                root, force=False, stable_seconds=0
            )

            self.assertEqual([row.media for row in candidates], [missing])
            self.assertEqual(counts["media"], 2)
            self.assertEqual(counts["existing"], 1)

    def test_detected_language_is_used_for_atomic_output(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            media = root / "lecture.mp4"
            self._old_file(media)
            before = media.stat()
            cues = [desktop.Cue(0.0, 1.5, "Hello, class.")]

            output = desktop.write_subtitle(
                media, cues, "en", before, force=False
            )

            self.assertEqual(output, root / "lecture.en.srt")
            self.assertTrue(output.is_file())
            self.assertEqual(list(root.glob("*.part-*")), [])

    def test_parse_whisper_json_uses_detected_language_and_millisecond_offsets(self):
        with tempfile.TemporaryDirectory() as root_name:
            result = Path(root_name) / "result.json"
            result.write_text(
                json.dumps(
                    {
                        "result": {"language": "EN"},
                        "transcription": [
                            {
                                "offsets": {"from": 1250, "to": 3500},
                                "text": " Hello, class. ",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            cues, language = desktop.parse_whisper_json(result)

            self.assertEqual(language, "en")
            self.assertEqual(cues, [desktop.Cue(1.25, 3.5, "Hello, class.")])

    def test_safe_extract_rejects_parent_path(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            archive = root / "runtime.zip"
            destination = root / "runtime"
            destination.mkdir()
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../outside.exe", b"unsafe")

            with self.assertRaises(RuntimeError):
                desktop._safe_extract(archive, destination)

            self.assertFalse((root / "outside.exe").exists())


if __name__ == "__main__":
    unittest.main()
