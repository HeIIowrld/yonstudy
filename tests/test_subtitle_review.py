import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from yonstudy.subtitle_review import probe_duration, review_segments, review_subtitle
from yonstudy.transcribe import SubtitleSegment


class SubtitleReviewTests(unittest.TestCase):
    def review_text(self, text, *, duration=120.0, suffix=".srt"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"강의{suffix}"
            path.write_text(text, encoding="utf-8")
            original = path.read_bytes()
            review = review_subtitle(path, duration)
            self.assertEqual(path.read_bytes(), original)
            return review

    def assert_reason(self, review, code):
        self.assertEqual(review.status, "suspect")
        self.assertTrue(any(reason.startswith(code + ":") for reason in review.reasons), review)

    def test_normal_srt_accepts_bom_crlf_multiline_and_formatting(self):
        review = self.review_text(
            "\ufeff1\r\n00:00:01,000 --> 00:00:04,000\r\n<b>안녕하세요.</b>\r\n"
            "오늘은 논리 회로를 배웁니다.\r\n\r\n"
            "2\r\n00:01:00,000 --> 00:01:04,000\r\n다음 내용입니다.\r\n"
        )
        self.assertEqual(review.status, "ok")
        self.assertEqual(review.metrics["cue_count"], 2)
        self.assertEqual(review.metrics["covered_sec"], 7)

    def test_vtt_accepts_header_metadata_cue_identifiers_settings_and_notes(self):
        review = self.review_text(
            "WEBVTT\nKind: captions\nLanguage: ko\n\n"
            "NOTE this is not a cue\n설명입니다.\n\n"
            "STYLE\n::cue { color: yellow; }\n\n"
            "REGION\nid:example\n\n"
            "intro\n00:01.000 --> 00:04.000 align:start position:10%\n"
            "<v 강사>이번 강의 내용입니다.</v>\n\n"
            "00:05.000 --> 00:08.000\n회로를 살펴봅시다.\n",
            suffix=".vtt",
        )
        self.assertEqual(review.status, "ok")
        self.assertEqual(review.metrics["cue_count"], 2)

    def test_empty_and_header_only_subtitles_are_suspect(self):
        for text, suffix in [("", ".srt"), ("WEBVTT\n\n", ".vtt")]:
            with self.subTest(suffix=suffix):
                self.assert_reason(self.review_text(text, suffix=suffix), "empty_subtitle")

    def test_malformed_blocks_are_not_silently_discarded(self):
        review = self.review_text(
            "1\n00:00:01,000 --> 00:00:04,000\n정상 구간입니다.\n\n"
            "2\n00:75:00,000 --> 00:76:00,000\n시간 형식이 잘못되었습니다.\n\n"
            "arbitrary broken data\n"
        )
        self.assert_reason(review, "malformed_subtitle")
        self.assertEqual(review.metrics["cue_count"], 1)
        self.assertEqual(review.metrics["malformed_block_count"], 2)

    def test_vtt_missing_header_is_suspect_even_if_cues_are_valid(self):
        review = self.review_text("00:01.000 --> 00:04.000\n내용입니다.\n", suffix=".vtt")
        self.assert_reason(review, "malformed_subtitle")
        self.assertEqual(review.metrics["cue_count"], 1)

    def test_arrow_in_lecture_text_is_not_a_timestamp(self):
        review = self.review_text("1\n00:00:01,000 --> 00:00:04,000\nA --> B 연결을 살펴봅시다.\n")
        self.assertEqual(review.status, "ok")

    def test_cues_without_separator_are_structurally_invalid(self):
        review = self.review_text(
            "1\n00:00:01,000 --> 00:00:04,000\n첫 구간입니다.\n"
            "2\n00:00:05,000 --> 00:00:08,000\n빈 줄 없이 이어진 구간입니다.\n"
        )
        self.assert_reason(review, "malformed_subtitle")

    def test_invalid_utf8_is_a_review_result_without_overwriting_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lecture.srt"
            path.write_bytes(b"\xff\xfe\xff")
            review = review_subtitle(path, 100)
            self.assert_reason(review, "invalid_encoding")
            self.assertEqual(path.read_bytes(), b"\xff\xfe\xff")

    def test_negative_reversed_and_outside_timestamps(self):
        review = self.review_text(
            "1\n-00:00:01,000 --> 00:00:01,000\n음수 시간입니다.\n\n"
            "2\n00:00:05,000 --> 00:00:04,000\n역전된 시간입니다.\n\n"
            "3\n00:02:05,000 --> 00:02:07,000\n영상 뒤의 시간입니다.\n"
        )
        for code in ["negative_timestamps", "reversed_timestamps", "outside_media"]:
            self.assert_reason(review, code)

    def test_nonfinite_and_zero_length_cues_are_suspect(self):
        review = review_segments(
            [SubtitleSegment(float("nan"), 1, "나쁜 시간"), SubtitleSegment(2, 2, "길이 없음")], 10
        )
        self.assert_reason(review, "invalid_timestamps")
        self.assert_reason(review, "reversed_timestamps")
        # 보고서로 안전하게 직렬화할 수 있다.
        json.dumps(review.metrics, allow_nan=False)

    def test_small_timestamp_rounding_overrun_is_accepted(self):
        review = review_segments([SubtitleSegment(8.0, 10.005, "강의를 마칩니다.")], 10.0)
        self.assertEqual(review.status, "ok")

    def test_actual_failure_shape_86_minutes_18_cues_is_suspect(self):
        segments = [SubtitleSegment(0, 745, "오늘은 논리 회로에 대해 설명하겠습니다.")]
        segments.extend(
            SubtitleSegment(745 + index * 259, min(745 + (index + 1) * 259, 5160), f"{index}번 회로 내용입니다.")
            for index in range(17)
        )
        review = review_segments(segments, 86 * 60)
        self.assert_reason(review, "long_sparse_cues")
        self.assertEqual(review.metrics["cue_count"], 18)
        self.assertEqual(review.metrics["max_cue_sec"], 745)
        self.assertEqual(review.metrics["thresholds"]["long_cue_sec"], 120)

    def test_long_silence_and_few_short_cues_are_not_failure_evidence(self):
        review = review_segments(
            [SubtitleSegment(0, 3, "안녕하세요. 오늘 수업을 시작합니다."),
             SubtitleSegment(5100, 5104, "이제 실습을 마칩니다.")],
            5160,
        )
        self.assertEqual(review.status, "ok")
        self.assertLess(review.metrics["chars_per_media_minute"], 1)

    def test_short_whole_recording_and_normal_lecture_are_accepted(self):
        self.assertEqual(review_segments([SubtitleSegment(0, 1, "네.")], 1).status, "ok")
        lecture = [SubtitleSegment(index * 5, index * 5 + 4, f"여기는 {index}번째 회로의 연결을 설명하는 부분입니다.")
                   for index in range(720)]
        self.assertEqual(review_segments(lecture, 3600).status, "ok")

    def test_long_dense_paragraph_cue_is_not_flagged_only_for_length(self):
        text = "여러 종류의 회로와 진리표를 차례대로 살펴보겠습니다. " * 4
        review = review_segments([SubtitleSegment(0, 121, text)], 130)
        self.assertEqual(review.status, "ok")

    def test_severe_repeated_sentences_are_flagged_but_short_acknowledgements_are_not(self):
        repeated = [SubtitleSegment(index * 8, index * 8 + 7, "시청해 주셔서 정말 감사합니다.") for index in range(12)]
        self.assert_reason(review_segments(repeated, 100), "severe_repetition")
        short = [SubtitleSegment(index * 8, index * 8 + 1, "네.") for index in range(12)]
        self.assertEqual(review_segments(short, 100).status, "ok")

    def test_hallucinated_phrase_repeated_inside_single_cue_is_flagged(self):
        review = review_segments([SubtitleSegment(0, 30, "시청해 주셔서 감사합니다. " * 30)], 30)
        self.assert_reason(review, "severe_repetition")

    def test_normal_overlapping_cues_count_union_coverage(self):
        review = review_segments(
            [SubtitleSegment(0, 5, "첫 번째 화자의 설명입니다."),
             SubtitleSegment(3, 8, "두 번째 화자의 응답입니다.")], 10
        )
        self.assertEqual(review.status, "ok")
        self.assertEqual(review.metrics["covered_sec"], 8)

    def test_invalid_duration_is_not_treated_as_quality_failure(self):
        for duration in [0, -1, float("inf"), float("nan")]:
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                review_segments([], duration)

    def test_segment_iterator_and_compatible_backend_objects_are_accepted(self):
        segments = (SimpleNamespace(start=0, end=3, text="수업 내용입니다.") for _ in range(1))
        self.assertEqual(review_segments(segments, 10).status, "ok")

    @patch("yonstudy.subtitle_review.subprocess.run")
    def test_probe_uses_argument_list_absolute_path_and_timeout(self, run):
        run.return_value = SimpleNamespace(stdout='{"format":{"duration":"5160.25"}}')
        media = Path("-lecture ; $(echo nope).m4a")
        self.assertEqual(probe_duration(media), 5160.25)
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[-2:], ["-i", str(media.resolve())])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertNotIn("shell", run.call_args.kwargs)

    @patch("yonstudy.subtitle_review.subprocess.run")
    def test_probe_rejects_missing_invalid_and_nonfinite_duration(self, run):
        for body in ['{}', 'not json', '{"format":{"duration":"N/A"}}',
                     '{"format":{"duration":"NaN"}}', '{"format":{"duration":"0"}}']:
            with self.subTest(body=body), self.assertRaises(ValueError):
                run.return_value = SimpleNamespace(stdout=body)
                probe_duration(Path("lecture.m4a"))

    @patch("yonstudy.subtitle_review.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 30))
    def test_probe_timeout_is_propagated(self, _run):
        with self.assertRaises(subprocess.TimeoutExpired):
            probe_duration(Path("lecture.m4a"))


if __name__ == "__main__":
    unittest.main()
