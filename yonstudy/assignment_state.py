"""사이트 제출 기록과 별개로 사용자의 제출 필요 여부를 해석한다."""

from __future__ import annotations


def is_submission_required(row: dict) -> bool:
    """지정한 과제만 제외한다. 제목에 '팀'이 있다는 이유로 추측하지 않는다."""
    return row.get("submission_requirement") != "not_required"


def submission_requirement_label(row: dict) -> str:
    return "" if is_submission_required(row) else "본인 제출 불필요"
