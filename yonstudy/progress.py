"""LearnUs VOD 진도 판정에 공통으로 쓰는 규칙."""

from __future__ import annotations


def progress_verified(
    progress_pct: float | None,
    watched_sec: int | None,
    duration_sec: int | None,
) -> bool:
    """진도율과 최대 학습 위치가 모두 완주를 나타내는지 확인한다."""
    return bool(
        (progress_pct or 0) >= 100
        and duration_sec is not None
        and duration_sec > 0
        and watched_sec is not None
        and watched_sec >= max(0, duration_sec - 2)
    )
