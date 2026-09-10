"""강의안 PDF에서 페이지별 텍스트를 뽑는다.

pypdf가 없으면 콘텐츠 스트림을 직접 읽는 간단한 폴백을 쓴다.
"""

from __future__ import annotations

import logging
import re
import zlib
from pathlib import Path


def extract_pages(pdf_path: str | Path) -> list[str]:
    path = Path(pdf_path)
    try:
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return [(p.extract_text() or "").strip() for p in reader.pages]
    except ImportError:
        return _extract_pages_fallback(path)


_TJ = re.compile(rb"\((?:\\.|[^\\()])*\)")


def _extract_pages_fallback(path: Path) -> list[str]:
    """pypdf 없이 텍스트를 추출한다. 페이지 경계는 근삿값이다."""
    raw = path.read_bytes()
    pages: list[str] = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        chunk = m.group(1)
        try:
            chunk = zlib.decompress(chunk)
        except zlib.error:
            continue
        parts = []
        for t in _TJ.findall(chunk):
            s = t[1:-1].replace(b"\\(", b"(").replace(b"\\)", b")")
            parts.append(s.decode("latin-1", "ignore"))
        text = " ".join(parts).strip()
        if len(text) > 20:
            pages.append(text)
    return pages


def slide_candidates(store, cmid: int, limit: int = 12) -> list[tuple[Path, str]]:
    """이 강의의 강의안일 수 있는 PDF 후보를 찾는다.

    제목 일치, 같은 주차, 같은 강좌 순으로 후보를 넓혀 간다. 실제 선택은 전사
    내용과의 일치도로 정한다.
    """
    row = store.query(
        "SELECT title, course_id, section_idx FROM activity WHERE cmid=?", (cmid,)
    )
    if not row:
        return []
    title, course_id, section = row[0]["title"], row[0]["course_id"], row[0]["section_idx"]

    seen: set[str] = set()
    out: list[tuple[Path, str]] = []

    def collect(sql: str, args: tuple) -> None:
        for r in store.query(sql, args):
            if r["sha256"] in seen or len(out) >= limit:
                continue
            p = store.blob_path(r["sha256"])
            if p.exists():
                seen.add(r["sha256"])
                out.append((p, r["name"]))

    base = """
        SELECT f.sha256, f.name FROM file f JOIN activity a ON a.cmid = f.cmid
         WHERE f.role='resource' AND f.sha256 IS NOT NULL AND lower(f.name) LIKE '%.pdf'
    """
    collect(base + " AND a.course_id=? AND a.title=?", (course_id, title))
    collect(base + " AND a.course_id=? AND a.section_idx=?", (course_id, section))
    collect(base + " AND a.course_id=?", (course_id,))
    return out


def find_slides_for(store, cmid: int) -> tuple[Path, str] | None:
    """같은 제목, 같은 주차 순으로 PDF를 찾아 `(blob 경로, 원본명)`을 반환한다."""
    row = store.query(
        "SELECT title, course_id, section_idx FROM activity WHERE cmid=?", (cmid,)
    )
    if not row:
        return None
    title, course_id, section = row[0]["title"], row[0]["course_id"], row[0]["section_idx"]

    exact = store.query(
        """
        SELECT f.sha256, f.name FROM file f
          JOIN activity a ON a.cmid = f.cmid
         WHERE a.course_id=? AND a.title=? AND f.role='resource' AND f.sha256 IS NOT NULL
        """,
        (course_id, title),
    )
    if not exact:
        exact = store.query(
            """
            SELECT f.sha256, f.name FROM file f
              JOIN activity a ON a.cmid = f.cmid
             WHERE a.course_id=? AND a.section_idx=? AND f.role='resource'
               AND f.sha256 IS NOT NULL AND lower(f.name) LIKE '%.pdf'
            """,
            (course_id, section),
        )
    for r in exact:
        p = store.blob_path(r["sha256"])
        if p.exists():
            return p, r["name"]
    return None
