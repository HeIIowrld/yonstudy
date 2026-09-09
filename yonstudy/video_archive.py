"""현재 학기의 접근 가능한 모든 VOD 원본을 remote에 보관한다."""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import vod as V


@dataclass
class VideoArchiveResult:
    mode: str = "archive-all-course-vods-no-playback"
    eligible: int = 0
    uploaded_files: int = 0
    uploaded_bytes: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    failures: list[dict] = field(default_factory=list)


def candidates(
    store, *, year: str, semester: str,
    course_ids: set[int] | None = None, limit: int | None = None,
) -> list[dict]:
    """현재 수강 중인 강좌에서 접근 가능하고 원본 URL이 확인된 VOD를 고른다.

    원본 다운로드는 LearnUs 플레이어의 진도 기록 API를 호출하지 않으므로 진도 추적
    여부와 무관하게 보관할 수 있다. 아직 공개되지 않았거나 접근이 제한된 영상은
    다음 자동 동기화에서 URL이 확인된 뒤 후보가 된다.
    """
    course_sql = ""
    params: list[object] = [year, semester]
    if course_ids is not None:
        if not course_ids:
            return []
        course_sql = f" AND c.course_id IN ({','.join('?' * len(course_ids))})"
        params.extend(sorted(course_ids))
    sql = f"""
        SELECT v.cmid,v.course_id,v.hls_url,a.title,a.url,a.duration,
               a.section_idx,a.section_name,
               c.year,c.semester,c.slug,c.title AS course_title,c.name AS course_name
          FROM vod v
          JOIN activity a ON a.cmid=v.cmid
          JOIN course c ON c.course_id=v.course_id
         WHERE c.year=? AND c.semester=? AND c.enrolled=1
           AND a.present=1
           AND v.status='ok'
           AND v.hls_url IS NOT NULL AND v.hls_url<>''
           AND COALESCE(a.restricted,0)=0
           {course_sql}
         ORDER BY c.name,a.section_idx,v.cmid
    """
    rows = [dict(row) for row in store.query(sql, tuple(params))]
    return rows[:limit] if limit else rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_course_vods(
    store, sink, *, year: str, semester: str,
    course_ids: set[int] | None = None, limit: int | None = None,
    client=None, dry_run: bool = False, download_fn=V.download,
) -> VideoArchiveResult:
    """접근 가능한 현재 학기 VOD를 임시 MP4로 받아 remote에 전송한다.

    성공 여부는 ``file(role='video')``에 남긴다. 성공한 원격 파일은 다음 실행에서
    크기까지 확인해 건너뛰며, 임시 로컬 파일은 성공/실패와 무관하게 제거한다.
    """
    rows = candidates(
        store, year=year, semester=semester,
        course_ids=course_ids, limit=limit,
    )
    result = VideoArchiveResult(eligible=len(rows))
    temp_root = Path(store.root) / "tmp"
    if not dry_run:
        temp_root.mkdir(parents=True, exist_ok=True)

    for row in rows:
        if not dry_run and row.get("duration"):
            seconds = 0
            for part in str(row["duration"]).split(":"):
                if not part.isdigit():
                    seconds = 0
                    break
                seconds = seconds * 60 + int(part)
            if seconds:
                store.db.execute(
                    "UPDATE vod SET duration_sec=COALESCE(duration_sec,?) WHERE cmid=?",
                    (seconds, row["cmid"]),
                )
        remote_path = sink.video_path(
            year=row["year"], semester=row["semester"],
            course_slug=row["slug"] or row["course_title"] or row["course_name"],
            title=row["title"], cmid=row["cmid"],
            section_idx=row["section_idx"], section_name=row["section_name"],
        )
        stable_url = row["url"]
        existing = store.file_record(stable_url, "video")
        expected_size = existing["bytes"] if existing else None
        if sink.exists(remote_path, expected_size):
            if not dry_run:
                store.save_file({
                    "course_id": row["course_id"], "cmid": row["cmid"],
                    "role": "video", "name": Path(remote_path).name,
                    "url": stable_url,
                    "sha256": existing["sha256"] if existing else None,
                    "bytes": expected_size,
                    "remote_path": remote_path, "remote_status": "ok",
                })
                store.update_file_remote(stable_url, "video", remote_path, "ok")
                store.commit()
            result.skipped_files += 1
            continue
        if dry_run:
            continue

        try:
            with tempfile.TemporaryDirectory(prefix=f"vod-{row['cmid']}-", dir=temp_root) as tmp:
                video_path = Path(tmp) / "source.mp4"
                output = V.Outputs(video=video_path)
                downloaded = download_fn(row["hls_url"], output, cmid=row["cmid"])
                if not downloaded.ok and client is not None:
                    fresh = V.refresh_hls_url(client, row["cmid"])
                    if fresh:
                        store.db.execute(
                            "UPDATE vod SET hls_url=? WHERE cmid=?", (fresh, row["cmid"])
                        )
                        downloaded = download_fn(fresh, output, cmid=row["cmid"])
                if not downloaded.ok or not video_path.is_file():
                    raise RuntimeError(downloaded.error or "영상 파일이 생성되지 않았습니다")

                size = video_path.stat().st_size
                measured_seconds = round(float(getattr(downloaded, "seconds", 0) or 0))
                if measured_seconds:
                    store.db.execute(
                        "UPDATE vod SET duration_sec=? WHERE cmid=?",
                        (measured_seconds, row["cmid"]),
                    )
                digest = _sha256(video_path)
                uploaded = sink.upload_file(remote_path, video_path)
                store.save_file({
                    "course_id": row["course_id"], "cmid": row["cmid"],
                    "role": "video", "name": Path(remote_path).name,
                    "url": stable_url, "sha256": digest, "bytes": size,
                    "remote_path": remote_path, "remote_status": "ok",
                })
                store.update_file_remote(stable_url, "video", remote_path, "ok")
                if uploaded:
                    result.uploaded_files += 1
                    result.uploaded_bytes += size
                else:
                    result.skipped_files += 1
        except Exception as exc:
            store.save_file({
                "course_id": row["course_id"], "cmid": row["cmid"],
                "role": "video", "name": Path(remote_path).name,
                "url": stable_url, "sha256": None, "bytes": None,
                "remote_path": remote_path, "remote_status": "error",
            })
            store.log("video_archive", str(row["cmid"]), False, str(exc))
            result.failed_files += 1
            result.failures.append({
                "cmid": row["cmid"], "title": row["title"], "error": str(exc)[:500],
            })
        finally:
            store.commit()
    return result


# 공개된 0.1 API와 기존 자동화 설정을 깨지 않기 위한 호환 이름이다.
ArchiveOnlyResult = VideoArchiveResult
archive_untracked_vods = archive_course_vods
