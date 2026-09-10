"""수강 기간이 열린 VOD를 Playwright로 재생한다.

서버의 진도 API를 직접 호출하지 않는다. 뷰어가 허용한 배속으로 실제 재생하고,
완주 후 출석부를 다시 읽어 반영 여부를 확인한다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from random import randint, random
from typing import Callable
from zoneinfo import ZoneInfo

from .progress import progress_verified

VIEWER = "https://ys.learnus.org/mod/vod/viewer.php?id={cmid}"
SEOUL = ZoneInfo("Asia/Seoul")
INTER_VIDEO_DELAY_MIN_SECONDS = 60
INTER_VIDEO_DELAY_MAX_SECONDS = 10 * 60


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


@dataclass
class Job:
    cmid: int
    course_id: int
    course_name: str
    title: str
    duration_sec: int
    watched_sec: int
    open_from: str | None
    open_to: str | None
    late_until: str | None
    rate: float
    urgency: str
    deadline: datetime | None
    tracks_progress: bool = True

    @property
    def remaining_sec(self) -> int:
        return max(0, self.duration_sec - self.watched_sec)

    @property
    def viewer_url(self) -> str:
        return VIEWER.format(cmid=self.cmid)

    @property
    def eta_sec(self) -> float:
        """실제 재생에 걸리는 시간 = 남은 분량 / 배속 (+ 버퍼링 여유)."""
        return self.remaining_sec / max(self.rate, 0.25) * 1.08


def build_plan(
    store,
    now: datetime | None = None,
    max_rate_cap: float = 2.0,
    course_ids: set[int] | None = None,
    include_late: bool = False,
    include_untracked_once: bool = False,
) -> list[Job]:
    """지금 재생해야 하는 영상을 골라 우선순위대로 정렬한다.

    정렬 기준:
    1. 긴급도
    2. 같은 긴급도 내 정확한 마감순
    3. 같은 마감 내 과목 순서 랜덤
    4. 남은 재생 시간이 긴 순

    진도 추적 영상은 진도율과 최대 학습 위치가 모두 완주를 나타내지 않으면서
    현재 진도 처리 기간인 경우만 고른다.
    ``include_untracked_once``를 켜면 진도율·완료 체크가 없는 영상도 공개 후
    한 번 완주 대상으로 넣는다.

    성공 기록은 ``crawl_log(kind='playback_once')``로 판정한다.
    """
    if now is None:
        now = datetime.now(SEOUL).replace(tzinfo=None)
    elif now.tzinfo is not None:
        now = now.astimezone(SEOUL).replace(tzinfo=None)

    course_clause = ""
    params: tuple = ()

    if course_ids is not None:
        if not course_ids:
            return []

        placeholders = ",".join("?" for _ in course_ids)
        course_clause = f" AND v.course_id IN ({placeholders})"
        params = tuple(sorted(course_ids))

    rows = store.query(
        f"""
        SELECT
            v.cmid,
            v.course_id,
            v.duration_sec,
            v.watched_sec,
            v.progress_pct,
            v.can_log_progress,
            v.max_rate,
            v.is_progress,
            v.status,
            a.title,
            a.completion,
            a.open_from,
            a.open_to,
            a.late_until,
            c.name AS course_name,
            EXISTS(
                SELECT 1
                  FROM crawl_log l
                 WHERE l.kind = 'playback_once'
                   AND l.ref = CAST(v.cmid AS TEXT)
                   AND l.ok = 1
            ) AS played_once
         FROM vod v
         JOIN activity a ON a.cmid = v.cmid
          JOIN course c ON c.course_id = v.course_id
         WHERE c.enrolled=1 AND a.present=1
           AND COALESCE(a.restricted,0)=0
         {course_clause}
        """,
        params,
    )

    jobs: list[Job] = []

    for row in rows:
        duration = row["duration_sec"] or 0
        if duration <= 0:
            continue

        start = _dt(row["open_from"])
        end = _dt(
            (
                row["late_until"]
                if include_late
                else None
            )
            or row["open_to"]
        )

        in_calendar = (
            (start is None or start <= now)
            and (end is None or now <= end)
        )

        # 뷰어 조회 실패·구형 DB의 NULL을 진도 추적 영상으로
        # 추측해 자동 재생하지 않는다.
        if row["is_progress"] not in (0, 1):
            continue
        tracks_progress = row["is_progress"] == 1

        if tracks_progress and (
            row["completion"] == "y"
            or progress_verified(
                row["progress_pct"], row["watched_sec"], row["duration_sec"]
            )
        ):
            continue

        if not tracks_progress:
            # 진도 추적이 없는 영상은 공개 후 한 번만 재생한다.
            if (
                not include_untracked_once
                or row["status"] != "ok"
                or bool(row["played_once"])
                or (start is not None and start > now)
            ):
                continue

            open_now = True

        elif row["status"] == "ok" and row["can_log_progress"] == 1:
            # 뷰어가 성공적으로 진도 처리 가능을 확인한 경우만 재생한다.
            open_now = (
                in_calendar
            )
        else:
            open_now = False

        if not open_now:
            continue

        rate = min(
            float(row["max_rate"] or 1.0),
            max_rate_cap,
        )

        if not tracks_progress:
            urgency = "1회 재생"
            deadline = None

        elif end is None:
            urgency = "상시"
            deadline = None

        else:
            time_left = end - now
            deadline = end

            if time_left < timedelta(days=1):
                urgency = "긴급"
            elif time_left < timedelta(days=3):
                urgency = "임박"
            else:
                urgency = "여유"

        jobs.append(
            Job(
                cmid=row["cmid"],
                course_id=row["course_id"],
                course_name=row["course_name"],
                title=row["title"],
                duration_sec=duration,
                watched_sec=row["watched_sec"] or 0,
                open_from=row["open_from"],
                open_to=row["open_to"],
                late_until=row["late_until"],
                rate=rate,
                urgency=urgency,
                deadline=deadline,
                tracks_progress=tracks_progress,
            )
        )

    urgency_order = {
        "긴급": 0,
        "임박": 1,
        "여유": 2,
        "상시": 3,
        "1회 재생": 4,
    }

    # 같은 긴급도에 속한 과목마다 랜덤 순위를 하나씩 부여한다.
    course_keys = {
        (job.urgency, job.course_id)
        for job in jobs
    }

    course_random_rank = {
        key: random()
        for key in course_keys
    }

    jobs.sort(
        key=lambda job: (
            urgency_order[job.urgency],
            job.deadline or datetime.max,
            course_random_rank[(job.urgency, job.course_id)],
            -job.remaining_sec,
        )
    )

    return jobs


def upcoming(store, now: datetime | None = None, days: int = 14) -> list[dict]:
    """아직 열리지 않았지만 곧 열리는 영상을 개시일에 자동 편입하도록 예약한다."""
    if now is None:
        now = datetime.now(SEOUL).replace(tzinfo=None)
    elif now.tzinfo is not None:
        now = now.astimezone(SEOUL).replace(tzinfo=None)
    horizon = now + timedelta(days=days)
    out = []
    for r in store.query(
        """
        SELECT a.cmid,a.title,a.open_from,a.open_to,a.completion,
               v.progress_pct,v.watched_sec,v.duration_sec,v.is_progress,
               c.name AS course_name,
               EXISTS(
                   SELECT 1 FROM crawl_log l
                    WHERE l.kind='playback_once'
                      AND l.ref=CAST(v.cmid AS TEXT) AND l.ok=1
               ) AS played_once
         FROM activity a JOIN course c ON c.course_id = a.course_id
          LEFT JOIN vod v ON v.cmid = a.cmid
         WHERE c.enrolled=1 AND a.present=1
           AND COALESCE(a.restricted,0)=0
           AND a.modname='vod' AND a.open_from IS NOT NULL
        """
    ):
        if (
            r["completion"] == "y"
            or progress_verified(
                r["progress_pct"], r["watched_sec"], r["duration_sec"]
            )
            or (r["is_progress"] == 0 and bool(r["played_once"]))
        ):
            continue
        start = _dt(r["open_from"])
        if start and now < start <= horizon:
            out.append(dict(r))
    out.sort(key=lambda x: x["open_from"])
    return out


# 브라우저 재생

PLAYWRIGHT_HELP = """\
Playwright가 필요합니다:

    python -m pip install playwright
    python -m playwright install chromium

설치 뒤 `video.canPlayType()`으로 H.264/AAC 지원을 확인하세요. 다른 시스템 Chrome을
쓰려면 YONSTUDY_BROWSER_CHANNEL=chrome을 설정할 수 있습니다.
"""


def _cookies_for_playwright(cookie_path: str) -> list[dict]:
    import http.cookiejar

    jar = http.cookiejar.MozillaCookieJar(cookie_path)
    jar.load(ignore_discard=True, ignore_expires=True)
    return [
        {
            "name": c.name, "value": c.value,
            "domain": c.domain, "path": c.path,
            "secure": bool(c.secure),
            "expires": c.expires or -1,
        }
        for c in jar
    ]


# 뷰어 안에서 재생 상태를 관찰하는 스크립트.
# viewer 페이지에 개발자 도구 탐지 트랩이 있으므로 console.log를 건드리지 않는다.
# console을 가로채면 강제 로그아웃 폼이 제출될 수 있다.
_WATCH_JS = """
(rate) => {
  const v = document.querySelector('video');
  if (!v) return null;
  if (v.playbackRate !== rate) v.playbackRate = rate;
  return {
    current: v.currentTime,
    duration: v.duration,
    paused: v.paused,
    rate: v.playbackRate,
    ready: v.readyState,
  };
}
"""


def inter_video_delay_seconds() -> int:
    """Return the pause used between consecutive items in a playback queue."""
    return randint(INTER_VIDEO_DELAY_MIN_SECONDS, INTER_VIDEO_DELAY_MAX_SECONDS)


def run_plan(
    plan: list[Job],
    cookie_path: str,
    *,
    dry_run: bool = False,
    rate: float | None = None,
    poll_sec: float = 15.0,
    on_result: Callable[[Job, bool, str], None] | None = None,
) -> int:
    if not plan:
        print("재생할 대상이 없습니다.")
        return 0

    if dry_run:
        print(f"[dry-run] {len(plan)}편, 총 {sum(j.eta_sec for j in plan)/3600:.1f}시간 예상")
        if len(plan) > 1:
            print(
                "  영상 사이 대기: "
                f"{INTER_VIDEO_DELAY_MIN_SECONDS // 60}~"
                f"{INTER_VIDEO_DELAY_MAX_SECONDS // 60}분씩 "
                f"{len(plan) - 1}회"
            )
        for j in plan:
            print(f"  {j.rate}x  {j.course_name[:12]:12s} {j.title[:40]:40s} "
                  f"{j.remaining_sec//60}분 → {j.eta_sec/60:.0f}분")
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(PLAYWRIGHT_HELP)
        return 1

    import time

    done = failed = 0
    with sync_playwright() as pw:
        channel = os.environ.get("YONSTUDY_BROWSER_CHANNEL", "").strip()
        launch_options = {
            "headless": True,
            "args": ["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
        }
        if channel:
            launch_options["channel"] = channel
        browser = pw.chromium.launch(**launch_options)
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        ctx.add_cookies(_cookies_for_playwright(cookie_path))

        codec_page = ctx.new_page()
        h264 = codec_page.evaluate(
            "() => document.createElement('video').canPlayType("
            "'video/mp4; codecs=\"avc1.42E01E, mp4a.40.2\"')"
        )
        codec_page.close()
        if not h264:
            browser.close()
            print("현재 브라우저가 LearnUs H.264/AAC 영상을 재생할 수 없습니다.")
            return 1

        for i, job in enumerate(plan, 1):
            if i > 1:
                delay = inter_video_delay_seconds()
                print(f"\n다음 영상까지 {delay // 60}분 {delay % 60}초 대기", flush=True)
                time.sleep(delay)
            use_rate = min(rate or job.rate, job.rate)
            eta_sec = job.remaining_sec / max(use_rate, 0.25) * 1.08
            print(f"\n[{i}/{len(plan)}] {job.course_name} — {job.title}")
            print(f"  남은 {job.remaining_sec//60}분, {use_rate}x → 약 {eta_sec/60:.0f}분")
            page = ctx.new_page()
            # 뷰어는 창을 닫을 때 beforeunload 확인창을 띄운다.
            page.on("dialog", lambda d: d.accept())
            try:
                page.goto(job.viewer_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_selector("video", timeout=60_000)
                page.evaluate("() => { const v=document.querySelector('video'); v.muted=true; v.play(); }")

                stalled = 0
                last = -1.0
                finished = False
                deadline = time.monotonic() + eta_sec + 300
                while time.monotonic() < deadline:
                    time.sleep(poll_sec)
                    st = page.evaluate(_WATCH_JS, use_rate)
                    if not st:
                        raise RuntimeError("video 요소가 사라졌습니다")
                    cur, dur = st["current"], st["duration"] or job.duration_sec
                    if cur <= last + 0.5:
                        stalled += 1
                        if stalled >= 3:  # 45초간 진전 없음
                            print("    정체 감지 — 재생 재시도")
                            page.evaluate("() => document.querySelector('video').play()")
                            stalled = 0
                    else:
                        stalled = 0
                    last = cur
                    print(f"    {cur/60:5.1f}/{dur/60:5.1f}분  ({cur/dur*100:5.1f}%)", flush=True)
                    if dur and cur >= dur - 2:
                        finished = True
                        break

                if not finished:
                    raise TimeoutError("예상 시간 안에 영상 끝까지 재생되지 않았습니다")

                # ended 로그(state 10)가 올라갈 시간을 준다.
                time.sleep(3)
                done += 1
                if on_result:
                    on_result(job, True, "ended")
            except Exception as exc:
                print(f"    실패: {exc}")
                failed += 1
                if on_result:
                    on_result(job, False, str(exc))
            finally:
                page.close()

        ctx.close()
        browser.close()

    print(f"\n완료 {done}편, 실패 {failed}편")
    print("재생 종료를 확인했습니다. 별도 진도 동기화로 출석 반영을 다시 확인하세요.")
    return 0 if failed == 0 else 1
