"""HLS 강의에서 오디오, 프레임, 영상 원본을 추출한다.

요청한 산출물은 ffmpeg 한 번의 실행으로 만든다. CDN URL이 만료되면
뷰어를 다시 열어 새 URL을 받는다.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .client import LEARNUS, LearnUsClient
from .parse import parse_vod_viewer

AUDIO_EXT = "opus"
AUDIO_ARGS = ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k"]
AUDIO_FORMAT = "opus"  # 임시 파일 확장자로는 컨테이너 추론이 안 되므로 -f로 명시한다.

# 화자의 움직임을 슬라이드 전환으로 오인하지 않도록 최소 간격을 둔다.
SCENE_THRESHOLD = 0.10
MIN_FRAME_GAP = 12.0  # 초. 슬라이드가 12초보다 빨리 넘어가는 강의는 드물다.
FRAME_WIDTH = 1280


class FfmpegMissing(RuntimeError):
    pass


def ensure_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise FfmpegMissing("ffmpeg가 필요합니다: apt-get install -y ffmpeg")
    return path


@dataclass
class Outputs:
    """이번 다운로드에서 무엇을 만들지."""

    audio: Path | None = None
    frames_dir: Path | None = None
    video: Path | None = None
    scene_threshold: float = SCENE_THRESHOLD
    min_frame_gap: float = MIN_FRAME_GAP

    @property
    def anything(self) -> bool:
        return any((self.audio, self.frames_dir, self.video))


@dataclass
class Result:
    cmid: int
    seconds: float = 0.0
    audio_bytes: int = 0
    frame_count: int = 0
    frame_bytes: int = 0
    video_bytes: int = 0
    error: str = ""
    made: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def total_bytes(self) -> int:
        return self.audio_bytes + self.frame_bytes + self.video_bytes


def probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip() or 0)
    except Exception:
        return 0.0


def refresh_hls_url(client: LearnUsClient, cmid: int) -> str | None:
    """CDN 서명이 만료됐을 때 뷰어를 다시 열어 주소를 갱신한다."""
    page = client.request(
        f"{LEARNUS}/mod/vod/viewer.php?id={cmid}", referer=f"{LEARNUS}/course/view.php"
    )
    return parse_vod_viewer(page, cmid).hls_url


def _dir_stats(d: Path, pattern: str = "*.jpg") -> tuple[int, int]:
    files = list(d.glob(pattern)) if d.exists() else []
    return len(files), sum(f.stat().st_size for f in files)


def download(hls_url: str, out: Outputs, cmid: int = 0, timeout: int = 7200) -> Result:
    """HLS 한 번 읽어 요청받은 산출물을 전부 만든다."""
    ensure_ffmpeg()
    res = Result(cmid=cmid)
    if not out.anything:
        res.error = "만들 산출물이 지정되지 않았습니다"
        return res

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", hls_url]
    temps: list[tuple[Path, Path]] = []  # (임시, 최종)

    if out.audio:
        out.audio.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.audio.with_suffix(out.audio.suffix + ".part")
        cmd += ["-map", "0:a", *AUDIO_ARGS, "-f", AUDIO_FORMAT, str(tmp)]
        temps.append((tmp, out.audio))

    if out.frames_dir:
        out.frames_dir.mkdir(parents=True, exist_ok=True)
        for old in out.frames_dir.glob("*.jpg"):
            old.unlink()
        cmd += [
            "-map", "0:v",
            "-vf",
            # prev_selected_t는 첫 프레임에서 NaN이라 gte(t-NaN, gap)이 항상 거짓이 된다.
            # isnan()을 더해 첫 장은 무조건 통과시켜야 한다.
            f"select='gt(scene,{out.scene_threshold})"
            f"*(isnan(prev_selected_t)+gte(t-prev_selected_t,{out.min_frame_gap}))'"
            f",scale={FRAME_WIDTH}:-2",
            "-vsync", "vfr", "-q:v", "6",
            str(out.frames_dir / "%04d.jpg"),
        ]

    if out.video:
        out.video.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.video.with_suffix(out.video.suffix + ".part")
        # 재인코딩 없이 영상·오디오만 담는다. HLS의 timed_id3 데이터 트랙은 MP4가
        # 지원하지 않아 전체 스트림(-map 0)을 복사하면 헤더 생성 단계에서 실패한다.
        cmd += [
            "-map", "0:v", "-map", "0:a?", "-c", "copy", "-f", "mp4", str(tmp),
        ]
        temps.append((tmp, out.video))

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        for tmp, _ in temps:
            tmp.unlink(missing_ok=True)
        res.error = f"시간 초과 ({timeout}초)"
        return res

    if proc.returncode != 0:
        for tmp, _ in temps:
            tmp.unlink(missing_ok=True)
        res.error = (proc.stderr or "ffmpeg 실패").strip()[:200]
        return res

    for tmp, final in temps:
        if tmp.exists():
            tmp.rename(final)

    if out.audio and out.audio.exists():
        res.audio_bytes = out.audio.stat().st_size
        res.seconds = probe_duration(out.audio)
        res.made.append("audio")
    if out.frames_dir:
        res.frame_count, res.frame_bytes = _dir_stats(out.frames_dir)
        if res.frame_count:
            res.made.append(f"frames×{res.frame_count}")
    if out.video and out.video.exists():
        res.video_bytes = out.video.stat().st_size
        res.seconds = res.seconds or probe_duration(out.video)
        res.made.append("video")
    return res


# 산출물 경로


def _safe(title: str, cmid: int) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "_", title)[:60].strip()
    return f"{cmid}_{s}" if s else str(cmid)


def paths_for(store, course_dir: str, cmid: int, title: str) -> dict[str, Path]:
    base = store.root / "courses" / course_dir
    stem = _safe(title, cmid)
    return {
        "audio": base / "audio" / f"{stem}.{AUDIO_EXT}",
        "frames": base / "frames" / stem,
        "video": base / "video" / f"{stem}.mp4",
    }


def plan_outputs(
    paths: dict[str, Path],
    *,
    want_audio: bool = True,
    want_frames: bool = True,
    want_video: bool = False,
    scene_threshold: float = SCENE_THRESHOLD,
) -> Outputs:
    """이미 있는 산출물은 빼고, 아직 없는 것만 요청한다."""
    frames_done = paths["frames"].is_dir() and any(paths["frames"].glob("*.jpg"))
    out = Outputs(scene_threshold=scene_threshold)
    if want_audio and not paths["audio"].exists():
        out.audio = paths["audio"]
    if want_frames and not frames_done:
        out.frames_dir = paths["frames"]
    if want_video and not paths["video"].exists():
        out.video = paths["video"]
    return out


def pending(
    store,
    course_id: int | None = None,
    limit: int | None = None,
    *,
    want_audio: bool = True,
    want_frames: bool = True,
    want_video: bool = False,
) -> list[dict]:
    """아직 만들 산출물이 남은 동영상 목록."""
    sql = """
        SELECT v.cmid, v.hls_url, v.duration_sec, a.title,
               c.year, c.semester, c.slug, c.name AS course_name
          FROM vod v
          JOIN activity a ON a.cmid = v.cmid
          JOIN course   c ON c.course_id = v.course_id
         WHERE v.hls_url IS NOT NULL AND v.hls_url != ''
    """
    args: tuple = ()
    if course_id:
        sql += " AND v.course_id = ?"
        args = (course_id,)
    sql += " ORDER BY c.year DESC, v.duration_sec"

    out = []
    for row in store.query(sql, args):
        r = dict(row)
        r["course_dir"] = f"{r['year']}-{r['semester']}/{r['slug']}"
        r["paths"] = paths_for(store, r["course_dir"], r["cmid"], r["title"])
        r["outputs"] = plan_outputs(
            r["paths"], want_audio=want_audio, want_frames=want_frames, want_video=want_video
        )
        if r["outputs"].anything:
            out.append(r)
        if limit and len(out) >= limit:
            break
    return out


# 다운로드 전에 용량을 가늠하기 위한 분당 대략값이다.
MB_PER_MIN = {"audio": 0.17, "frames": 0.12, "video": 3.20}


def estimate(rows: list[dict]) -> tuple[float, dict[str, float]]:
    """(총 시간, 산출물별 예상 GB). duration이 없는 편은 평균으로 보정한다."""
    known = [r["duration_sec"] for r in rows if r.get("duration_sec")]
    avg = (sum(known) / len(known)) if known else 0
    minutes = sum((r.get("duration_sec") or avg) for r in rows) / 60
    gb = {}
    for key, per_min in MB_PER_MIN.items():
        want = any(
            getattr(r["outputs"], "audio" if key == "audio" else
                    "frames_dir" if key == "frames" else "video")
            for r in rows
        )
        if want:
            gb[key] = minutes * per_min / 1024
    return minutes / 60, gb
