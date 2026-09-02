"""강의안 PDF에서 슬라이드 텍스트를 뽑는다.

pypdf가 있으면 그것을 쓰고, 없으면 PDF 콘텐츠 스트림을 직접 풀어 텍스트를 긁는
폴백을 쓴다 (설치 없이도 최소한 동작하게).
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
    """pypdf 없이 쓰는 최소 추출기 — 페이지 경계는 근사한다."""
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
    """이 강의의 강의안일 수 있는 PDF 후보들.

    제목이 정확히 일치하는 자료를 최우선으로, 그 다음 같은 주차, 그 다음 같은 강좌 순.
    실제 강좌를 돌려 보니 제목 규칙이 강좌마다 달라서(예: VOD는 '2주차 1차시 온라인 강의',
    PDF는 'Lecture 3-MRP.pdf') **위치만으로 고르면 엉뚱한 장을 집는다.**
    그래서 여기서는 후보만 넓게 주고, 실제 선택은 전사 내용과 대조해 결정한다.
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
    """VOD와 같은 제목의 자료(ubfile) PDF를 찾는다.

    실측: 강의안 ubfile의 제목이 VOD 제목과 정확히 같은 강좌가 많아
    (예: 'Lecture 1-1: Logistics' ↔ 같은 이름의 PDF) 제목 매칭이 가장 확실하다.
    실패하면 같은 섹션(주차)의 PDF 중 하나를 고른다.

    반환값은 (blob 경로, 원본 파일명). blob은 sha256 이름이라 표시용 이름이 따로 필요하다.
    """
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
