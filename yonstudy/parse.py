"""LearnUs(Moodle + coursemos) HTML 파서.

모든 셀렉터는 2026-08-02에 실제 응답을 받아 확인한 구조를 기준으로 한다.
DOM이 바뀌면 여기만 고치면 되도록 파싱을 한 곳에 모았다.
"""

from __future__ import annotations

import html as html_mod
import json
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

LEARNUS = "https://ys.learnus.org"

SEMESTER_NAMES = {"10": "1학기", "11": "여름계절수업", "20": "2학기", "21": "겨울계절수업"}

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def text(fragment: str) -> str:
    """HTML 조각에서 순수 텍스트만 뽑아 공백을 정규화한다."""
    return _WS.sub(" ", html_mod.unescape(_TAG.sub(" ", fragment))).strip()


def _cells(row: str) -> list[str]:
    return [text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]


# --------------------------------------------------------------------------
# 강좌 목록  /local/ubion/user/index.php?year=all&semester=all
# --------------------------------------------------------------------------


@dataclass
class Course:
    course_id: int
    year: str
    semester: str  # "1학기" 등 표시명
    kind: str  # 교과 / 비교과 …
    title: str  # 원문 "경제학개론 (ECO1002.03-00)"
    name: str  # "경제학개론"
    code: str | None  # "ECO1002"
    section: str | None  # "03"

    @property
    def slug(self) -> str:
        safe = re.sub(r'[\\/:*?"<>|]', "_", self.name).strip()
        return f"{self.code or 'NA'}_{safe}"

    @property
    def url(self) -> str:
        return f"{LEARNUS}/course/view.php?id={self.course_id}"


_COURSE_TITLE = re.compile(r"^(.*?)\s*\(([A-Za-z0-9]+)\.(\d+)-(\d+)\)$")


def parse_course_list(page: str) -> list[Course]:
    body = re.search(r'<tbody class="my-course-lists">(.*?)</tbody>', page, re.S)
    if not body:
        return []
    courses: list[Course] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1), re.S):
        link = re.search(
            r'href="[^"]*course/view\.php\?id=(\d+)"[^>]*>(.*?)</a>', row, re.S
        )
        if not link:
            continue
        cells = _cells(row)
        badge = re.search(r'class="badge[^"]*">([^<]*)<', row)
        title = text(link.group(2))
        m = _COURSE_TITLE.match(title)
        courses.append(
            Course(
                course_id=int(link.group(1)),
                year=cells[0] if cells else "",
                semester=cells[1] if len(cells) > 1 else "",
                kind=text(badge.group(1)) if badge else "",
                title=title,
                name=m.group(1) if m else title,
                code=m.group(2) if m else None,
                section=m.group(3) if m else None,
            )
        )
    return courses


def parse_semester_options(page: str) -> dict[str, list[str]]:
    """연도/학기 셀렉터의 선택 가능한 값들."""
    out: dict[str, list[str]] = {}
    for sel in re.findall(r"<select[^>]*>.*?</select>", page, re.S):
        name = re.search(r'(?:name|id)="([^"]+)"', sel)
        if not name:
            continue
        out[name.group(1)] = [
            v for v, _ in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)<', sel)
        ]
    return out


# --------------------------------------------------------------------------
# 강좌 페이지  /course/view.php?id=…
# --------------------------------------------------------------------------


# 49개 강좌 전수 조사(2026-08-02)에서 실제로 등장한 활동 모듈.
# 개수는 조사 시점 총계 — 파서를 손볼 때 어디가 중요한지 가늠용.
KNOWN_MODULES = {
    "vod": "동영상 강의 (697)",
    "ubfile": "자료 파일 (551)",
    "assign": "과제 (229)",
    "ubboard": "게시판 (194)",
    "label": "설명 텍스트 (82)",
    "quiz": "퀴즈/시험 (81)",
    "feedback": "설문형 출석 (74)",
    "folder": "폴더 자료 (59)",
    "zoom": "실시간 화상강의 (45)",
    "url": "외부 링크 (40)",
    "turnitintooltwo": "Turnitin 표절검사 제출 (26)",
    "resource": "표준 자료 (26)",
    "forum": "포럼 (11)",
    "vpl": "코딩 과제 Virtual Programming Lab (8)",
    "lti": "외부 도구 연동 (2)",
    "choice": "선택형 설문 (2)",
}

# 제출물이 발생하는 = "내가 한 활동"으로 아카이빙해야 하는 모듈.
# forum은 여기 넣지 않는다 — "제출" 개념이 없어 전부 미제출로 잡히기 때문이다.
# 대신 강좌 단위로 /mod/forum/user.php 에서 내가 쓴 글을 모은다 (parse_forum_posts).
SUBMISSION_MODULES = {"assign", "turnitintooltwo", "vpl", "quiz", "feedback", "choice"}
# 자료 파일이 붙는 모듈
RESOURCE_MODULES = {"ubfile", "folder", "resource"}


@dataclass
class Activity:
    cmid: int
    modname: str  # vod / assign / ubboard / ubfile / url / quiz …
    title: str
    url: str
    section_idx: int | None = None
    section_name: str = ""
    indent: int = 0  # mod-indent 들여쓰기 깊이
    completion: str | None = None  # y / n / None(추적 안 함)
    # VOD 전용
    open_from: str | None = None  # "2026-03-04 00:00:00"
    open_to: str | None = None
    late_until: str | None = None  # "(지각 : …)" 로 표기되는 지각 인정 기한
    duration: str | None = None  # "33:59" 또는 "1:02:33"
    restricted: bool = False

    @property
    def viewer_url(self) -> str | None:
        if self.modname != "vod":
            return None
        return f"{LEARNUS}/mod/vod/viewer.php?id={self.cmid}"

    @property
    def is_submission(self) -> bool:
        return self.modname in SUBMISSION_MODULES

    @property
    def duration_sec(self) -> int | None:
        """'33:59' / '1:02:33' 모두 초로 환산. 두 포맷 모두 실제로 존재한다."""
        if not self.duration:
            return None
        s = 0
        for part in self.duration.split(":"):
            if not part.isdigit():
                return None
            s = s * 60 + int(part)
        return s


_ACTIVITY_LI = re.compile(
    r'<li class="activity\s+(\w+)\s+modtype_\w+[^"]*"\s+id="module-(\d+)"(.*?)</li>', re.S
)
_PERIOD = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*~\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
)


_INSTANCE = re.compile(r'class="instancename">(.*?)</span>\s*</a>', re.S)
_ACCESSHIDE = re.compile(r'<span class="accesshide[^"]*"[^>]*>.*?</span>', re.S)


def _instance_name(blob: str) -> str:
    """활동 제목.

    두 가지 변종을 모두 처리해야 한다 (49개 강좌 전수 확인):
      * vod  : 제목 뒤에 <span class="accesshide"> VOD</span>가 붙는다
      * assign 등 : accesshide 없이 제목만
      * 제한됨(dimmed) 활동 : <a> 자체가 없고 <div class="dimmed">로 감싸진다
    accesshide를 먼저 걷어낸 뒤 instancename의 닫는 </span>까지 취하면 셋 다 잡힌다.
    """
    m = re.search(r'class="instancename">(.*?)</span>', _ACCESSHIDE.sub("", blob), re.S)
    return text(m.group(1)) if m else ""


def _activity_url(blob: str, modname: str, cmid: int) -> str:
    m = re.search(r'href="(https://[^"]*/mod/\w+/(?:view|viewer)\.php\?id=\d+[^"]*)"', blob)
    return m.group(1) if m else f"{LEARNUS}/mod/{modname}/view.php?id={cmid}"


def _indent_depth(blob: str) -> int:
    m = re.search(r'class="mod-indent mod-indent-(\d+)"', blob)
    return int(m.group(1)) if m else 0


def _completion_state(blob: str) -> str | None:
    """강좌 활동의 완료 체크 상태를 ``y``/``n``으로 정규화한다.

    LearnUs 구형 테마는 ``completion-auto-y`` 아이콘을 쓰지만, 수동 체크와
    새 Moodle 마크업은 checkbox/ARIA/data-completionstate로 상태를 표현한다.
    완료 추적 자체를 켜지 않은 활동은 ``None``으로 둔다. ``None``을 미완료로
    바꾸면 체크박스가 없는 과목이 전부 할 일로 오인되기 때문이다.
    """
    m = re.search(r"completion-(?:auto|manual)-([yn])\b", blob, re.I)
    if m:
        return m.group(1).lower()

    m = re.search(
        r"data-(?:activity-)?completionstate\s*=\s*['\"]?([01])\b",
        blob,
        re.I,
    )
    if m:
        return "y" if m.group(1) == "1" else "n"

    # 수동 완료 토글. 관련 form/button 안의 상태만 보며, 과제 본문의 일반
    # checkbox를 활동 완료로 잘못 읽지 않는다.
    controls = re.findall(
        r"<(?:form|button|input)\b[^>]*(?:togglecompletion|activity-completion|completion-toggle)[^>]*>",
        blob,
        re.I,
    )
    controls += re.findall(
        r"<form\b[^>]*(?:togglecompletion|activity-completion|completion-toggle)[^>]*>.*?</form>",
        blob,
        re.I | re.S,
    )
    for control in controls:
        aria = re.search(r"aria-(?:checked|pressed)\s*=\s*['\"](true|false)['\"]", control, re.I)
        if aria:
            return "y" if aria.group(1).lower() == "true" else "n"
        if re.search(r"\btype\s*=\s*['\"]checkbox['\"]", control, re.I):
            return "y" if re.search(r"\bchecked(?:\s|=|/?>)", control, re.I) else "n"

    labels = " ".join(
        html_mod.unescape(value)
        for value in re.findall(r"(?:alt|title)\s*=\s*['\"]([^'\"]+)['\"]", blob, re.I)
    ).lower()
    if labels:
        # 토글의 문구는 '누르면 될 상태'이므로 현재 상태와 반대다.
        if re.search(r"완료하지 않은 것으로 표시|mark as not done", labels):
            return "y"
        if re.search(r"완료로 표시|mark as done", labels):
            return "n"
        if re.search(r"미완료|완료되지 (?:않음|않았)|not completed|incomplete", labels):
            return "n"
        if re.search(r"완료됨|이수 완료|\bcompleted\b|\bdone\b", labels):
            return "y"
    return None


_LATE = re.compile(r"지각\s*[:：]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _period_and_duration(blob: str) -> dict:
    """학습기간·지각기한·재생시간.

    697개 VOD 중 94개(13.5%)는 학습기간 표기가 아예 없다 — 진도처리기간 미설정 강좌.
    일부 강좌는 정규기간 뒤에 "(지각 : …)"로 지각 인정 기한을 따로 둔다.
    재생시간은 MM:SS와 HH:MM:SS 두 포맷이 모두 쓰인다.
    """
    period = _PERIOD.search(blob)
    late = _LATE.search(blob)
    dur = re.search(r'class="text-info">\s*,?\s*(\d{1,2}(?::\d{2}){1,2})\s*<', blob)
    return {
        "open_from": period.group(1) if period else None,
        "open_to": period.group(2) if period else None,
        "late_until": late.group(1) if late else None,
        "duration": dur.group(1) if dur else None,
    }


def parse_course_page(page: str) -> tuple[list[Activity], dict]:
    """강좌 페이지에서 활동 목록과 강좌 메타를 뽑는다."""
    activities: list[Activity] = []

    # 섹션(주차) 경계 위치를 미리 구해 활동을 배정한다.
    sections: list[tuple[int, int, str]] = []  # (start, idx, name)
    for m in re.finditer(
        r'<li[^>]*id="section-(\d+)"[^>]*>(.*?)(?=<li[^>]*id="section-\d+"|\Z)',
        page,
        re.S,
    ):
        name = re.search(r'class="sectionname"[^>]*>(.*?)</', m.group(2), re.S)
        sections.append((m.start(), int(m.group(1)), text(name.group(1)) if name else ""))

    def section_of(pos: int) -> tuple[int | None, str]:
        found = (None, "")
        for start, idx, name in sections:
            if start <= pos:
                found = (idx, name)
            else:
                break
        return found

    for m in _ACTIVITY_LI.finditer(page):
        modname, cmid, blob = m.group(1), int(m.group(2)), m.group(3)
        if modname == "label":
            continue
        idx, sname = section_of(m.start())
        activities.append(
            Activity(
                cmid=cmid,
                modname=modname,
                title=_instance_name(blob) or f"({modname} {cmid})",
                url=_activity_url(blob, modname, cmid),
                section_idx=idx,
                section_name=sname,
                indent=blob.count("mod-indent-outer") and _indent_depth(blob),
                completion=_completion_state(blob),
                **_period_and_duration(blob),
                restricted="isrestricted" in blob,
            )
        )

    meta = {}
    prof = re.search(r'class="coursename"[^>]*>(.*?)</', page, re.S)
    if prof:
        meta["header"] = text(prof.group(1))
    return activities, meta


# --------------------------------------------------------------------------
# VOD 뷰어  /mod/vod/viewer.php?id=…
# --------------------------------------------------------------------------

# mod_vod/vod AMD 모듈의 progress() 시그니처 (d..C, 26개) 순서.
# 실제 번들에서 확인: c.progress=function(d,e,f,g,h,i,j,k,l,m,n,o,p,q,r,s,t,u,v,w,x,y,z,A,B,C)
_PROGRESS_PARAMS = [
    "vod_tag_id",      # d
    "is_progress",     # e  진도 집계 대상 여부
    "f_flag",          # f  0이면 이어보기/이동제한 분기 활성
    "g_ts",            # g
    "h_num",           # h
    "progress_period", # i  ★ 진도처리기간 여부. False면 ajax() 자체가 no-op
    "courseid",        # j
    "cmid",            # k
    "trackid",         # l
    "attempt",         # m
    "max_position",    # n  ★ 지금까지의 최대 시청 위치(초)
    "resume",          # o
    "interval_ms",     # p  주기 로그 간격
    "hls",             # q
    "swf_url",         # r  ★ falsy일 때만 seek 제한 코드가 설치됨
    "rate_allowed",    # s  ★ 배속 허용
    "youtube",         # t
    "before_progress", # u
    "rate_max",        # v  ★ 최대 허용 배속
    "quality_selector",# w
    "checker_url",     # x  ★ 외부 learningChecker 모듈 경로
    "checker_token",   # y
    "logtime",         # z
    "skip_offset",     # A
    "use_contents",    # B
    "contents",        # C
]


@dataclass
class VodViewer:
    cmid: int
    status: str = "ok"  # ok / empty / no_access / error
    hls_url: str | None = None
    poster: str | None = None
    uuid: str | None = None
    subtitle_langs: list[str] = field(default_factory=list)
    available: str | None = None
    playback_rates: list[float] = field(default_factory=list)
    progress: dict = field(default_factory=dict)

    @property
    def can_log_progress(self) -> bool:
        """지금 재생하면 진도가 실제로 기록되는가."""
        return bool(self.progress.get("progress_period")) and bool(
            self.progress.get("is_progress")
        )

    @property
    def max_rate(self) -> float:
        if not self.progress.get("rate_allowed"):
            return 1.0
        try:
            return float(self.progress.get("rate_max") or 1.0)
        except (TypeError, ValueError):
            return 1.0

    @property
    def seek_restricted(self) -> bool:
        """앞으로 건너뛰기가 막혀 있는가 (swf_url이 falsy일 때만 제한 코드가 붙는다)."""
        return not self.progress.get("swf_url")


def _js_args(raw: str) -> list:
    """progress(...) 호출의 인자 문자열을 파이썬 값 리스트로 변환."""
    try:
        return json.loads("[" + raw.replace("\\/", "/") + "]")
    except json.JSONDecodeError:
        return []


def parse_vod_viewer(page: str, cmid: int) -> VodViewer:
    """뷰어 페이지 → 재생/진도 정보.

    실측: 697편 중 94편은 재생 정보가 아예 없다. 원인이 둘로 갈린다.
      * 153바이트짜리 빈 페이지 — 콘텐츠가 삭제된 과거 강좌 (주로 2022년)
      * 강좌 페이지로 되돌려 보냄 — 열람기간이 끝났거나 제한된 영상
    둘 다 파서 문제가 아니므로 status로 구분해 기록한다.
    """
    v = VodViewer(cmid=cmid)
    if len(page) < 500:
        v.status = "empty"
        return v
    if "data-setup-lazy" not in page and ".progress(" not in page:
        v.status = "no_access"
        return v

    setup = re.search(r"data-setup-lazy='(.*?)'", page, re.S) or re.search(
        r'data-setup-lazy="(.*?)"', page, re.S
    )
    if setup:
        try:
            cfg = json.loads(html_mod.unescape(setup.group(1)))
            v.hls_url = (cfg.get("sources") or {}).get("src")
            v.playback_rates = cfg.get("playbackRates") or []
        except json.JSONDecodeError:
            pass

    poster = re.search(r'poster="([^"]+)"', page)
    if poster:
        v.poster = poster.group(1)

    uu = re.search(r"subtitle_auto\.php\?uuid=([0-9a-f-]+)", page)
    if uu:
        v.uuid = uu.group(1)
    v.subtitle_langs = sorted(
        set(re.findall(r"subtitle_auto\.php\?uuid=[0-9a-f-]+&(?:amp;)?language=(\w+)", page))
    )
    if not v.uuid and v.hls_url:
        m = re.search(r"/([0-9a-f-]{36})/", v.hls_url)
        if m:
            v.uuid = m.group(1)

    avail = re.search(r'class="progress_date">\s*(.*?)\s*</div>', page, re.S)
    if avail:
        v.available = text(avail.group(1))

    call = re.search(r"\.progress\((.*?)\);", page, re.S)
    if call:
        args = _js_args(call.group(1))
        v.progress = dict(zip(_PROGRESS_PARAMS, args))
    return v


# --------------------------------------------------------------------------
# 진도 리포트  /report/ubcompletion/user_progress.php?id=…
# --------------------------------------------------------------------------


@dataclass
class ProgressRow:
    week: str
    title: str
    content_time: str  # "33:59"
    max_position: str  # "47:24" 또는 "-"
    progress: str  # "100%" 또는 ""

    @staticmethod
    def _secs(hms: str) -> int:
        parts = [p for p in hms.strip().split(":") if p.isdigit()]
        s = 0
        for p in parts:
            s = s * 60 + int(p)
        return s

    @property
    def duration_sec(self) -> int:
        return self._secs(self.content_time)

    @property
    def watched_sec(self) -> int:
        # 진도표 링크 문구가 언어에 따라 `Details (1)` 또는 `상세보기 (1)`로
        # 시간 바로 뒤에 붙는다. 제거하지 않으면 `56:40 상세보기`가 56초로 잘린다.
        value = re.split(r"(?:Details|상세보기)", self.max_position, flags=re.I)[0]
        return self._secs(value)

    @property
    def done(self) -> bool:
        return self.progress.strip().rstrip("%") in ("100", "100.0")


def parse_progress_report(page: str) -> tuple[dict, list[ProgressRow]]:
    """(학생 정보, 콘텐츠별 진도 행). 표는 한 줄에 여러 콘텐츠가 들어갈 수 있다."""
    info: dict = {}
    tables = re.findall(r"<table[^>]*>.*?</table>", page, re.S)
    if tables:
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tables[0], re.S):
            c = _cells(row)
            if len(c) >= 2:
                info[c[0]] = c[1]

    rows: list[ProgressRow] = []
    if len(tables) > 1:
        week = ""
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tables[1], re.S):
            c = _cells(row)
            if not c or c[0] in ("Week", "주차"):
                continue
            # 주차 셀은 rowspan이라 첫 행에만 등장한다.
            if c and re.fullmatch(r"\d+", c[0]):
                week, c = c[0], c[1:]
            # 콘텐츠 4칸(제목/길이/최대위치/진도)씩 반복
            for i in range(0, len(c) - 3, 4):
                title, dur, pos, prog = c[i], c[i + 1], c[i + 2], c[i + 3]
                if not title or not re.match(r"^\d+:\d+", dur):
                    continue
                rows.append(
                    ProgressRow(
                        week=week,
                        title=title,
                        content_time=dur,
                        max_position=pos.split("Details")[0].strip(),
                        progress=prog,
                    )
                )
    return info, rows


# --------------------------------------------------------------------------
# 과제  /mod/assign/view.php?id=…
# --------------------------------------------------------------------------


@dataclass
class AssignDetail:
    cmid: int
    modname: str = "assign"
    fields: dict = field(default_factory=dict)
    submitted_files: list[tuple[str, str]] = field(default_factory=list)  # (파일명, URL)
    intro_files: list[tuple[str, str]] = field(default_factory=list)

    # 제출 여부를 나타내는 표현은 모듈/언어마다 다르다.
    _SUBMITTED = ("submitted", "제출 완료", "제출완료", "보고서 제출", "채점", "graded",
                  "answered", "완료", "응답")
    _NOT_SUBMITTED = ("no submission", "no attempt", "제출 안 함", "미제출", "not submitted")

    @property
    def status_text(self) -> str:
        for key in (
            "Submission status", "제출 여부", "제출 상태",
            "Attempt status", "Completion status", "상태",
        ):
            if self.fields.get(key):
                return self.fields[key]
        return ""

    @property
    def submitted(self) -> bool:
        s = self.status_text.lower()
        if not s:
            # 상태표가 없는 모듈(quiz/feedback/vpl)은 제출 파일·응시 기록 유무로 판단
            return bool(self.submitted_files) or bool(self.fields.get("_attempts"))
        if any(n in s for n in self._NOT_SUBMITTED):
            return False
        return any(y in s for y in self._SUBMITTED)


_PLUGINFILE = re.compile(r'href="(https://ys\.learnus\.org/pluginfile\.php/[^"]+)"[^>]*>(.*?)</a>', re.S)


def parse_submission(page: str, cmid: int, modname: str = "assign") -> AssignDetail:
    """제출형 활동 상세.

    모듈마다 화면이 다르다 (49개 강좌 전수 조사 기준):
      * assign          — generaltable에 Submission status / Due date / Grade
      * turnitintooltwo — 자체 제출 테이블, pluginfile 대신 자체 다운로드 링크
      * vpl             — 코드 제출, 상태가 표가 아니라 문장으로 나옴
      * quiz            — 응시 이력 표(Attempt / Marks)
      * feedback        — 응답 완료 여부만
    공통 골격(정의 테이블 + pluginfile 링크)을 먼저 훑고, 모듈별 신호를 덧댄다.
    """
    d = AssignDetail(cmid=cmid, modname=modname)

    # 라벨/값 2열 테이블은 모듈을 가리지 않고 대부분 존재한다.
    for tbl in re.findall(r"<table[^>]*>.*?</table>", page, re.S):
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S):
            c = _cells(row)
            if len(c) == 2 and c[0] and len(c[0]) < 40:
                d.fields.setdefault(c[0], c[1])

    for m in _PLUGINFILE.finditer(page):
        url = html_mod.unescape(m.group(1))
        name = text(m.group(2)) or url.rsplit("/", 1)[-1].split("?")[0]
        if "submission" in url or "onlinetext" in url:
            d.submitted_files.append((name, url))
        elif "introattachment" in url or "_intro" in url:
            d.intro_files.append((name, url))
        else:
            d.intro_files.append((name, url))

    if modname == "turnitintooltwo":
        _parse_turnitin(page, d)
    elif modname == "vpl":
        _parse_vpl(page, d)
    elif modname == "quiz":
        # 응시 이력 표: "Review" 링크 개수 = 응시 횟수
        attempts = len(re.findall(r"/mod/quiz/review\.php\?attempt=\d+", page))
        if attempts:
            d.fields["_attempts"] = str(attempts)
        grade = re.search(r"([\d.]+)\s*(?:out of|/)\s*([\d.]+)", text(page))
        if grade:
            d.fields.setdefault("Grade", f"{grade.group(1)}/{grade.group(2)}")
    elif modname == "feedback":
        if re.search(r"이미 (?:이 설문에 )?응답|already (?:completed|submitted)", page, re.I):
            d.fields.setdefault("Completion status", "answered")

    return d


_TII_DATE = re.compile(r"(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2})")
_TII_GRADE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+)")


def _parse_turnitin(page: str, d: AssignDetail) -> None:
    """Turnitin 제출 테이블.

    컬럼이 숨김열 때문에 헤더와 위치가 어긋나므로, 위치 대신 값의 모양으로 찾는다.
    실측 행: [part, '', 이름, 학번, 제목, 제목+URL, 보고서ID, …, '2026/04/26 22:45', '93',
              '93 /100 …', '보고서 제출', '', '--']
    """
    for tbl in re.findall(r"<table[^>]*>.*?</table>", page, re.S):
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S):
            flat = " ".join(_cells(row))
            when = _TII_DATE.search(flat)
            if not when:
                continue
            d.fields.setdefault("Last modified", when.group(1))
            # 날짜(2026/04/26)가 성적(93/100)으로 오인되므로 먼저 지운다.
            grade = _TII_GRADE.search(_TII_DATE.sub(" ", flat))
            if grade and int(grade.group(2)) <= 1000:
                d.fields.setdefault("Grade", f"{grade.group(1)}/{grade.group(2)}")
            state = re.search(r"(보고서 제출|제출 완료|Submitted|미제출|Not Submitted)", flat)
            d.fields["Submission status"] = state.group(1) if state else "제출 완료"
            return

    # 제출 테이블이 없으면 마감/시작일만이라도 남긴다.
    due = re.search(r"마감일.*?(\d{4}-\s*\d{1,2}월-\d{1,2}\s+\d{2}:\d{2})", text(page))
    if due:
        d.fields.setdefault("Due date", due.group(1))
    d.fields.setdefault("Submission status", "미제출")


_VPL_DOWNLOAD = re.compile(
    r'href="([^"]*?/mod/vpl/views/downloadsubmission\.php\?[^"]*?submissionid=(\d+)[^"]*)"'
)


def _parse_vpl(page: str, d: AssignDetail) -> None:
    """VPL(코딩 과제).

    제출 화면은 JS로 그려져 본문 텍스트로는 판정할 수 없다. 대신 페이지에 심어진
    downloadsubmission.php 링크에 submissionid가 있으면 제출이 존재한다는 뜻이다.
    그 URL이 곧 제출한 소스코드 묶음을 받는 경로이기도 하다.
    """
    m = _VPL_DOWNLOAD.search(page)
    if m:
        d.fields["Submission status"] = "제출 완료"
        d.fields["_vpl_submission_id"] = m.group(2)
        d.submitted_files.append(
            (f"vpl_submission_{m.group(2)}.zip", html_mod.unescape(m.group(1)))
        )
    else:
        d.fields.setdefault("Submission status", "미제출")
    grade = re.search(r"(?:성적|Grade)[^0-9]{0,20}(\d+(?:\.\d+)?)\s*/\s*(\d+)", text(page))
    if grade:
        d.fields.setdefault("Grade", f"{grade.group(1)}/{grade.group(2)}")


# --------------------------------------------------------------------------
# 게시판  mod/ubboard  (공지사항 / Q&A / 자료 게시판)
# --------------------------------------------------------------------------


@dataclass
class Post:
    post_id: str  # ubboard=bwid, forum=post id
    no: str = ""  # 게시판이 붙인 글 번호
    subject: str = ""
    writer: str = ""
    written_at: str = ""
    hits: str = ""
    url: str = ""
    body: str = ""  # 텍스트로 평탄화한 본문
    body_html: str = ""
    attachments: list[tuple[str, str]] = field(default_factory=list)
    replies: int = 0


def _plugin_assets(blob: str, marker: str) -> list[tuple[str, str]]:
    """본문에 붙은 파일 — 첨부 링크(href)와 본문 삽입 이미지(img src)를 모두 모은다."""
    out, seen = [], set()
    for name, url in parse_pluginfiles(blob):
        if marker in url and url not in seen:
            seen.add(url)
            out.append((name, url))
    for m in re.finditer(r'<img[^>]+src="([^"]*pluginfile\.php/[^"]+)"', blob):
        url = html_mod.unescape(m.group(1))
        if marker in url and url not in seen:
            seen.add(url)
            out.append((unquote(url.rsplit("/", 1)[-1].split("?")[0]), url))
    return out


# 2페이지부터는 href가 article.php?id=X&page=2&bwid=Y 형태로 바뀐다.
# id 바로 뒤에 bwid가 온다고 가정하면 1페이지에서만 동작한다.
_UB_ROW = re.compile(
    r'href="([^"]*?/mod/ubboard/article\.php\?[^"]*?bwid=(\d+)[^"]*)"[^>]*>(.*?)</a>', re.S
)


def parse_ubboard_list(page: str) -> tuple[list[Post], int]:
    """게시판 목록 한 페이지. (글 목록, 마지막 페이지 번호)를 돌려준다."""
    posts: list[Post] = []
    tbl = re.search(r"<table[^>]*ubboard_table.*?</table>", page, re.S)
    if tbl:
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl.group(0), re.S):
            link = _UB_ROW.search(row)
            if not link:
                continue
            cells = _cells(row)
            title = text(link.group(3))
            # 댓글 수는 제목 <a> 바깥의 <span class="comment">[3]</span> 에 있다.
            reply = re.search(r'<span class="comment">\s*\[(\d+)\]', row)
            posts.append(
                Post(
                    post_id=link.group(2),
                    no=cells[0] if cells else "",
                    subject=re.sub(r"\s*\[\d+\]\s*$", "", title),
                    writer=cells[2] if len(cells) > 2 else "",
                    written_at=cells[3] if len(cells) > 3 else "",
                    hits=cells[4] if len(cells) > 4 else "",
                    url=html_mod.unescape(link.group(1)),
                    replies=int(reply.group(1)) if reply else 0,
                )
            )
    pages = [int(p) for p in re.findall(r"[?&]page=(\d+)", html_mod.unescape(page))]
    return posts, (max(pages) if pages else 1)


def parse_ubboard_article(page: str, post: Post) -> Post:
    view = re.search(r'<div class="ubboard_view">(.*?)(?=<footer|</body)', page, re.S)
    blob = view.group(1) if view else page
    subject = re.search(r'<div class="subject">\s*<h\d>(.*?)</h\d>', blob, re.S)
    writer = re.search(r'<div class="writer">.*?</span>\s*:\s*(.*?)</div>', blob, re.S)
    date = re.search(r'<div class="date">.*?</span>\s*:\s*(.*?)</div>', blob, re.S)
    hit = re.search(r'<div class="hit">.*?</span>\s*:\s*(.*?)</div>', blob, re.S)
    content = re.search(r'<div class="content">(.*?)</div>\s*</div>', blob, re.S) or re.search(
        r'<div class="text_to_html">(.*?)</div>', blob, re.S
    )
    if subject:
        post.subject = text(subject.group(1))
    if writer:
        post.writer = text(writer.group(1))
    if date:
        post.written_at = text(date.group(1))
    if hit:
        post.hits = text(hit.group(1))
    if content:
        post.body_html = content.group(1)
        post.body = text(content.group(1))
    post.attachments = _plugin_assets(blob, "mod_ubboard")
    return post


# --------------------------------------------------------------------------
# 포럼  mod/forum
# --------------------------------------------------------------------------


def parse_forum_discussions(page: str) -> list[tuple[str, str]]:
    """포럼 목록에서 (토론 id, 제목). 표 형태·카드 형태 모두 대응."""
    out, seen = [], set()
    plain = html_mod.unescape(page)
    for m in re.finditer(
        r'href="[^"]*?/mod/forum/discuss\.php\?d=(\d+)[^"]*"[^>]*>(.*?)</a>', plain, re.S
    ):
        did = m.group(1)
        if did in seen:
            continue
        seen.add(did)
        out.append((did, text(m.group(2))))
    return out


_FORUM_POST = re.compile(r'<div class="forumpost[^"]*"[^>]*>', re.S)


def parse_forum_discussion(page: str) -> list[Post]:
    """토론 한 건의 모든 글. forumpost 블록은 중첩되므로 시작 위치로 잘라 쓴다."""
    plain = html_mod.unescape(page)
    starts = [m.start() for m in _FORUM_POST.finditer(plain)]
    posts: list[Post] = []
    for i, start in enumerate(starts):
        blob = plain[start : starts[i + 1] if i + 1 < len(starts) else len(plain)]
        subject = re.search(r'<div class="subject"[^>]*>(.*?)</div>', blob, re.S)
        author = re.search(r'<div class="author"[^>]*>(.*?)</div>', blob, re.S)
        body = re.search(r'<div class="[^"]*posting[^"]*"[^>]*>(.*?)</div>', blob, re.S)
        pid = re.search(r"#p(\d+)", blob)
        line = text(author.group(1)) if author else ""
        when = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", line)
        posts.append(
            Post(
                post_id=pid.group(1) if pid else str(i),
                subject=text(subject.group(1)) if subject else "",
                writer=re.sub(r"^by\s+", "", line.split(" - ")[0]).strip(),
                written_at=when.group(1) if when else "",
                body=text(body.group(1)) if body else "",
                body_html=body.group(1) if body else "",
                        attachments=_plugin_assets(blob, "mod_forum"),
            )
        )
    return posts


def parse_forum_posts(page: str) -> list[dict]:
    """/mod/forum/user.php 가 돌려주는 '내가 쓴 글' 목록.

    포럼은 assign 같은 제출 상태가 없으므로, 이 목록이 곧 내 활동 기록이다.
    """
    posts: list[dict] = []
    plain = html_mod.unescape(page)
    for m in re.finditer(
        r'<div class="forumpost[^"]*".*?</div>\s*</div>\s*</div>', plain, re.S
    ):
        blob = m.group(0)
        subject = re.search(r'class="subject[^"]*"[^>]*>(.*?)</', blob, re.S)
        when = re.search(r'class="author"[^>]*>(.*?)</', blob, re.S)
        link = re.search(r"/mod/forum/discuss\.php\?d=\d+(?:#p\d+)?", blob)
        posts.append(
            {
                "subject": text(subject.group(1)) if subject else "",
                "author_line": text(when.group(1)) if when else "",
                "url": LEARNUS + link.group(0) if link else None,
                "body": text(blob)[:2000],
            }
        )
    if not posts:  # 레이아웃이 다르면 링크만이라도 남긴다
        for d in sorted(set(re.findall(r"/mod/forum/discuss\.php\?d=\d+", plain))):
            posts.append({"subject": "", "author_line": "", "url": LEARNUS + d, "body": ""})
    return posts


def find_user_id(page: str) -> str | None:
    """내 Moodle userid — VPL/Turnitin 상세 조회에 필요하다."""
    # HTML에서 &가 &amp;로 이스케이프돼 있어 그대로 두면 매칭되지 않는다.
    plain = html_mod.unescape(page)
    m = re.search(r"[?&]userid=(\d+)", plain) or re.search(
        r"/user/(?:view|profile)\.php\?id=(\d+)", plain
    )
    return m.group(1) if m else None


# 이전 이름 호환
def parse_assign(page: str, cmid: int) -> AssignDetail:
    return parse_submission(page, cmid, "assign")


# --------------------------------------------------------------------------
# 첨부파일 (모든 페이지 공통)
# --------------------------------------------------------------------------


def parse_pluginfiles(page: str) -> list[tuple[str, str]]:
    seen, out = set(), []
    for m in _PLUGINFILE.finditer(page):
        url = html_mod.unescape(m.group(1))
        if url in seen:
            continue
        seen.add(url)
        out.append((text(m.group(2)) or url.rsplit("/", 1)[-1].split("?")[0], url))
    return out
