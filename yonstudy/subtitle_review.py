"""자막을 수정하지 않고 구조와 뚜렷한 전사 실패 징후를 검토한다.

``suspect``는 사람이 검토하거나 재전사할 후보라는 휴리스틱 판정이다.
정확도나 실제 음성의 존재를 보증하지 않는다. 긴 침묵, 적은 전체 자막 수,
낮은 전체 영상 대비 문자 수만으로 실패를 추정하지 않는다.
"""

from __future__ import annotations

import html
import json
import math
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .transcribe import SubtitleSegment


# 경계값도 결과에 기록하여 자동 재전사 판단 근거를 확인할 수 있게 한다.
LONG_CUE_SECONDS = 120.0
LONG_CUE_MAX_CHARS_PER_SECOND = 0.75
LOW_DENSITY_MIN_COVERED_SECONDS = 300.0
LOW_DENSITY_MAX_CHARS_PER_SECOND = 0.25
REPEAT_MIN_COUNT = 10
REPEAT_MIN_FRACTION = 0.6
REPEAT_MIN_SECONDS = 60.0
_TIMESTAMP = re.compile(r"^(-?)(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{1,3})$")
_CUE_TIME = re.compile(r"^(\S+)\s+-->\s+(\S+)(?:\s+.*)?$")
_TAGS = re.compile(r"<[^>]*>")


@dataclass
class Review:
    status: str
    reasons: list[str]
    metrics: dict


def probe_duration(media: Path) -> float:
    """ffprobe로 유한한 양수 길이를 읽는다. 실패하거나 길이가 없으면 예외를 낸다."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", "-i", str(Path(media).resolve()),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ffprobe 결과에 올바른 미디어 길이가 없습니다") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("미디어 길이는 유한한 양수여야 합니다")
    return duration


def _seconds(timestamp: str) -> float:
    match = _TIMESTAMP.fullmatch(timestamp)
    if not match:
        raise ValueError("잘못된 자막 타임스탬프")
    sign, hours, minutes, seconds, fraction = match.groups()
    if int(minutes) >= 60 or int(seconds) >= 60:
        raise ValueError("자막 분/초는 60보다 작아야 합니다")
    value = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    value += int(fraction) / (10 ** len(fraction))
    return -value if sign else value


def _visible_text(text: str) -> str:
    return html.unescape(_TAGS.sub("", text))


def _normalized(text: str) -> str:
    return "".join(char.casefold() for char in _visible_text(text) if char.isalnum())


def _coverage(intervals: list[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    start, end = sorted(intervals)[0]
    total = 0.0
    for next_start, next_end in sorted(intervals)[1:]:
        if next_start > end:
            total += end - start
            start, end = next_start, next_end
        else:
            end = max(end, next_end)
    return total + end - start


def review_segments(segments: Iterable[SubtitleSegment], duration_sec: float) -> Review:
    """구조 오류와 심한 저밀도/반복만 의심 후보로 표시한다. 원본은 변경하지 않는다."""
    if not math.isfinite(duration_sec) or duration_sec <= 0:
        raise ValueError("duration_sec는 유한한 양수여야 합니다")
    rows = list(segments)
    reasons: list[str] = []
    invalid = empty = negative = reversed_times = outside = long_sparse = 0
    within_cue_repeats = 0
    chars = 0
    longest = 0.0
    tolerance = max(2.0, min(10.0, duration_sec * 0.002))
    intervals: list[tuple[float, float]] = []
    texts: list[str] = []
    durations: list[float] = []
    for row in rows:
        visible = _visible_text(row.text)
        count = sum(not char.isspace() for char in visible)
        chars += count
        empty += not bool(visible.strip())
        normalized = _normalized(row.text)
        texts.append(normalized)
        if not math.isfinite(row.start) or not math.isfinite(row.end):
            invalid += 1
            durations.append(0.0)
            continue
        negative += row.start < 0 or row.end < 0
        reversed_times += row.end <= row.start
        outside += row.start > duration_sec + tolerance or row.end > duration_sec + tolerance
        span = max(0.0, row.end - row.start)
        durations.append(span)
        longest = max(longest, span)
        if row.end > row.start and row.end > 0 and row.start < duration_sec:
            intervals.append((max(0.0, row.start), min(duration_sec, row.end)))
        if span > LONG_CUE_SECONDS and count / span < LONG_CUE_MAX_CHARS_PER_SECOND:
            long_sparse += 1
        if len(normalized) >= 80:
            repeated = re.search(r"(.{6,80}?)\1{7,}", normalized)
            if repeated and len(repeated.group(0)) / len(normalized) >= 0.8:
                within_cue_repeats += 1

    covered = _coverage(intervals)
    density = chars / covered if covered else 0.0
    eligible = Counter(text for text in texts if len(text) >= 8)
    dominant, dominant_count = eligible.most_common(1)[0] if eligible else ("", 0)
    dominant_fraction = dominant_count / len(rows) if rows else 0.0
    dominant_seconds = sum(span for text, span in zip(texts, durations) if text == dominant)
    repeated_run = run = 0
    repeated_run_seconds = run_seconds = 0.0
    previous = ""
    for text, span in zip(texts, durations):
        if len(text) >= 8:
            run = run + 1 if text == previous else 1
            run_seconds = run_seconds + span if text == previous else span
        else:
            run = 0
            run_seconds = 0.0
        previous = text
        if run > repeated_run or (run == repeated_run and run_seconds > repeated_run_seconds):
            repeated_run, repeated_run_seconds = run, run_seconds

    if not rows or chars == 0:
        reasons.append("empty_subtitle: 읽을 수 있는 자막 내용이 없습니다")
    if empty:
        reasons.append(f"empty_cues: 내용 없는 구간 {empty}개")
    if invalid:
        reasons.append(f"invalid_timestamps: 유한하지 않은 시간 {invalid}개")
    if negative:
        reasons.append(f"negative_timestamps: 음수 시간 구간 {negative}개")
    if reversed_times:
        reasons.append(f"reversed_timestamps: 끝이 시작보다 늦지 않은 구간 {reversed_times}개")
    if outside:
        reasons.append(f"outside_media: 영상 길이 + {tolerance:g}초를 벗어난 구간 {outside}개")
    if long_sparse:
        reasons.append(
            f"long_sparse_cues: {LONG_CUE_SECONDS:g}초 초과이면서 "
            f"초당 {LONG_CUE_MAX_CHARS_PER_SECOND:g}자 미만인 구간 {long_sparse}개"
        )
    # 전체 영상 길이 대신 실제로 자막이 표시되는 시간을 사용한다.
    # 따라서 정상적인 긴 무음 구간만으로 저밀도 판정이 나지 않는다.
    if covered >= LOW_DENSITY_MIN_COVERED_SECONDS and density < LOW_DENSITY_MAX_CHARS_PER_SECOND:
        reasons.append(
            f"low_active_text_density: 자막 표시 시간 {covered:.1f}초에서 "
            f"초당 {density:.3f}자 (기준 {LOW_DENSITY_MAX_CHARS_PER_SECOND:g}자 미만)"
        )
    if (
        dominant_count >= REPEAT_MIN_COUNT
        and dominant_fraction >= REPEAT_MIN_FRACTION
        and dominant_seconds >= REPEAT_MIN_SECONDS
    ) or (repeated_run >= 12 and repeated_run_seconds >= REPEAT_MIN_SECONDS) or within_cue_repeats:
        reasons.append("severe_repetition: 충분히 긴 동일 문구가 과도하게 반복됩니다")

    metrics = {
        "duration_sec": duration_sec,
        "cue_count": len(rows),
        "text_chars": chars,
        "covered_sec": round(covered, 3),
        "coverage_ratio": round(covered / duration_sec, 5),
        "chars_per_covered_sec": round(density, 5),
        "chars_per_media_minute": round(chars * 60 / duration_sec, 3),
        "max_cue_sec": round(longest, 3),
        "long_sparse_cue_count": long_sparse,
        "invalid_timestamp_count": invalid,
        "negative_timestamp_count": negative,
        "reversed_timestamp_count": reversed_times,
        "outside_media_count": outside,
        "empty_cue_count": empty,
        "dominant_repeat_count": dominant_count,
        "dominant_repeat_fraction": round(dominant_fraction, 5),
        "max_repeat_run": repeated_run,
        "within_cue_repeat_count": within_cue_repeats,
        "thresholds": {
            "long_cue_sec": LONG_CUE_SECONDS,
            "long_cue_max_chars_per_sec": LONG_CUE_MAX_CHARS_PER_SECOND,
            "low_density_min_covered_sec": LOW_DENSITY_MIN_COVERED_SECONDS,
            "low_density_max_chars_per_sec": LOW_DENSITY_MAX_CHARS_PER_SECOND,
            "repeat_min_count": REPEAT_MIN_COUNT,
            "repeat_min_fraction": REPEAT_MIN_FRACTION,
            "repeat_min_sec": REPEAT_MIN_SECONDS,
            "outside_media_tolerance_sec": tolerance,
        },
    }
    return Review("suspect" if reasons else "ok", reasons, metrics)


def review_subtitle(path: Path, duration_sec: float) -> Review:
    """SRT/VTT를 읽어 구조와 품질을 검토한다. 파일을 쓰거나 교정하지 않는다."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        review = review_segments([], duration_sec)
        review.reasons.insert(0, "invalid_encoding: UTF-8 자막으로 읽을 수 없습니다")
        return review
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    vtt = path.suffix.lower() == ".vtt" or text.startswith("WEBVTT")
    malformed = 0
    if vtt:
        if not re.match(r"^WEBVTT(?:[ \t].*)?(?:\n|$)", text):
            malformed += 1
        else:
            # 헤더는 다음 빈 줄까지이며 Kind/Language 등의 메타데이터가 가능하다.
            parts = re.split(r"\n[ \t]*\n", text, maxsplit=1)
            if len(parts) == 2:
                text = parts[1]
            elif "-->" not in parts[0]:
                text = ""
            else:
                malformed += 1
                text = text.split("\n", 1)[1]
    segments: list[SubtitleSegment] = []
    for block in re.split(r"\n[ \t]*\n", text):
        if not block.strip():
            continue
        lines = block.strip().splitlines()
        if vtt and re.match(r"^(?:NOTE(?:[ \t]|$)|STYLE$|REGION$)", lines[0]):
            continue
        index = 0 if "-->" in lines[0] else 1
        if index >= len(lines):
            malformed += 1
            continue
        if index == 1 and not vtt and not lines[0].strip().isdigit():
            malformed += 1
            continue
        match = _CUE_TIME.fullmatch(lines[index].strip())
        if not match:
            malformed += 1
            continue
        try:
            start, end = (_seconds(value) for value in match.groups())
        except ValueError:
            malformed += 1
            continue
        if any(re.match(r"^-?\d+:", line.strip()) and "-->" in line for line in lines[index + 1:]):
            # 빈 줄 없이 다음 cue가 붙은 경우다. 본문의 일반 화살표는 허용한다.
            malformed += 1
            continue
        segments.append(SubtitleSegment(start, end, "\n".join(lines[index + 1:])))
    review = review_segments(segments, duration_sec)
    review.metrics["malformed_block_count"] = malformed
    if malformed:
        review.status = "suspect"
        review.reasons.insert(0, f"malformed_subtitle: 해석할 수 없는 자막 블록/헤더 {malformed}개")
    return review
