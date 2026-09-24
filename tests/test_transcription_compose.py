import unittest
from pathlib import Path


class TranscriptionComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / "compose.transcription.yaml"
        cls.source = source.read_text(encoding="utf-8")

    def test_local_and_remote_are_explicit_profiles(self):
        self.assertIn('transcriber-local:', self.source)
        self.assertIn('profiles: ["local"]', self.source)
        self.assertIn('transcriber-remote:', self.source)
        self.assertIn('profiles: ["remote"]', self.source)
        self.assertEqual(self.source.count('container_name: yonstudy-transcriber'), 1)

    def test_local_mounts_archive_and_remote_uses_rclone_staging(self):
        self.assertIn(':/archive', self.source)
        self.assertIn('/app/deploy/remote_transcribe_loop.py', self.source)
        self.assertIn('RCLONE_CONFIG: /config/rclone/rclone.conf', self.source)
        self.assertIn(':/work/archive', self.source)


if __name__ == "__main__":
    unittest.main()
