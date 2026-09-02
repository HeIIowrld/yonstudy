import unittest

from yonstudy.parse import ProgressRow, _completion_state


class CompletionStateTests(unittest.TestCase):
    def test_legacy_icons(self):
        self.assertEqual(_completion_state('<img src="completion-auto-y">'), "y")
        self.assertEqual(_completion_state('<img src="completion-manual-n">'), "n")

    def test_data_state(self):
        self.assertEqual(_completion_state('<button data-completionstate="1">'), "y")
        self.assertEqual(_completion_state('<button data-activity-completionstate="0">'), "n")

    def test_manual_checkbox(self):
        self.assertEqual(
            _completion_state('<input class="activity-completion" type="checkbox" checked>'),
            "y",
        )
        self.assertEqual(
            _completion_state('<input class="activity-completion" type="checkbox">'),
            "n",
        )
        self.assertEqual(
            _completion_state(
                '<form class="togglecompletion"><input type="checkbox" checked></form>'
            ),
            "y",
        )

    def test_aria_and_localized_labels(self):
        self.assertEqual(
            _completion_state('<button class="completion-toggle" aria-pressed="true">'),
            "y",
        )
        self.assertEqual(_completion_state('<img title="완료로 표시: 1주차">'), "n")
        self.assertEqual(
            _completion_state('<img title="완료하지 않은 것으로 표시: 1주차">'),
            "y",
        )

    def test_untracked_is_not_incomplete(self):
        self.assertIsNone(_completion_state('<li><input type="checkbox"></li>'))


class ProgressRowTests(unittest.TestCase):
    def test_localized_details_suffix_does_not_truncate_seconds(self):
        row = ProgressRow(
            week="1",
            title="강의",
            content_time="56:40",
            max_position="56:40 상세보기 (1)",
            progress="100%",
        )
        self.assertEqual(row.duration_sec, 3400)
        self.assertEqual(row.watched_sec, 3400)


if __name__ == "__main__":
    unittest.main()
