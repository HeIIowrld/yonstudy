import importlib.util
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch


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

    def test_runtime_python_uses_real_redirected_path(self):
        runtime = Path("C:/Users/test/AppData/Local/yonstudy-transcriber")
        actual = Path("C:/Users/test/AppData/Local/Packages/Python/LocalCache/python.exe")
        with (
            patch.object(desktop.sys, "platform", "win32"),
            patch.object(desktop.os.path, "realpath", return_value=str(actual)) as realpath,
        ):
            result = desktop.runtime_python(runtime)

        self.assertEqual(result, actual)
        requested = str(realpath.call_args.args[0]).replace("\\", "/")
        self.assertTrue(requested.endswith("/venv/Scripts/python.exe"))

    def test_runtime_layout_requires_pyvenv_cfg(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            python = root / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"python")

            self.assertFalse(desktop._runtime_layout_valid(python))
            (python.parent.parent / "pyvenv.cfg").write_text("home=x\n", encoding="utf-8")
            self.assertTrue(desktop._runtime_layout_valid(python))

    def test_damaged_runtime_is_recreated_and_actual_path_rechecked(self):
        runtime = Path("runtime")
        damaged = Path("redirected/old/bin/python")
        repaired = Path("redirected/new/bin/python")
        builder = Mock()
        with (
            patch.object(desktop, "runtime_python", side_effect=[damaged, repaired]),
            patch.object(desktop, "_python_runtime_valid", side_effect=[False, True]),
            patch.object(desktop.venv, "EnvBuilder", return_value=builder) as env_builder,
            patch.object(desktop.Path, "exists", return_value=True),
            patch.object(desktop.Path, "mkdir"),
        ):
            result = desktop.ensure_python_runtime(runtime)

        self.assertEqual(result, repaired)
        env_builder.assert_called_once_with(with_pip=True, clear=True)
        builder.create.assert_called_once_with(runtime / "venv")

    def test_valid_runtime_is_reused(self):
        runtime = Path("runtime")
        python = Path("redirected/venv/bin/python")
        with (
            patch.object(desktop, "runtime_python", return_value=python),
            patch.object(desktop, "_python_runtime_valid", return_value=True),
            patch.object(desktop.venv, "EnvBuilder") as env_builder,
        ):
            result = desktop.ensure_python_runtime(runtime)

        self.assertEqual(result, python)
        env_builder.assert_not_called()

    def test_semester_lock_rejects_second_process(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            runtime = root / "runtime"
            semester = root / "2026-2"
            semester.mkdir()

            with desktop.semester_lock(runtime, semester):
                with self.assertRaises(desktop.AlreadyRunningError):
                    with desktop.semester_lock(runtime, semester):
                        pass

    def test_session_log_mirrors_output(self):
        with tempfile.TemporaryDirectory() as root_name:
            runtime = Path(root_name)
            with desktop.session_log(runtime) as path:
                print("진단 메시지")

            self.assertIsNotNone(path)
            self.assertIn("진단 메시지", path.read_text(encoding="utf-8"))

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

    def test_incomplete_cached_runtime_is_reinstalled_with_required_dlls(self):
        with tempfile.TemporaryDirectory() as root_name:
            runtime = Path(root_name)
            target = runtime / "runtimes" / "cpu-test"
            target.mkdir(parents=True)
            (target / "whisper-cli.exe").write_bytes(b"stale executable")
            checksum = "a" * 64
            (target / ".archive.sha256").write_text(checksum + "\n", encoding="ascii")

            archive = runtime / "fresh.zip"
            required = desktop._runtime_required_files("cpu-test")
            with zipfile.ZipFile(archive, "w") as output:
                for filename in required:
                    output.writestr(f"Release/{filename}", filename.encode())
            spec = desktop.ArchiveSpec(
                "cpu-test", "fresh.zip", "https://example.test/fresh.zip", checksum
            )

            with patch.object(desktop, "download", return_value=archive):
                executable = desktop.install_runtime(runtime, spec)

            self.assertEqual(executable, target / "whisper-cli.exe")
            self.assertEqual(desktop._runtime_missing_files(target, spec.name), [])
            self.assertEqual(executable.read_bytes(), b"whisper-cli.exe")


if __name__ == "__main__":
    unittest.main()
