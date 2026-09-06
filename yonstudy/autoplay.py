"""자동수강 — 열람기간이 열린 미시청 영상을 실제로 재생해 진도를 채운다.

## 진도가 어떻게 기록되는지 (mod_vod/vod AMD 모듈 실측, 2026-08-02)

    k.on("timeupdate", function(){
        ...
        if (1 != k.paused() && l.current >= l.max) l.max = Math.floor(l.current);
    })
    c.ajax = function(state, from, to){
        if (c.isProgress && c.cmid>0 && c.isLogin && c.isProgressPeriodCheck) {
            $.post("/mod/vod/action.php", {type:"vod_log", track, attempt, state, positionfrom, positionto, logtime})
        }
    }

즉 진도는 **`video.currentTime`의 최댓값(`l.max`)** 하나로 결정된다. 서버로 올라가는
페이로드에도 경과 실시간(wall clock)이 들어가지 않는다. 따라서 **2배속으로 봐도
진도는 100% 그대로 인정된다** — A/B 파일럿 없이 코드로 확정된 사실이다.

제약도 같은 코드에서 확인했다:
  * `isProgressPeriodCheck`(progress_period)가 false면 `ajax()` 자체가 no-op —
    진도처리기간이 아니면 아무리 재생해도 기록되지 않는다.
  * 배속 상한은 `rate_max`. 초과하면 플레이어가 강제로 되돌리고 경고를 띄운다.
  * state 코드: 1=진입, 3=재생, 2=일시정지/이동, 10=종료, 99=창 닫기.

## 이 모듈이 하지 않는 것

`/mod/vod/action.php`를 직접 호출해 진도를 만들어내지 않는다. 실제 재생만 한다.
로그 위조는 기록 조작이고, 이 도구의 범위 밖이다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

VIEWER = "https://ys.learnus.org/mod/vod/viewer.php?id={cmid}"
SEOUL = ZoneInfo("Asia/Seoul")


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

    진도 추적 영상은 100% 미만이면서 현재 진도처리기간인 경우만 고른다.
    ``include_untracked_once``를 켜면 진도율·완료 체크가 없는 영상도 공개 후 한 번
    완주 대상으로 넣는다. 성공 기록은 ``crawl_log(kind='playback_once')``로 판정한다.
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
        course_clause = f" AND v.course_id IN ({','.join('?' * len(course_ids))})"
        params = tuple(sorted(course_ids))
    rows = store.query(
        f"""
        SELECT v.cmid, v.course_id, v.duration_sec, v.watched_sec, v.progress_pct,
               v.can_log_progress, v.max_rate, v.is_progress, v.status,
               a.title, a.open_from, a.open_to, a.late_until,
               c.name AS course_name,
               EXISTS(
                   SELECT 1 FROM crawl_log l
                    WHERE l.kind='playback_once' AND l.ref=CAST(v.cmid AS TEXT) AND l.ok=1
               ) AS played_once
          FROM vod v
          JOIN activity a ON a.cmid = v.cmid
          JOIN course   c ON c.course_id = v.course_id
         WHERE COALESCE(v.progress_pct, 0) < 100
         {course_clause}
        """,
        params,
    )

    jobs: list[Job] = []
    for r in rows:
        duration = r["duration_sec"] or 0
        if duration <= 0:
            continue

        start = _dt(r["open_from"])
        end = _dt((r["late_until"] if include_late else None) or r["open_to"])
        in_calendar = (not start or start <= now) and (not end or now <= end)
        tracks_progress = r["is_progress"] != 0
        if not tracks_progress:
            # 완료 신호가 없는 영상은 뷰어가 실제로 열리고, 아직 로컬 완주 기록이
            # 없으며, 공개일이 지난 경우 한 번만 재생한다. 마감 표시는 없는 경우가 많다.
            if (
                not include_untracked_once
                or r["status"] != "ok"
                or bool(r["played_once"])
                or (start is not None and start > now)
            ):
                continue
            open_now = True
        # 뷰어의 판정과 화면의 정상 수강기간을 함께 만족해야 한다. 기간 표기가
        # 아예 없는 강좌만 뷰어 판정에 전적으로 의존한다. 기본값은 지각기간 제외다.
        elif r["can_log_progress"] is not None:
            open_now = bool(r["can_log_progress"]) and in_calendar
        elif start and end:
            open_now = start <= now <= end
        else:
            open_now = False
        if not open_now:
            continue

        rate = min(float(r["max_rate"] or 1.0), max_rate_cap)
        if not tracks_progress:
            urgency, deadline = "1회 재생", None
        elif end is None:
            urgency, deadline = "상시", None
        else:
            left = end - now
            deadline = end
            urgency = (
                "긴급" if left < timedelta(days=1)
                else "임박" if left < timedelta(days=3)
                else "여유"
            )
        jobs.append(
            Job(
                cmid=r["cmid"], course_id=r["course_id"],
                course_name=r["course_name"], title=r["title"],
                duration_sec=duration, watched_sec=r["watched_sec"] or 0,
                open_from=r["open_from"], open_to=r["open_to"],
                late_until=r["late_until"], rate=rate,
                urgency=urgency, deadline=deadline,
                tracks_progress=tracks_progress,
            )
        )

    order = {"긴급": 0, "임박": 1, "여유": 2, "상시": 3, "1회 재생": 4}
    jobs.sort(key=lambda j: (order[j.urgency], j.deadline or datetime.max, -j.remaining_sec))
    return jobs


def upcoming(store, now: datetime | None = None, days: int = 14) -> list[dict]:
    """아직 열리지 않았지만 곧 열리는 영상 — 열리는 날 자동 편입 예약용."""
    if now is None:
        now = datetime.now(SEOUL).replace(tzinfo=None)
    elif now.tzinfo is not None:
        now = now.astimezone(SEOUL).replace(tzinfo=None)
    horizon = now + timedelta(days=days)
    out = []
    for r in store.query(
        """
        SELECT a.cmid, a.title, a.open_from, a.open_to, c.name AS course_name
          FROM activity a JOIN course c ON c.course_id = a.course_id
          LEFT JOIN vod v ON v.cmid = a.cmid
         WHERE a.modname='vod' AND a.open_from IS NOT NULL
           AND COALESCE(v.progress_pct, 0) < 100
        """
    ):
        start = _dt(r["open_from"])
        if start and now < start <= horizon:
            out.append(dict(r))
    out.sort(key=lambda x: x["open_from"])
    return out


# --------------------------------------------------------------------------
# 재생 워커
# --------------------------------------------------------------------------

PLAYWRIGHT_HELP = """\
Playwright가 필요합니다:

    /root/yonstudy/.venv/bin/pip install playwright
    /root/yonstudy/.venv/bin/playwright install chromium

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
# console.log를 건드리지 않는다 — viewer 페이지에 devtools 탐지 트랩이 있어서
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
