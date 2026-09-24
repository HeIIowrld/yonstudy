import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from deploy.transcribe_loop import next_delay, read_reprocess_list, worker_options


class ReviewLoopTests(unittest.TestCase):
    def test_queue_drains_without_half_hour_gaps(self):
        self.assertEqual(next_delay({'pending_files': 3}, 300), 5)
        self.assertEqual(next_delay({'pending_files': 0, 'blocked_files': 2}, 300), 300)
        self.assertEqual(next_delay({'pending_files': 3, 'locked': True}, 300), 300)

    def test_review_uses_bounded_nas_defaults_and_auto_language(self):
        with patch.dict(os.environ, {}, clear=True):
            options = worker_options()
        self.assertEqual(options['model'], 'small')
        self.assertEqual(options['model_id'], 'small-int8')
        self.assertEqual(options['cpu_threads'], 2)
        self.assertEqual(options['state_dir'], '/state')
        self.assertIsNone(options['language'])

    def test_reprocess_list_is_shared_with_local_profile(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'reprocess.txt'
            source.write_text('# upgraded model\ncourse/one.m4a\n\ncourse/two.mp4\n', encoding='utf-8')
            self.assertEqual(read_reprocess_list(source), {'course/one.m4a', 'course/two.mp4'})

    def test_explicit_model_and_language_are_passed_through(self):
        with patch.dict(os.environ, {
            'YONSTUDY_TRANSCRIBE_MODEL': '/models/preloaded',
            'YONSTUDY_TRANSCRIBE_LANGUAGE': 'en',
            'YONSTUDY_TRANSCRIBE_CPU_THREADS': 'bad',
        }, clear=True):
            options = worker_options()
        self.assertEqual(options['model'], '/models/preloaded')
        self.assertEqual(options['language'], 'en')
        self.assertEqual(options['cpu_threads'], 2)


if __name__ == '__main__':
    unittest.main()
