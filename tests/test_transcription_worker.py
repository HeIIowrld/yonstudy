import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yonstudy import transcription_worker as worker
from yonstudy.transcribe import SubtitleSegment, render_srt


GOOD = [SubtitleSegment(0, 10, "This is a meaningful lecture sentence about logic circuits.")]
BAD = [SubtitleSegment(0.16, 745.44, "One short sentence.")]


class FakeTranscriber:
    def __init__(self, results=None, callback=None):
        self.results = results or [GOOD]
        self.callback = callback
        self.calls = []

    def transcribe(self, media, **kwargs):
        self.calls.append((media, kwargs))
        if self.callback:
            self.callback(media)
        result = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result, "ko"


class TranscriptionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "semester"
        self.root.mkdir()
        self.state_dir = self.base / "state"

    def media(self, name):
        media = self.root / name
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"recorded audio")
        os.utime(media, (100, 100))
        return media

    def subtitle(self, media, segments=GOOD, suffix="en.srt"):
        path = media.with_name(f"{media.stem}.{suffix}")
        path.write_text(render_srt(segments), encoding="utf-8")
        return path

    def run_worker(self, **kwargs):
        defaults = dict(state_dir=self.state_dir, stable_seconds=0, probe=lambda _: 1000,
                        transcriber=FakeTranscriber(), say=lambda _: None)
        defaults.update(kwargs)
        return worker.run_once(self.root, **defaults)

    def state(self):
        return json.loads((self.state_dir / "state.json").read_text())

    def test_normal_other_language_subtitle_prevents_retranscription(self):
        media = self.media("lecture.mp4")
        good = self.subtitle(media)
        bad = self.subtitle(media, BAD, "ko.srt")
        fake = FakeTranscriber()
        before = good.read_bytes(), bad.read_bytes()
        result = self.run_worker(transcriber=fake, language="ko")
        self.assertEqual(result["ok_files"], 1)
        self.assertEqual(fake.calls, [])
        self.assertEqual((good.read_bytes(), bad.read_bytes()), before)

    def test_queue_prioritizes_suspect_then_recordings_then_videos(self):
        self.media("a-video.mp4")
        self.media("b-recording.m4a")
        suspect = self.media("z-suspect.mp4")
        self.subtitle(suspect, BAD)
        with patch.object(worker, "FasterWhisperTranscriber") as model:
            result = self.run_worker(dry_run=True, transcriber=None)
        self.assertEqual([item["media"] for item in result["items"]],
                         ["z-suspect.mp4", "b-recording.m4a", "a-video.mp4"])
        self.assertEqual(result["pending_files"], 3)
        model.assert_not_called()

    def test_shorter_job_is_first_within_the_same_priority(self):
        self.media("a-long.m4a")
        self.media("z-short.m4a")
        result = self.run_worker(dry_run=True, probe=lambda p: 1000 if "long" in p.name else 90)
        self.assertEqual([item["media"] for item in result["items"]], ["z-short.m4a", "a-long.m4a"])

    def test_model_upgrade_reprocesses_good_subtitle_only_once(self):
        media = self.media("lecture.m4a")
        self.subtitle(media)
        first = self.run_worker(
            reprocess_paths={media.name}, model_id="large-v3-turbo",
        )
        self.assertEqual(first["completed_files"], 1)
        self.assertEqual(self.state()["items"][media.name]["completed_model"], "large-v3-turbo")
        second = self.run_worker(
            reprocess_paths={media.name}, model_id="large-v3-turbo",
        )
        self.assertEqual(second["selected_files"], 0)
        self.assertEqual(second["ok_files"], 1)

    def test_reviewed_replacement_preserves_exact_original_backup(self):
        media = self.media("논회설2-2.m4a")
        old = self.subtitle(media, BAD)
        original = old.read_bytes()
        result = self.run_worker()
        self.assertEqual(result["completed_files"], 1)
        self.assertFalse(old.exists())
        output = media.with_suffix(".ko.srt")
        self.assertEqual(output.read_text(), render_srt(GOOD))
        backups = result["items"][0]["backups"]
        self.assertEqual(len(backups), 1)
        self.assertEqual(Path(backups[0]).read_bytes(), original)
        self.assertEqual(output.stat().st_mode & 0o777, 0o644)
        self.run_worker()
        item = self.state()["items"][media.name]
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["backups"], backups)
        self.assertEqual(item["attempts"], 1)

    def test_suspect_asr_retries_once_without_vad(self):
        self.media("lecture.m4a")
        fake = FakeTranscriber([BAD, GOOD])
        result = self.run_worker(transcriber=fake)
        self.assertEqual([kwargs["vad_filter"] for _, kwargs in fake.calls], [True, False])
        self.assertEqual(result["completed_files"], 1)
        self.assertEqual(result["items"][0]["asr_passes"], 2)

    def test_failed_quality_preserves_original_and_does_not_starve_queue(self):
        media = self.media("a-suspect.m4a")
        old = self.subtitle(media, BAD)
        original = old.read_bytes()
        other = self.media("b-missing.m4a")
        fake = FakeTranscriber([BAD])
        first = self.run_worker(transcriber=fake)
        self.assertEqual(first["failed_files"], 1)
        self.assertEqual(old.read_bytes(), original)
        self.assertEqual(len(fake.calls), 2)
        second_fake = FakeTranscriber()
        second = self.run_worker(transcriber=second_fake)
        self.assertEqual(second_fake.calls[0][0], other)
        self.assertEqual(second["completed_files"], 1)
        state = self.state()
        state["items"][media.name]["next_retry_at"] = 0
        (self.state_dir / "state.json").write_text(json.dumps(state))
        third = self.run_worker(transcriber=FakeTranscriber([BAD]))
        self.assertEqual(third["items"][0]["status"], "blocked")
        fourth_fake = FakeTranscriber()
        fourth = self.run_worker(transcriber=fourth_fake)
        self.assertEqual(fourth["blocked_files"], 1)
        self.assertEqual(fourth_fake.calls, [])
        self.assertEqual(self.state()["items"][media.name]["attempts"], 2)
        self.assertEqual(old.read_bytes(), original)

    def test_editing_failed_subtitle_resets_fingerprint_retry_budget(self):
        media = self.media("lecture.m4a")
        old = self.subtitle(media, BAD)
        self.run_worker(transcriber=FakeTranscriber([BAD]))
        old.write_text(render_srt([SubtitleSegment(0, 700, "Edited, still suspicious.")]))
        result = self.run_worker()
        self.assertEqual(result["completed_files"], 1)
        self.assertEqual(result["items"][0]["attempts"], 1)
        self.assertEqual(result["items"][0]["total_attempts"], 2)

    def test_same_size_subtitle_edit_with_restored_mtime_is_not_overwritten(self):
        media = self.media("lecture.m4a")
        subtitle = self.subtitle(media, BAD)
        before = subtitle.stat()
        edited = subtitle.read_bytes().replace(b"One short sentence.", b"New short sentence.")
        def mutate(_media):
            subtitle.write_bytes(edited)
            os.utime(subtitle, ns=(before.st_atime_ns, before.st_mtime_ns))
        result = self.run_worker(transcriber=FakeTranscriber(callback=mutate))
        self.assertEqual(result["changed_files"], 1)
        self.assertEqual(subtitle.read_bytes(), edited)
        self.assertFalse(media.with_suffix(".ko.srt").exists())

    def test_media_change_during_asr_prevents_publish(self):
        media = self.media("lecture.m4a")
        def mutate(path):
            path.write_bytes(b"new recorded audio")
        result = self.run_worker(transcriber=FakeTranscriber(callback=mutate))
        self.assertEqual(result["changed_files"], 1)
        self.assertFalse(media.with_suffix(".ko.srt").exists())

    def test_new_subtitle_during_asr_is_preserved(self):
        media = self.media("lecture.m4a")
        def mutate(path):
            self.subtitle(path, GOOD)
        result = self.run_worker(transcriber=FakeTranscriber(callback=mutate))
        self.assertEqual(result["changed_files"], 1)
        self.assertTrue(media.with_suffix(".en.srt").exists())
        self.assertFalse(media.with_suffix(".ko.srt").exists())

    def test_in_progress_status_is_flushed_before_asr_and_model_loaded_once(self):
        self.media("a.m4a")
        self.media("b.m4a")
        def check_running(media):
            state = self.state()["items"][media.name]
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["attempts"], 1)
            self.assertIn("전사 중", (self.state_dir / "status.md").read_text())
        fake = FakeTranscriber(callback=check_running)
        with patch.object(worker, "FasterWhisperTranscriber", return_value=fake) as factory:
            result = self.run_worker(limit=2, transcriber=None)
        factory.assert_called_once()
        self.assertEqual(result["completed_files"], 2)

    def test_process_lock_prevents_duplicate_scan_and_asr(self):
        self.media("lecture.m4a")
        self.state_dir.mkdir()
        with (self.state_dir / "worker.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fake = FakeTranscriber()
            result = self.run_worker(transcriber=fake)
        self.assertTrue(result["locked"])
        self.assertEqual(fake.calls, [])
        self.assertFalse((self.state_dir / "state.json").exists())

    def test_backup_like_filenames_and_hidden_media_are_excluded(self):
        media = self.media("lecture.m4a")
        for name in ("lecture.backup.srt", "lecture.en.backup.srt", ".lecture.ko.srt"):
            (media.parent / name).write_text(render_srt(GOOD))
        self.media(".hidden/recording.m4a")
        self.media(".hidden.m4a")
        result = self.run_worker(dry_run=True)
        self.assertEqual(result["media_files"], 1)
        self.assertEqual(result["missing_files"], 1)
        self.assertEqual(worker.paired_subtitles(media), [])

    def test_unstable_media_is_not_processed(self):
        media = self.media("lecture.m4a")
        os.utime(media, None)
        fake = FakeTranscriber()
        result = self.run_worker(stable_seconds=120, transcriber=fake)
        self.assertEqual(result["waiting_files"], 1)
        self.assertEqual(fake.calls, [])

    def test_unstable_suspect_subtitle_is_not_replaced(self):
        media = self.media("lecture.m4a")
        subtitle = self.subtitle(media, BAD)
        fake = FakeTranscriber()
        result = self.run_worker(stable_seconds=120, transcriber=fake)
        self.assertEqual(result["waiting_files"], 1)
        self.assertEqual(fake.calls, [])
        self.assertTrue(subtitle.exists())

    def test_large_text_loss_retries_without_vad_then_keeps_original(self):
        media = self.media("lecture.m4a")
        paragraphs = " ".join(f"Lecture topic {index} has distinct content." for index in range(15))
        original = self.subtitle(media, [SubtitleSegment(0, 1000, paragraphs)])
        body = original.read_bytes()
        fake = FakeTranscriber([GOOD])
        result = self.run_worker(transcriber=fake)
        self.assertEqual(result["failed_files"], 1)
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("substantial_text_loss", result["items"][0]["error"])
        self.assertEqual(original.read_bytes(), body)

    def test_repetition_can_be_replaced_by_shorter_nonrepeating_transcript(self):
        media = self.media("lecture.m4a")
        repeated = [SubtitleSegment(index * 10, index * 10 + 9, "Thank you very much for watching.") for index in range(30)]
        self.subtitle(media, repeated)
        result = self.run_worker()
        self.assertEqual(result["completed_files"], 1)

    def test_partial_old_backup_is_recovered_atomically(self):
        media = self.media("lecture.m4a")
        subtitle = self.subtitle(media, BAD)
        original = subtitle.read_bytes()
        snapshot = worker._snapshot(media)
        import hashlib
        identity = hashlib.sha256(media.name.encode()).hexdigest()[:20]
        backup = self.state_dir / "backups" / identity / worker._fingerprint(snapshot) / subtitle.name
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"partial previous copy")
        result = self.run_worker()
        self.assertEqual(result["completed_files"], 1)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(list(backup.parent.glob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
