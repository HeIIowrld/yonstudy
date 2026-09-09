"""수업 시간표를 이용해 외부 녹음 파일을 과목별로 자동 분류한다."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import time as time_module
from calendar import monthrange
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .export import term_folder
from .flat_layout import canonical_filename

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


DEFAULT_EXTENSIONS = {
    ".3gp", ".aac", ".amr", ".caf", ".flac", ".m4a", ".mp3", ".mp4",
    ".mov", ".ogg", ".opus", ".wav", ".webm", ".wma",
}
WEEKDAY_NAMES = ("월", "화", "수", "목", "금", "토", "일")
_WEEKDAYS = {
    "월": 0, "월요일": 0, "mon": 0, "monday": 0,
    "화": 1, "화요일": 1, "tue": 1, "tues": 1, "tuesday": 1,
    "수": 2, "수요일": 2, "wed": 2, "wednesday": 2,
    "목": 3, "목요일": 3, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "금": 4, "금요일": 4, "fri": 4, "friday": 4,
    "토": 5, "토요일": 5, "sat": 5, "saturday": 5,
    "일": 6, "일요일": 6, "sun": 6, "sunday": 6,
}


@dataclass(frozen=True)
class MediaInfo:
    captured_at: datetime
    timestamp_source: str
    duration_sec: float
    metadata_title: str | None = None


@dataclass(frozen=True)
class Match:
    status: str
    course_id: int | None = None
    slot_id: int | None = None
    course_name: str | None = None
    method: str | None = None
    confidence: float = 0.0
    week: int = 0
    lesson: int = 0
    details: dict = field(default_factory=dict)


@dataclass
class TimetableImportResult:
    source: str
    slots: int
    courses: int


@dataclass
class ClassificationResult:
    discovered: int = 0
    pending: int = 0
    skipped: int = 0
    reclassified: int = 0
    matched: int = 0
    ambiguous: int = 0
    unclassified: int = 0
    imported: int = 0
    moved: int = 0
    failed: int = 0
    items: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _normal(value: object) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", str(value or "").lower())


def _parse_date(value: object, label: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}은 YYYY-MM-DD 형식이어야 합니다: {value!r}") from exc


def _term_dates(year: object, semester: object) -> tuple[str, str] | None:
    if not year or not semester:
        return None
    number = int(str(year))
    value = str(semester)
    if value == "1학기":
        return f"{number}-03-01", f"{number}-06-30"
    if value == "여름계절수업":
        return f"{number}-07-01", f"{number}-08-31"
    if value == "2학기":
        return f"{number}-09-01", f"{number}-12-31"
    if value == "겨울계절수업":
        following = number + 1
        return f"{following}-01-01", f"{following}-02-{monthrange(following, 2)[1]:02d}"
    return None


def _parse_clock(value: object, label: str) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"\d{3,4}", raw):
        raw = raw.zfill(4)
        raw = f"{raw[:2]}:{raw[2:]}"
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", raw)
    if not match:
        raise ValueError(f"{label}은 HH:MM 형식이어야 합니다: {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"{label} 시간이 범위를 벗어났습니다: {value!r}")
    return f"{hour:02d}:{minute:02d}"


def _weekday(value: object) -> int:
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ValueError(f"요일 숫자는 월=0 .. 일=6 범위여야 합니다: {value}")
    raw = str(value or "").strip().lower().replace(".", "")
    if raw.isdigit():
        return _weekday(int(raw))
    if raw not in _WEEKDAYS:
        raise ValueError(f"알 수 없는 요일입니다: {value!r}")
    return _WEEKDAYS[raw]


def _day_values(row: dict) -> list[object]:
    value = row.get("weekdays", row.get("days", row.get("weekday", row.get("day"))))
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [part for part in re.split(r"[,/|·\s]+", str(value)) if part]


def _load_timetable(path: Path) -> tuple[dict, list[dict]]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return {}, [dict(row) for row in csv.DictReader(handle)]
    if path.suffix.lower() == ".toml":
        if tomllib is None:
            raise ValueError("Python 3.10에서 TOML 시간표를 쓰려면 `pip install tomli`가 필요합니다.")
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"시간표 TOML을 읽을 수 없습니다: {exc}") from exc
    else:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"시간표 JSON을 읽을 수 없습니다: {exc}") from exc
    if isinstance(payload, list):
        return {}, payload
    if not isinstance(payload, dict):
        raise ValueError("시간표 JSON은 객체 또는 수업 목록이어야 합니다.")
    rows = payload.get(
        "classes",
        payload.get("class", payload.get("slots", payload.get("timetable"))),
    )
    if not isinstance(rows, list):
        raise ValueError("시간표 JSON에 classes(또는 slots) 목록이 없습니다.")
    return payload, rows


def _course_aliases(row: dict) -> list[str]:
    return [
        str(row.get(key) or "")
        for key in ("name", "title", "code", "slug")
        if row.get(key)
    ]


def _resolve_course(store, raw: dict, *, year: str | None, semester: str | None) -> dict:
    course_id = raw.get("course_id")
    clauses, args = ["1=1"], []
    if course_id not in (None, ""):
        clauses.append("course_id=?")
        try:
            args.append(int(course_id))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"course_id가 정수가 아닙니다: {course_id!r}") from exc
    if year:
        clauses.append("year=?")
        args.append(str(year))
    if semester:
        clauses.append("semester=?")
        args.append(semester)
    candidates = [
        dict(row) for row in store.query(
            "SELECT course_id,year,semester,name,title,code,slug FROM course WHERE "
            + " AND ".join(clauses),
            tuple(args),
        )
    ]
    if course_id not in (None, ""):
        if len(candidates) != 1:
            raise ValueError(f"아카이브에서 course_id={course_id} 강좌를 찾지 못했습니다.")
        return candidates[0]

    hint = raw.get("course", raw.get("course_name", raw.get("name", raw.get("code"))))
    needle = _normal(hint)
    if not needle:
        raise ValueError("각 수업에는 course_id 또는 course 이름/코드가 필요합니다.")

    scored: list[tuple[int, dict]] = []
    for course in candidates:
        aliases = [_normal(alias) for alias in _course_aliases(course)]
        exact = any(needle == alias for alias in aliases)
        partial = any(needle in alias or alias in needle for alias in aliases if alias)
        if exact or partial:
            scored.append((100 if exact else 60, course))
    if not scored:
        raise ValueError(f"아카이브 강좌와 매칭되지 않습니다: {hint!r}")
    best_score = max(score for score, _ in scored)
    best = [course for score, course in scored if score == best_score]
    if len(best) != 1:
        names = ", ".join(str(course.get("name") or course["course_id"]) for course in best)
        raise ValueError(f"강좌 이름이 모호합니다: {hint!r} → {names}. course_id를 써 주세요.")
    return best[0]


def import_timetable(
    store,
    path: str | Path,
    *,
    year: str | None = None,
    semester: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    commit: bool = True,
) -> TimetableImportResult:
    """TOML/JSON/CSV 시간표를 저장한다. 같은 파일을 다시 읽으면 해당 원본만 교체한다."""
    path = Path(path).expanduser().resolve()
    defaults, raw_rows = _load_timetable(path)
    year = str(year or defaults.get("year") or "") or None
    semester = semester or defaults.get("semester")
    term_range = _term_dates(year, semester)
    default_from = valid_from or defaults.get("valid_from") or defaults.get("start_date")
    default_to = valid_to or defaults.get("valid_to") or defaults.get("end_date")
    if not default_from and term_range:
        default_from = term_range[0]
    if not default_to and term_range:
        default_to = term_range[1]
    default_from = _parse_date(default_from or "1900-01-01", "valid_from")
    default_to = _parse_date(default_to or "9999-12-31", "valid_to")

    prepared_by_key: dict[tuple, dict] = {}
    for index, raw in enumerate(raw_rows, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"{index}번째 수업이 객체가 아닙니다.")
        course = _resolve_course(store, raw, year=year, semester=semester)
        days = _day_values(raw)
        if not days:
            raise ValueError(f"{index}번째 수업에 weekday/days가 없습니다.")
        start = _parse_clock(raw.get("start", raw.get("starts_at")), "start")
        end = _parse_clock(raw.get("end", raw.get("ends_at")), "end")
        row_from = _parse_date(raw.get("valid_from") or default_from, "valid_from")
        row_to = _parse_date(raw.get("valid_to") or default_to, "valid_to")
        if row_from > row_to:
            raise ValueError(f"{index}번째 수업의 valid_from이 valid_to보다 늦습니다.")
        for day in days:
            prepared = {
                "course_id": course["course_id"],
                "weekday": _weekday(day),
                "starts_at": start,
                "ends_at": end,
                "valid_from": row_from,
                "valid_to": row_to,
                "location": raw.get("location"),
            }
            key = tuple(prepared[name] for name in (
                "course_id", "weekday", "starts_at", "ends_at", "valid_from", "valid_to"
            ))
            prepared_by_key[key] = prepared

    prepared_rows = list(prepared_by_key.values())

    source = str(path)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        existing = {
            (
                row["course_id"], row["weekday"], row["starts_at"], row["ends_at"],
                row["valid_from"], row["valid_to"],
            ): row["id"]
            for row in store.query("SELECT * FROM timetable_slot WHERE source=?", (source,))
        }
        for key, slot_id in existing.items():
            if key not in prepared_by_key:
                store.db.execute("DELETE FROM timetable_slot WHERE id=?", (slot_id,))
        for row in prepared_rows:
            store.db.execute(
                """
                INSERT INTO timetable_slot
                    (course_id,weekday,starts_at,ends_at,valid_from,valid_to,
                     location,source,imported_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(course_id,weekday,starts_at,ends_at,valid_from,valid_to,source)
                DO UPDATE SET location=excluded.location,imported_at=excluded.imported_at
                """,
                (*row.values(), source, now),
            )
        if commit:
            store.commit()
    except Exception:
        store.db.rollback()
        raise
    return TimetableImportResult(
        source=source,
        slots=len(prepared_rows),
        courses=len({row["course_id"] for row in prepared_rows}),
    )


def _local_datetime(
    parts: tuple[str, ...], timezone: ZoneInfo, period: str = ""
) -> datetime | None:
    try:
        year, month, day, hour, minute = (int(value) for value in parts[:5])
        second = int(parts[5]) if len(parts) > 5 and parts[5] else 0
        normalized_period = period.lower()
        if normalized_period in {"오후", "pm"} and hour < 12:
            hour += 12
        elif normalized_period in {"오전", "am"} and hour == 12:
            hour = 0
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone)
    except (TypeError, ValueError):
        return None


def timestamp_from_filename(path: str | Path, timezone: ZoneInfo) -> datetime | None:
    """휴대폰/녹음 앱이 흔히 쓰는 날짜 파일명에서 로컬 녹음 시작 시각을 읽는다."""
    stem = Path(path).stem
    compact = re.search(
        r"(?<!\d)(20\d{2})(\d{2})(\d{2})[T _.-]?(\d{2})(\d{2})(\d{2})?(?!\d)",
        stem,
    )
    if compact:
        return _local_datetime(compact.groups(), timezone)
    separated = re.search(
        r"(?<!\d)(20\d{2})[-_.년 ]+(\d{1,2})[-_.월 ]+(\d{1,2})(?:일)?"
        r"(?:[ T_]+|\s*)(오전|오후|am|pm)?\s*(\d{1,2})[:시._-]+\s*(\d{2})"
        r"(?:[:분._-]+\s*(\d{2}))?",
        stem,
        re.I,
    )
    if separated:
        year, month, day, period, hour, minute, second = separated.groups()
        return _local_datetime(
            (year, month, day, hour, minute, second or "0"),
            timezone,
            period or "",
        )
    return None


def _metadata_datetime(value: object, timezone: ZoneInfo) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def probe_media(path: str | Path) -> tuple[object | None, float, str | None]:
    """ffprobe가 있으면 컨테이너 생성시각과 길이를 읽고, 없으면 조용히 폴백한다."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries",
                "format=duration:format_tags=creation_time,title:"
                "stream_tags=creation_time,title",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return None, 0.0, None
        payload = json.loads(result.stdout or "{}")
        fmt = payload.get("format") or {}
        creation = (fmt.get("tags") or {}).get("creation_time")
        title = (fmt.get("tags") or {}).get("title")
        if not creation:
            for stream in payload.get("streams") or []:
                creation = (stream.get("tags") or {}).get("creation_time")
                title = title or (stream.get("tags") or {}).get("title")
                if creation:
                    break
        try:
            duration = max(0.0, float(fmt.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0.0
        # 시간대 변환은 호출자가 수행한다.
        return creation, duration, str(title).strip() if title else None
    except (FileNotFoundError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None, 0.0, None


def inspect_recording(path: str | Path, timezone: ZoneInfo) -> MediaInfo:
    path = Path(path)
    creation, duration, title = probe_media(path)
    captured = _metadata_datetime(creation, timezone)
    source = "metadata"
    if captured is None:
        captured = timestamp_from_filename(path, timezone)
        source = "filename"
    if captured is None:
        captured = datetime.fromtimestamp(path.stat().st_mtime, timezone)
        source = "mtime"
    return MediaInfo(captured, source, duration, title)


def _clock(value: str) -> time:
    return time.fromisoformat(value)


def _slot_interval(slot: dict, observed: datetime) -> tuple[datetime, datetime] | None:
    # 자정을 넘기는 수업은 관측일 전날 시작했을 수도 있다.
    for day in (observed.date(), observed.date() - timedelta(days=1)):
        if day.weekday() != int(slot["weekday"]):
            continue
        if not (
            date.fromisoformat(slot["valid_from"])
            <= day
            <= date.fromisoformat(slot["valid_to"])
        ):
            continue
        start = datetime.combine(day, _clock(slot["starts_at"]), observed.tzinfo)
        end = datetime.combine(day, _clock(slot["ends_at"]), observed.tzinfo)
        if end <= start:
            end += timedelta(days=1)
        return start, end
    return None


def _distance_seconds(observed: datetime, start: datetime, end: datetime) -> float:
    if observed < start:
        return (start - observed).total_seconds()
    if observed > end:
        return (observed - end).total_seconds()
    return 0.0


def _course_hint_ids(courses: list[dict], filename: str) -> set[int]:
    haystack = _normal(Path(filename).stem)
    found: set[int] = set()
    for course in courses:
        for alias in _course_aliases(course):
            needle = _normal(alias)
            if len(needle) >= 3 and needle in haystack:
                found.add(int(course["course_id"]))
                break
    return found


def _week_and_lesson(store, slot: dict, captured_at: datetime) -> tuple[int, int]:
    first = date.fromisoformat(slot["valid_from"])
    occurrence = captured_at.date()
    # 자정을 넘긴 수업은 녹음일이 아니라 실제 수업 시작일을 기준으로 주차를 센다.
    if occurrence.weekday() != int(slot["weekday"]):
        previous = occurrence - timedelta(days=1)
        if previous.weekday() == int(slot["weekday"]):
            occurrence = previous
    # 같은 월·수 수업이 언제나 같은 n주차에 속하도록 학기 시작일이 포함된
    # 월요일을 주차 경계로 사용한다.
    week_start = first - timedelta(days=first.weekday())
    week = 0 if first.year <= 1900 else max(1, (occurrence - week_start).days // 7 + 1)
    weekly_slots = {
        (int(row["weekday"]), row["starts_at"], row["ends_at"])
        for row in store.query(
            """
            SELECT weekday,starts_at,ends_at FROM timetable_slot
             WHERE course_id=? AND valid_from=? AND valid_to=?
            """,
            (slot["course_id"], slot["valid_from"], slot["valid_to"]),
        )
    }
    ordered = sorted(weekly_slots)
    current = (int(slot["weekday"]), slot["starts_at"], slot["ends_at"])
    lesson = ordered.index(current) + 1 if current in ordered else 1
    return week, lesson


def match_recording(
    store,
    captured_at: datetime,
    filename: str,
    *,
    timestamp_source: str = "filename",
    grace_minutes: int = 20,
) -> Match:
    """녹음 시작 시각과 시간표를 비교한다. 모호한 결과는 강제로 한 과목에 넣지 않는다."""
    slots = [
        dict(row) for row in store.query(
            """
            SELECT t.*,c.name,c.title,c.code,c.slug,c.year,c.semester
              FROM timetable_slot t JOIN course c ON c.course_id=t.course_id
             WHERE c.enrolled=1
            """
        )
    ]
    courses_by_id = {int(slot["course_id"]): slot for slot in slots}
    # 시간표에 없는 강좌도 파일명 단독 폴백 대상으로 포함한다.
    for row in store.query(
        "SELECT course_id,name,title,code,slug,year,semester FROM course WHERE enrolled=1"
    ):
        courses_by_id.setdefault(int(row["course_id"]), dict(row))
    courses = list(courses_by_id.values())
    hints = _course_hint_ids(courses, filename)
    grace = max(0, grace_minutes) * 60
    candidates: list[dict] = []
    for slot in slots:
        interval = _slot_interval(slot, captured_at)
        if not interval:
            continue
        start, end = interval
        distance = _distance_seconds(captured_at, start, end)
        if distance <= grace:
            relation = (
                "upcoming" if captured_at < start
                else "ended" if captured_at > end
                else "in_class"
            )
            candidates.append({
                "slot": slot,
                "distance": distance,
                "start": start,
                "end": end,
                "relation": relation,
                "hinted": int(slot["course_id"]) in hints,
            })
    candidates.sort(
        key=lambda item: (
            item["distance"],
            {"in_class": 0, "upcoming": 1, "ended": 2}[item["relation"]],
            not item["hinted"],
            item["slot"]["id"],
        )
    )

    if candidates:
        distinct_courses = {int(item["slot"]["course_id"]) for item in candidates}
        hinted_candidates = [item for item in candidates if item["hinted"]]
        chosen = None
        method = "timetable"
        if len(distinct_courses) == 1:
            chosen = candidates[0]
        elif hinted_candidates and len({
            int(item["slot"]["course_id"]) for item in hinted_candidates
        }) == 1:
            chosen = hinted_candidates[0]
            method = "timetable+filename"
        elif len(candidates) == 1 or (
            len(candidates) > 1
            and candidates[1]["distance"] - candidates[0]["distance"] >= 10 * 60
        ):
            chosen = candidates[0]
        elif candidates[0]["relation"] == "upcoming":
            # 직전 수업 종료와 다음 수업 시작의 정확한 중간(예: 13:55)은
            # 녹음을 미리 켠 것으로 보고 다음 수업에 배정한다. 같은 시각에
            # 시작할 수업이 여럿이면 기존처럼 모호 상태를 유지한다.
            nearest = [
                item for item in candidates
                if item["distance"] == candidates[0]["distance"]
            ]
            upcoming = [item for item in nearest if item["relation"] == "upcoming"]
            if len(upcoming) == 1 and all(
                item["relation"] in {"upcoming", "ended"} for item in nearest
            ):
                chosen = upcoming[0]
                method = "timetable+upcoming"
        if chosen is not None:
            slot = chosen["slot"]
            week, lesson = _week_and_lesson(store, slot, captured_at)
            in_class = chosen["distance"] == 0
            source_cap = {"filename": 0.98, "metadata": 0.92, "mtime": 0.65}.get(
                timestamp_source, 0.70
            )
            time_score = (
                0.94
                if in_class
                else max(
                    0.70,
                    0.90 - chosen["distance"] / max(grace, 1) * 0.20,
                )
            )
            confidence = min(source_cap, time_score) + (
                0.02 if method.endswith("filename") else 0
            )
            return Match(
                status="matched",
                course_id=int(slot["course_id"]),
                slot_id=int(slot["id"]),
                course_name=slot.get("name") or slot.get("title"),
                method=method,
                confidence=round(min(confidence, 0.99), 2),
                week=week,
                lesson=lesson,
                details={
                    "slot": (
                        f"{WEEKDAY_NAMES[int(slot['weekday'])]} "
                        f"{slot['starts_at']}-{slot['ends_at']}"
                    ),
                    "distance_minutes": round(chosen["distance"] / 60, 1),
                    "timing": chosen["relation"],
                },
            )

        return Match(
            status="ambiguous",
            method="timetable",
            details={
                "candidates": [
                    {
                        "course_id": int(item["slot"]["course_id"]),
                        "course": item["slot"].get("name") or item["slot"].get("title"),
                        "slot": f"{item['slot']['starts_at']}-{item['slot']['ends_at']}",
                    }
                    for item in candidates[:5]
                ]
            },
        )

    if len(hints) == 1:
        course_id = next(iter(hints))
        course = courses_by_id[course_id]
        return Match(
            status="matched",
            course_id=course_id,
            course_name=course.get("name") or course.get("title"),
            method="filename",
            confidence=0.70,
            details={"note": "시간표 구간 밖이지만 파일명에서 과목을 하나로 식별함"},
        )
    if len(hints) > 1:
        return Match(
            status="ambiguous",
            method="filename",
            details={"candidate_course_ids": sorted(hints)},
        )
    return Match(
        status="unclassified",
        details={"note": "녹음 시각에 해당하는 수업이나 파일명 과목 단서가 없음"},
    )


def discover_recordings(
    sources: list[str | Path],
    *,
    recursive: bool = True,
    extensions: set[str] | None = None,
    store_root: str | Path | None = None,
) -> list[Path]:
    extensions = {value.lower() for value in (extensions or DEFAULT_EXTENSIONS)}
    generated_roots: list[Path] = []
    if store_root:
        root = Path(store_root).resolve()
        generated_roots = [
            (root / "blobs").resolve(),
            (root / "courses").resolve(),
            (root / "recordings").resolve(),
        ]
    found: dict[str, Path] = {}
    for source_value in sources:
        source = Path(source_value).expanduser()
        if source.is_file():
            candidates = [source]
        elif source.is_dir():
            candidates = source.rglob("*") if recursive else source.glob("*")
        else:
            raise FileNotFoundError(f"녹음 경로를 찾을 수 없습니다: {source}")
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            resolved = path.resolve()
            if any(resolved == root or root in resolved.parents for root in generated_roots):
                continue
            found[str(resolved)] = resolved
    return sorted(found.values(), key=lambda item: str(item).lower())


def _safe_name(value: str, limit: int = 120) -> str:
    from .filename_normalization import nfc

    value = nfc(re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value)).strip(" ._") or "녹음"
    return value.encode("utf-8")[:limit].decode("utf-8", "ignore").rstrip(" ._") or "녹음"


def _target_for(
    store,
    source: Path,
    media: MediaInfo,
    match: Match,
    digest: str,
    destination: Path | None,
) -> Path:
    stamp = media.captured_at.strftime("%Y-%m-%d_%H%M%S")
    filename = canonical_filename(
        week=match.week,
        lesson=match.lesson,
        kind="강의녹음",
        title=media.captured_at.strftime("%Y%m%d_%H%M"),
        stable_id=f"r{digest[:8]}",
        extension=source.suffix.lower(),
    )
    if match.status == "matched" and match.course_id is not None:
        rows = store.query(
            "SELECT year,semester,slug,title,name FROM course WHERE course_id=?",
            (match.course_id,),
        )
        if rows:
            course = rows[0]
            course_name = _safe_name(course["slug"] or course["title"] or course["name"])
            if destination:
                base = destination / term_folder(
                    course["year"], course["semester"]
                ) / course_name
            else:
                base = (
                    store.root
                    / "courses"
                    / f"{course['year']}-{course['semester']}"
                    / course_name
                    / "recordings"
                )
        else:
            base = store.root / "recordings" / "unclassified"
    else:
        if destination:
            base = destination / "unmatched" / "recordings"
        else:
            base = store.root / "recordings" / (
                "ambiguous" if match.status == "ambiguous" else "unclassified"
            )
        filename = f"{stamp}__{_safe_name(source.stem)}__r{digest[:8]}{source.suffix.lower()}"
    target = base / filename
    blob = store.blob_path(digest)
    if target.exists():
        try:
            if os.path.samefile(target, blob):
                return target
        except OSError:
            pass
        target = target.with_name(f"{target.stem}__{digest[:8]}{target.suffix}")
    return target


def _link_blob(store, digest: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    blob = store.blob_path(digest)
    try:
        os.link(blob, target)
    except OSError:
        shutil.copy2(blob, target)


def _remove_old_managed_link(
    store,
    old_path: str | None,
    target: Path,
    digest: str,
    destination: Path | None,
) -> None:
    if not old_path:
        return
    roots = [store.root.resolve()]
    if destination:
        roots.append(destination.resolve())
    old_value = Path(old_path)
    old = (
        old_value.resolve()
        if old_value.is_absolute()
        else (store.root / old_value).resolve()
    )
    try:
        if not any(old == root or root in old.parents for root in roots):
            return
        if old == target.resolve() or not old.is_file():
            return
        blob = store.blob_path(digest)
        try:
            same_content = os.path.samefile(old, blob)
        except OSError:
            same_content = False
        if not same_content:
            check = hashlib.sha256()
            with old.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    check.update(chunk)
            same_content = check.hexdigest() == digest
        if not same_content:
            return
        old.unlink()
    except (OSError, ValueError):
        return


def reclassify_unmatched(
    store,
    *,
    destination: str | Path | None = None,
    grace_minutes: int = 20,
) -> int:
    """시간표가 나중에 추가된 경우 저장된 미분류 blob을 다시 과목에 연결한다."""
    destination_path = Path(destination).expanduser().resolve() if destination else None
    changed = 0
    rows = store.query(
        "SELECT * FROM recording WHERE match_status IN ('ambiguous','unclassified')"
    )
    for original in rows:
        row = dict(original)
        try:
            captured_at = datetime.fromisoformat(row["captured_at"])
        except (TypeError, ValueError):
            continue
        match = match_recording(
            store,
            captured_at,
            " ".join(filter(None, (row.get("original_name"), row.get("metadata_title")))),
            timestamp_source=row.get("timestamp_source") or "metadata",
            grace_minutes=grace_minutes,
        )
        if match.status != "matched":
            continue
        blob = store.blob_path(row["sha256"])
        if not blob.is_file():
            continue
        source = Path(row.get("original_name") or "recording.m4a")
        media = MediaInfo(
            captured_at=captured_at,
            timestamp_source=row.get("timestamp_source") or "metadata",
            duration_sec=float(row.get("duration_sec") or 0),
            metadata_title=row.get("metadata_title"),
        )
        target = _target_for(
            store,
            source,
            media,
            match,
            row["sha256"],
            destination_path,
        )
        _link_blob(store, row["sha256"], target)
        try:
            stored_path = str(target.relative_to(store.root))
        except ValueError:
            stored_path = str(target)
        store.db.execute(
            """
            UPDATE recording
               SET course_id=?,timetable_slot_id=?,week=?,lesson=?,
                   match_status=?,match_method=?,confidence=?,path=?,
                   details_json=?,updated_at=?
             WHERE id=?
            """,
            (
                match.course_id,
                match.slot_id,
                match.week,
                match.lesson,
                match.status,
                match.method,
                match.confidence,
                stored_path,
                json.dumps(match.details, ensure_ascii=False),
                datetime.now().astimezone().isoformat(timespec="seconds"),
                row["id"],
            ),
        )
        store.commit()
        _remove_old_managed_link(
            store,
            row.get("path"),
            target,
            row["sha256"],
            destination_path,
        )
        changed += 1
    return changed


def _read_scan_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_scan_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _ready_after_stability_check(
    store,
    paths: list[Path],
    *,
    state_path: Path,
    stable_seconds: int,
    now: float,
) -> tuple[list[Path], int, int, dict]:
    previous = _read_scan_state(state_path).get("files") or {}
    current: dict[str, dict] = {}
    ready: list[Path] = []
    pending = skipped = 0
    for path in paths:
        stat = path.stat()
        key = str(path)
        fingerprint = {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        old = previous.get(key) if isinstance(previous, dict) else None
        same = bool(
            isinstance(old, dict)
            and old.get("bytes") == stat.st_size
            and old.get("mtime_ns") == stat.st_mtime_ns
        )
        already = store.query(
            """
            SELECT 1 FROM recording
             WHERE source_path=? AND source_bytes=? AND source_mtime_ns=?
             LIMIT 1
            """,
            (key, stat.st_size, stat.st_mtime_ns),
        )
        if already or (same and old.get("processed")):
            current[key] = {
                **fingerprint,
                "stable_since": (old or {}).get("stable_since", now),
                "processed": True,
            }
            skipped += 1
            continue
        stable_since = float(old.get("stable_since", now)) if same else now
        current[key] = {**fingerprint, "stable_since": stable_since, "processed": False}
        if stable_seconds <= 0 or (same and now - stable_since >= stable_seconds):
            ready.append(path)
        else:
            pending += 1
    return ready, pending, skipped, {"updated_at": now, "files": current}


def classify_recordings(
    store,
    sources: list[str | Path],
    *,
    timezone: str = "Asia/Seoul",
    grace_minutes: int = 20,
    recursive: bool = True,
    extensions: set[str] | None = None,
    dry_run: bool = False,
    move: bool = False,
    destination: str | Path | None = None,
    stable_seconds: int = 0,
    scan_state_path: str | Path | None = None,
) -> ClassificationResult:
    """녹음을 찾아 분류하고, 적용 모드에서는 blob과 과목별 링크를 만든다."""
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"알 수 없는 시간대입니다: {timezone}") from exc
    destination_path = Path(destination).expanduser().resolve() if destination else None
    paths = discover_recordings(
        sources,
        recursive=recursive,
        extensions=extensions,
        store_root=store.root,
    )
    result = ClassificationResult(discovered=len(paths))
    state = None
    state_path = None
    if stable_seconds > 0 or scan_state_path:
        state_path = Path(scan_state_path or store.root / "recording_scan_state.json")
        paths, result.pending, result.skipped, state = _ready_after_stability_check(
            store,
            paths,
            state_path=state_path,
            stable_seconds=max(0, stable_seconds),
            now=time_module.time(),
        )
    for path in paths:
        try:
            source_stat = path.stat()
            media = inspect_recording(path, zone)
            match = match_recording(
                store,
                media.captured_at,
                " ".join(filter(None, (path.name, media.metadata_title))),
                timestamp_source=media.timestamp_source,
                grace_minutes=grace_minutes,
            )
            setattr(result, match.status, getattr(result, match.status) + 1)
            item = {
                "source": str(path),
                "captured_at": media.captured_at.isoformat(timespec="seconds"),
                "timestamp_source": media.timestamp_source,
                "duration_sec": round(media.duration_sec, 1),
                "metadata_title": media.metadata_title,
                "status": match.status,
                "course_id": match.course_id,
                "course": match.course_name,
                "method": match.method,
                "confidence": match.confidence,
                "week": match.week,
                "lesson": match.lesson,
                "details": match.details,
            }
            if not dry_run:
                digest, _ = store.put_blob_file(path)
                finished_stat = path.stat()
                if (
                    finished_stat.st_size != source_stat.st_size
                    or finished_stat.st_mtime_ns != source_stat.st_mtime_ns
                ):
                    raise RuntimeError("가져오는 동안 파일이 변경되어 다음 스캔까지 보류합니다.")
                previous = store.query("SELECT path FROM recording WHERE sha256=?", (digest,))
                target = _target_for(
                    store,
                    path,
                    media,
                    match,
                    digest,
                    destination_path,
                )
                _link_blob(store, digest, target)
                old_path = previous[0]["path"] if previous else None
                try:
                    stored_path = str(target.relative_to(store.root))
                except ValueError:
                    stored_path = str(target)
                store.save_recording({
                    "sha256": digest,
                    "original_name": path.name,
                    "metadata_title": media.metadata_title,
                    "source_path": str(path),
                    "source_bytes": source_stat.st_size,
                    "source_mtime_ns": source_stat.st_mtime_ns,
                    "captured_at": media.captured_at.isoformat(timespec="seconds"),
                    "timestamp_source": media.timestamp_source,
                    "duration_sec": media.duration_sec,
                    "course_id": match.course_id,
                    "timetable_slot_id": match.slot_id,
                    "week": match.week,
                    "lesson": match.lesson,
                    "match_status": match.status,
                    "match_method": match.method,
                    "confidence": match.confidence,
                    "path": stored_path,
                    "details_json": json.dumps(match.details, ensure_ascii=False),
                })
                store.commit()
                _remove_old_managed_link(
                    store,
                    old_path,
                    target,
                    digest,
                    destination_path,
                )
                item["path"] = stored_path
                item["sha256"] = digest
                result.imported += 1
                if state is not None and str(path) in state["files"]:
                    state["files"][str(path)]["processed"] = True
                if move and path.resolve() != target.resolve():
                    path.unlink()
                    result.moved += 1
            result.items.append(item)
        except Exception as exc:
            result.failed += 1
            result.items.append({"source": str(path), "status": "error", "error": str(exc)})
    if state is not None and state_path is not None and not dry_run:
        _write_scan_state(state_path, state)
    return result
