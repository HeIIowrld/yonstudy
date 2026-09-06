"""LearnUs 자료와 게시글을 OneDrive가 읽기 좋은 폴더로 내보낸다."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .daily import SEOUL


TERM_CODES = {
    "1학기": "1",
    "2학기": "2",
    "여름계절수업": "S",
    "겨울계절수업": "W",
}


def term_folder(year: str, semester: str) -> str:
    return f"{year}-{TERM_CODES.get(semester, semester)}"


def _safe(value: str | None, fallback: str = "이름없음", limit: int = 180) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value or "").strip(" .")
    value = value or fallback
    return value.encode("utf-8")[:limit].decode("utf-8", "ignore").rstrip(" .") or fallback


def _same_file(path: Path, source: Path, size: int | None) -> bool:
    if not path.is_file():
        return False
    if size is not None and path.stat().st_size != size:
        return False
    # copy2가 원본 mtime을 보존한다. 매일 수 GB를 다시 해시하지 않아도 새 blob은
    # 크기 또는 mtime 차이로 다시 복사된다.
    return path.stat().st_mtime_ns == source.stat().st_mtime_ns


@dataclass
class ExportResult:
    destination: str
    courses: int = 0
    material_files: int = 0
    board_attachments: int = 0
    posts: int = 0
    copied_files: int = 0
    copied_bytes: int = 0
    missing_blobs: int = 0


def export_onedrive_tree(
    store,
    destination: str | Path,
    *,
    year: str,
    semester: str,
    dry_run: bool = False,
) -> ExportResult:
    """자료/게시판을 내보낸다. 기존 대상 파일은 지우지 않는 증분 복사다."""
    # flat_layout은 term_folder를 이 모듈에서 가져오므로 순환 import를 피하기 위해
    # 실행 시점에 공통 강의자료 파일명 함수를 불러온다.
    from .flat_layout import resource_filename

    root = Path(destination)
    result = ExportResult(destination=str(root))
    courses = store.query(
        """
        SELECT course_id,year,semester,name,title,slug
          FROM course WHERE year=? AND semester=? ORDER BY name
        """,
        (year, semester),
    )
    result.courses = len(courses)

    for course in courses:
        term = _safe(term_folder(course["year"], course["semester"]))
        course_name = _safe(course["slug"] or course["title"] or course["name"])
        course_dir = root / term / course_name
        if not dry_run:
            for category in ("게시판_첨부", "QNA_공지"):
                (course_dir / category).mkdir(parents=True, exist_ok=True)

        files = store.query(
            """
            SELECT f.id,f.cmid,f.role,f.name,f.sha256,f.bytes,f.saved_at,
                   a.title AS activity_title,a.section_idx,a.section_name,a.open_from
              FROM file f LEFT JOIN activity a ON a.cmid=f.cmid
             WHERE f.course_id=? AND f.role IN ('resource','post')
             ORDER BY f.role,f.cmid,f.name
            """,
            (course["course_id"],),
        )
        for row in files:
            base_name = row["name"] or f"파일_{row['cmid']}"
            if row["role"] == "resource":
                result.material_files += 1
                rel = Path(resource_filename(
                    section_idx=row["section_idx"], section_name=row["section_name"],
                    activity_title=row["activity_title"], name=base_name,
                    file_id=row["id"], open_from=row["open_from"],
                    saved_at=row["saved_at"],
                ))
            else:
                result.board_attachments += 1
                board = _safe(row["activity_title"], f"게시판_{row['cmid']}")
                rel = Path("게시판_첨부") / board / _safe(f"{row['id']}_{base_name}")
            source = store.blob_path(row["sha256"]) if row["sha256"] else None
            if source is None or not source.is_file():
                result.missing_blobs += 1
                continue
            target = course_dir / rel
            if _same_file(target, source, row["bytes"]):
                continue
            result.copied_files += 1
            result.copied_bytes += row["bytes"] or source.stat().st_size
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

        posts = store.query(
            """
            SELECT p.post_id,p.modname,p.subject,p.writer,p.written_at,p.url,p.body,
                   p.cmid,a.title AS board_title
              FROM post p LEFT JOIN activity a ON a.cmid=p.cmid
             WHERE p.course_id=?
               AND NOT (p.modname='forum' AND p.post_id LIKE 't%' AND p.body IS NULL)
             ORDER BY p.cmid,p.written_at,p.id
            """,
            (course["course_id"],),
        )
        result.posts += len(posts)
        for post in posts:
            board = _safe(post["board_title"], f"게시판_{post['cmid']}")
            day = re.sub(r"\D", "", (post["written_at"] or "")[:10]) or "날짜없음"
            filename = _safe(
                f"{day}_{post['post_id']}_{post['subject']}",
                f"글_{post['post_id']}",
                140,
            ) + ".md"
            target = course_dir / "QNA_공지" / board / filename
            body = "\n".join(
                [
                    f"# {post['subject'] or '(제목 없음)'}",
                    "",
                    f"- 과목: {course['title'] or course['name']}",
                    f"- 게시판: {post['board_title'] or post['modname']}",
                    f"- 작성자: {post['writer'] or '알 수 없음'}",
                    f"- 작성일: {post['written_at'] or '알 수 없음'}",
                    f"- 원문: {post['url'] or '없음'}",
                    "",
                    post["body"] or "(본문을 수집하지 못했습니다.)",
                    "",
                ]
            )
            encoded = body.encode("utf-8")
            if target.is_file() and target.read_bytes() == encoded:
                continue
            result.copied_files += 1
            result.copied_bytes += len(encoded)
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(encoded)

    manifest = {
        **asdict(result),
        "year": year,
        "semester": semester,
        "generated_at": datetime.now(SEOUL).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": "incremental-no-delete",
    }
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        (root / "yonstudy-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result
