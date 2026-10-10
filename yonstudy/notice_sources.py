"""현재 수강 과목의 공식 공지와 변경 시점을 함께 읽는다."""

from __future__ import annotations

import re


_NOTICE_BOARD = re.compile(r"공지|announcements?|notices?", re.I)
_QUESTION_BOARD = re.compile(r"질의|질문|문의|q\s*&\s*a|qna", re.I)


def is_notice_board(title: str | None) -> bool:
    return bool(_NOTICE_BOARD.search(title or "")) and not _QUESTION_BOARD.search(title or "")


def current_notice_posts(store, *, year: str, semester: str) -> list[dict]:
    rows = store.query(
        """SELECT p.cmid,p.post_id,p.modname,p.subject AS title,p.body,p.url,
                  p.written_at,p.fetched_at,p.updated_at,c.course_id,c.name AS course_name,
                  a.title AS board_name
             FROM post p JOIN course c ON c.course_id=p.course_id
             LEFT JOIN activity a ON a.cmid=p.cmid
            WHERE c.year=? AND c.semester=? AND c.enrolled=1
              AND COALESCE(a.present,1)=1
              AND NOT (p.modname='forum' AND p.post_id LIKE 't%')""", (year, semester),
    )
    return [dict(row) for row in rows if is_notice_board(row["board_name"])]


def notice_version(row: dict) -> str:
    written = re.findall(r"20\d{2}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?", row.get("written_at") or "")
    return max([value.replace(" ", "T") for value in written]
               + [row.get("updated_at") or "", row.get("fetched_at") or ""])


def notice_excerpt(body: str, limit: int = 500) -> str:
    text = " ".join(body.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
