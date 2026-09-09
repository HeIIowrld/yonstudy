import os
import tempfile
import unittest
from pathlib import Path

from yonstudy.transcribe import (
    SubtitleSegment,
    existing_subtitle,
    render_srt,
    scan_directory,
    target_subtitle,
    transcribe_directory,
)


class FakeTranscriber:
    def __init__(self, *, mutate_source: bool = False):
        self.calls: list[Path] = []
        self.mutate_source = mutate_source

    def transcribe(self, media, **_kwargs):
        self.calls.append(media)
        if self.mutate_source:
            media.write_bytes(media.read_bytes() + b"more")
        return (
            [
                SubtitleSegment(0.0, 1.25, "안녕하세요."),
                SubtitleSegment(61.002, 62.9996, "두 번째 구간입니다."),
            ],
            "ko",
        )


class FolderTranscriptionTests(unittest.TestCase):
    def _old_file(self, path: Path, body: bytes = b"media") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        os.utime(path, (100.0, 100.0))

    def test_scans_recursively_and_skips_any_paired_subtitle(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            missing = root / "과목" / "W01-L01__강의영상.mp4"
            existing = root / "과목" / "W01-L02__강의영상.mkv"
            fresh = root / "W01-L03__강의영상.webm"
            self._old_file(missing)
            self._old_file(existing)
            self._old_file(fresh)
            (existing.parent / f"{existing.stem}.en.vtt").write_text(
                "WEBVTT\n", encoding="utf-8"
            )
            os.utime(fresh, (995.0, 995.0))

            scan = scan_directory(root, now=1000.0, stable_seconds=120)

            self.assertEqual(scan.media_files, 3)
            self.assertEqual(scan.existing_subtitles, 1)
            self.assertEqual(scan.unstable_files, 1)
            self.assertEqual([row.media for row in scan.candidates], [missing])
            self.assertEqual(
                scan.candidates[0].subtitle,
                missing.with_name(f"{missing.stem}.ko.srt"),
            )

    def test_generates_utf8_srt_next_to_video(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            media = root / "W01-L00__1주차 동영상 강의__cmid4532837.mp4"
            self._old_file(media)
            fake = FakeTranscriber()

            result = transcribe_directory(
                root, stable_seconds=0, transcriber=fake
            )

            output = target_subtitle(media)
            self.assertEqual(result.completed_files, 1)
            self.assertEqual(fake.calls, [media])
            self.assertEqual(existing_subtitle(media), output)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "1\n00:00:00,000 --> 00:00:01,250\n안녕하세요.\n\n"
                "2\n00:01:01,002 --> 00:01:03,000\n두 번째 구간입니다.\n",
            )
            self.assertEqual(list(root.glob("*.part-*")), [])

    def test_auto_language_uses_detected_language_in_filename(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            media = root / "English lecture.mp4"
            self._old_file(media)
            fake = FakeTranscriber()

            result = transcribe_directory(
                root,
                language=None,
                stable_seconds=0,
                transcriber=fake,
            )

            self.assertEqual(result.completed_files, 1)
            self.assertTrue((root / "English lecture.ko.srt").is_file())
            self.assertEqual(
                result.items[0]["subtitle"],
                str(root / "English lecture.ko.srt"),
            )

    def test_source_change_discards_temporary_subtitle(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            media = root / "lecture.mp4"
            self._old_file(media)

            result = transcribe_directory(
                root,
                stable_seconds=0,
                transcriber=FakeTranscriber(mutate_source=True),
            )

            self.assertEqual(result.changed_during_run, 1)
            self.assertEqual(result.completed_files, 0)
            self.assertFalse(target_subtitle(media).exists())

    def test_dry_run_never_loads_transcription_backend(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            media = root / "lecture.m4a"
            self._old_file(media)

            result = transcribe_directory(root, stable_seconds=0, dry_run=True)

            self.assertEqual(result.selected_files, 1)
            self.assertEqual(result.items[0]["status"], "dry-run")
            self.assertFalse(target_subtitle(media).exists())

    def test_render_srt_skips_blank_text(self):
        rendered = render_srt(
            [
                SubtitleSegment(0, 1, "  "),
                SubtitleSegment(2, 3, "내용"),
            ]
        )

        self.assertEqual(rendered, "1\n00:00:02,000 --> 00:00:03,000\n내용\n")

    def test_language_tag_cannot_escape_output_directory(self):
        with self.assertRaises(ValueError):
            target_subtitle(Path("lecture.mp4"), "../ko")


if __name__ == "__main__":
    unittest.main()
