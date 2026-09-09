import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from deploy.install_desktop_script import (
    SCRIPT_NAME,
    configured_destination,
    current_semester,
    install_script,
    main,
)


KST = ZoneInfo("Asia/Seoul")


class DesktopScriptDeployTests(unittest.TestCase):
    def test_current_semester_handles_winter_as_previous_second_semester(self):
        self.assertEqual(current_semester(datetime(2027, 2, 1, tzinfo=KST)), "2026-2")
        self.assertEqual(current_semester(datetime(2027, 3, 1, tzinfo=KST)), "2027-1")
        self.assertEqual(current_semester(datetime(2027, 9, 1, tzinfo=KST)), "2027-2")

    def test_configured_destination_requires_an_absolute_container_path(self):
        self.assertIsNone(configured_destination("  "))
        with self.assertRaises(ValueError):
            configured_destination("2026-2")

    def test_relative_command_line_destination_fails(self):
        with tempfile.TemporaryDirectory() as root_name:
            source = Path(root_name) / "source.py"
            source.write_text("pass\n", encoding="utf-8")

            self.assertEqual(
                main(["--source", str(source), "--destination", "2026-2"]),
                1,
            )

    def test_install_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            source = root / "source.py"
            destination = root / "2026-2"
            destination.mkdir()
            source.write_text("print('first')\n", encoding="utf-8")

            target, changed = install_script(source, destination)
            first_mtime = target.stat().st_mtime_ns
            self.assertTrue(changed)
            self.assertEqual(target.name, SCRIPT_NAME)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(list(destination.glob(f".{SCRIPT_NAME}.*.tmp")), [])

            target, changed = install_script(source, destination)
            self.assertFalse(changed)
            self.assertEqual(target.stat().st_mtime_ns, first_mtime)

            source.write_text("print('second')\n", encoding="utf-8")
            target, changed = install_script(source, destination)
            self.assertTrue(changed)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(list(destination.glob(f".{SCRIPT_NAME}.*.tmp")), [])

    def test_explicit_missing_destination_fails(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            source = root / "source.py"
            source.write_text("pass\n", encoding="utf-8")
            missing = root / "missing"

            with patch.dict(
                os.environ,
                {"YONSTUDY_DESKTOP_SCRIPT_DIR": str(missing)},
                clear=False,
            ):
                self.assertEqual(main(["--source", str(source)]), 1)

    def test_missing_automatic_semester_is_a_safe_noop(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            source = root / "source.py"
            source.write_text("pass\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                result = main(
                    ["--source", str(source), "--archive-root", str(root)]
                )

            self.assertEqual(result, 0)
            self.assertEqual(list(root.rglob(SCRIPT_NAME)), [])


if __name__ == "__main__":
    unittest.main()
