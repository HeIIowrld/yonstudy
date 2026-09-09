"""Yonsei-OJ 과제 문제를 읽어 제출 초안용 명세로 정규화한다."""

from __future__ import annotations

import html as html_mod
import http.cookiejar
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path

from .client import UA, parse_input_tags
from .lti import LtiAssignment


OJ_ORIGIN = "https://yonsei-oj.duckdns.org:508"
TIMEOUT = 20


class OjArchiveError(RuntimeError):
    """로그인, 참여 상태 또는 문제 페이지를 안전하게 처리할 수 없음."""


class OjParticipationRequired(OjArchiveError):
    """문제 명세를 보려면 아직 콘테스트 참여가 필요함."""


@dataclass
class OjProblem:
    code: str
    title: str
    url: str
    points: str | None
    time_limit: str | None
    memory_limit: str | None
    languages: list[str]
    statement: str


@dataclass
class OjContest:
    slug: str
    title: str
    url: str
    problems: list[OjProblem]


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", "".join(self._text)).strip()
            self.links.append((self._href, text))
            self._href = None
            self._text = []


class _MarkdownParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._pre = False
        self._inline_code = False
        self._line_start = True

    def _break(self, count: int = 1) -> None:
        current = "".join(self.parts)
        missing = count - (len(current) - len(current.rstrip("\n")))
        if missing > 0:
            self.parts.append("\n" * missing)
        self._line_start = True

    def handle_starttag(self, tag, _attrs):
        tag = tag.lower()
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._break(2)
            # 문제 하나가 통합 명세의 3단계 제목이므로
            # 본문 소제목은 그 아래에 둔다.
            self.parts.append("#### ")
            self._line_start = False
        elif tag == "p":
            self._break(2)
        elif tag == "li":
            self._break(1)
            self.parts.append("- ")
            self._line_start = False
        elif tag == "br":
            self._break(1)
        elif tag == "pre":
            self._break(2)
            self.parts.append("```python\n")
            self._pre = True
            self._line_start = True
        elif tag == "code" and not self._pre:
            if self.parts and not self.parts[-1].endswith((" ", "\n")):
                self.parts.append(" ")
            self.parts.append("`")
            self._inline_code = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}:
            self._break(2 if tag != "li" else 1)
        elif tag == "pre":
            self._break(1)
            self.parts.append("```\n")
            self._pre = False
            self._line_start = True
        elif tag == "code" and not self._pre:
            self.parts.append("`")
            self._inline_code = False

    def handle_data(self, data):
        if self._pre:
            self.parts.append(data)
            return
        value = re.sub(r"\s+", " ", data)
        if not value.strip():
            return
        stripped = value.strip()
        if (
            not self._line_start
            and not self._inline_code
            and self.parts
            and not self.parts[-1].endswith((" ", "\n"))
            and stripped[0] not in ".,;:!?)]}"
        ):
            self.parts.append(" ")
        self.parts.append(stripped)
        self._line_start = False

    def markdown(self) -> str:
        value = "".join(self.parts)
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def _plain(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html_mod.unescape(value)).strip()


def _links(page: str) -> list[tuple[str, str]]:
    parser = _LinkParser()
    parser.feed(page)
    parser.close()
    return parser.links


def parse_problem(page: str, source_url: str) -> OjProblem:
    title_match = re.search(
        r'<div class="problem-title">.*?<h2[^>]*>(.*?)</h2>', page, re.I | re.S
    )
    code_match = re.search(r"/problem/([^/?#]+)", source_url)
    if not title_match or not code_match:
        raise OjArchiveError("Yonsei-OJ 문제 제목 또는 코드를 찾지 못했습니다")

    info: dict[str, str] = {}
    for key, value in re.findall(
        r'class="pi-name">(.*?)</span>\s*<span class="pi-value">(.*?)</span>',
        page,
        re.I | re.S,
    ):
        info[_plain(key).rstrip(":").lower()] = _plain(value)

    allowed = re.search(
        r'id="allowed-langs".*?<div class="toggled">(.*?)</div>', page, re.I | re.S
    )
    languages = []
    if allowed:
        languages = [x.strip() for x in _plain(allowed.group(1)).split(",") if x.strip()]

    description = re.search(
        r'<div class="content-description screen">(.*?)<hr>', page, re.I | re.S
    )
    if not description:
        raise OjArchiveError("Yonsei-OJ 문제 본문을 찾지 못했습니다")
    parser = _MarkdownParser()
    parser.feed(description.group(1))
    parser.close()
    statement = parser.markdown()
    if not statement:
        raise OjArchiveError("Yonsei-OJ 문제 본문이 비어 있습니다")

    return OjProblem(
        code=code_match.group(1),
        title=_plain(title_match.group(1)),
        url=source_url,
        points=info.get("points"),
        time_limit=info.get("time limit"),
        memory_limit=info.get("memory limit"),
        languages=languages,
        statement=statement,
    )


def _assignment_key(value: str) -> str:
    value = re.sub(r"\bcoding\s+assignment\b.*$", "", value, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", value.lower())


class YonseiOjClient:
    def __init__(self, cookie_path: str | Path):
        self.cookie_path = Path(cookie_path)
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self.jar = http.cookiejar.MozillaCookieJar(str(self.cookie_path))
        if self.cookie_path.exists():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def _request(
        self,
        path: str,
        data: dict[str, str] | None = None,
        referer: str | None = None,
    ) -> tuple[str, str]:
        url = urllib.parse.urljoin(OJ_ORIGIN + "/", path)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(url, data=body, headers=headers)
        with self.opener.open(request, timeout=TIMEOUT) as response:
            return response.read().decode("utf-8", "replace"), response.geturl()

    def save(self) -> None:
        self.jar.save(ignore_discard=True, ignore_expires=True)
        try:
            self.cookie_path.chmod(0o600)
        except OSError:
            pass

    def login(self, username: str, password: str) -> None:
        login_url = f"{OJ_ORIGIN}/accounts/login/?next=/contests/"
        page, _ = self._request(login_url)
        token = parse_input_tags(page).get("csrfmiddlewaretoken")
        if not token:
            raise OjArchiveError("Yonsei-OJ 로그인 CSRF 토큰을 찾지 못했습니다")
        page, _ = self._request(
            login_url,
            {
                "csrfmiddlewaretoken": token,
                "username": username,
                "password": password,
                "next": "/contests/",
            },
            referer=login_url,
        )
        if "Log out" not in page:
            raise OjArchiveError("Yonsei-OJ 로그인에 실패했습니다")
        self.save()

    def authenticated_page(self, path: str = "/contests/") -> str:
        page, _ = self._request(path)
        if "Log out" in page:
            return page
        username = os.environ.get("YONSEI_OJ_ID") or os.environ.get("LEARNUS_ID")
        password = os.environ.get("YONSEI_OJ_PW") or os.environ.get("LEARNUS_PW")
        if not username or not password:
            raise OjArchiveError(
                "Yonsei-OJ 자격 증명이 없습니다: "
                "YONSEI_OJ_ID/YONSEI_OJ_PW를 설정하세요"
            )
        self.login(username, password)
        page, _ = self._request(path)
        if "Log out" not in page:
            raise OjArchiveError("Yonsei-OJ 로그인 세션을 확인하지 못했습니다")
        return page

    def fetch_contest(self, assignment_title: str, *, auto_join: bool = False) -> OjContest:
        listing = self.authenticated_page("/contests/")
        contests = list({
            href: text
            for href, text in _links(listing)
            if re.fullmatch(r"/contest/[A-Za-z0-9_-]+", href)
        }.items())
        expected = _assignment_key(assignment_title)
        matches = [(href, text) for href, text in contests if _assignment_key(text) == expected]
        if len(matches) != 1:
            raise OjArchiveError(
                "과제 제목과 일치하는 Yonsei-OJ 콘테스트를 찾지 못했습니다: "
                f"{assignment_title}"
            )
        href, title = matches[0]
        page = self.authenticated_page(href)
        problem_paths = sorted(
            {
                link
                for link, _ in _links(page)
                if re.fullmatch(r"/problem/[A-Za-z0-9_-]+", link)
            }
        )
        join_path = f"{href}/join"
        join_form = re.search(
            r'<form\b[^>]*\baction=["\']'
            + re.escape(join_path)
            + r'["\'][^>]*>',
            page,
            re.I,
        )
        if not problem_paths and join_form:
            if not auto_join:
                raise OjParticipationRequired(
                    f"Yonsei-OJ 콘테스트 참여가 필요합니다: {title}"
                )
            token = parse_input_tags(page).get("csrfmiddlewaretoken")
            if not token:
                raise OjArchiveError("Yonsei-OJ 참여 CSRF 토큰을 찾지 못했습니다")
            page, _ = self._request(
                join_path,
                {"csrfmiddlewaretoken": token},
                referer=urllib.parse.urljoin(OJ_ORIGIN, href),
            )
            problem_paths = sorted(
                {
                    link
                    for link, _ in _links(page)
                    if re.fullmatch(r"/problem/[A-Za-z0-9_-]+", link)
                }
            )
        # 참여 뒤의 일반 콘테스트 주소는 안내 탭을 보여 줄 수 있다. 이때 공개
        # 랭킹의 문제별 열에서 코드를 얻어 문제 페이지 주소를 복원한다.
        if not problem_paths and f'{href}/leave' in page:
            ranking = self.authenticated_page(f"{href}/ranking/")
            codes = {
                match.group(1)
                for link, _ in _links(ranking)
                if (match := re.fullmatch(
                    re.escape(href) + r"/rank/([A-Za-z0-9_-]+)/", link
                ))
            }
            problem_paths = [f"/problem/{code}" for code in sorted(codes)]
        if not problem_paths:
            raise OjArchiveError(
                f"Yonsei-OJ 콘테스트 문제 목록이 비어 있습니다: {title}"
            )

        problems = []
        for path in problem_paths:
            problem_page = self.authenticated_page(path)
            problems.append(parse_problem(problem_page, urllib.parse.urljoin(OJ_ORIGIN, path)))
        self.save()
        return OjContest(
            slug=href.rsplit("/", 1)[-1],
            title=title,
            url=urllib.parse.urljoin(OJ_ORIGIN, href),
            problems=problems,
        )


def render_contest_markdown(contest: OjContest) -> str:
    lines = [
        "## Yonsei-OJ 상세 명세",
        "",
        f"- 콘테스트: {contest.title}",
        f"- 문제 수: {len(contest.problems)}",
        f"- 원문: {contest.url}",
    ]
    for number, problem in enumerate(contest.problems, 1):
        lines.extend([
            "",
            f"### OJ-{number}. {problem.title} (`{problem.code}`)",
            "",
            f"- 배점: {problem.points or '알 수 없음'}",
            f"- 시간 제한: {problem.time_limit or '알 수 없음'}",
            f"- 메모리 제한: {problem.memory_limit or '알 수 없음'}",
            f"- 허용 언어: {', '.join(problem.languages) or '알 수 없음'}",
            f"- 원문: {problem.url}",
            "",
            problem.statement,
        ])
    return "\n".join(lines).strip()


def enrich_with_yonsei_oj(
    assignment: LtiAssignment,
    activity_title: str,
    cookie_path: str | Path,
) -> tuple[LtiAssignment, OjContest | None]:
    if "yonsei-oj.duckdns.org" not in assignment.instructions:
        return assignment, None
    auto_join = os.environ.get("YONSTUDY_OJ_AUTO_JOIN", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }
    contest = YonseiOjClient(cookie_path).fetch_contest(
        activity_title or assignment.title,
        auto_join=auto_join,
    )
    markdown = render_contest_markdown(contest)
    escaped = html_mod.escape(markdown)
    return replace(
        assignment,
        instructions=f"{assignment.instructions}\n\n{markdown}".strip(),
        instructions_html=(
            f"{assignment.instructions_html}\n<h2>Yonsei-OJ 상세 명세</h2>"
            f"<pre>{escaped}</pre>"
        ),
    ), contest


__all__ = [
    "OJ_ORIGIN",
    "OjArchiveError",
    "OjContest",
    "OjParticipationRequired",
    "OjProblem",
    "YonseiOjClient",
    "enrich_with_yonsei_oj",
    "parse_problem",
    "render_contest_markdown",
]
