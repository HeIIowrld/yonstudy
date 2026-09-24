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
from .html_content import Element, clean_html, find_elements, html_to_markdown
from .lti import LtiAssignment
from .markdown_view import markdown_to_html


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
    statement_html: str = ""


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


def _plain(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html_mod.unescape(value)).strip()


def _has_class(attrs: dict, name: str) -> bool:
    return name in (attrs.get("class") or "").split()


def _links(page: str) -> list[tuple[str, str]]:
    parser = _LinkParser()
    parser.feed(page)
    parser.close()
    return parser.links


def _statement_fragment(description: Element) -> str:
    # Some DMOJ themes mark the statement; Yonsei's current theme uses an
    # anonymous div followed by a separator and a clarification action.
    bodies = description.find_all(
        lambda _tag, attrs: _has_class(attrs, "content-text")
        or _has_class(attrs, "problem-statement")
    )
    if bodies:
        return bodies[0].inner_html()
    for index, child in enumerate(description.children):
        if not isinstance(child, Element):
            continue
        if (
            _has_class(child.attrs, "clarify")
            or _has_class(child.attrs, "problem-actions")
            or (child.tag == "a" and re.search(r"/tickets/new/?(?:[?#].*)?$", child.attrs.get("href") or ""))
        ):
            children = description.children[:index]
            while children and (
                isinstance(children[-1], str) and not children[-1].strip()
                or isinstance(children[-1], Element) and children[-1].tag == "hr"
            ):
                children.pop()
            return Element("", children=children).inner_html()
    return description.inner_html()


def _statement_headings(fragment: str) -> str:
    headings = find_elements(fragment, lambda tag, _attrs: bool(re.fullmatch(r"h[1-6]", tag)))
    if not headings:
        return fragment
    # Each problem has an h3 in the combined assignment. Keep the relative
    # statement hierarchy underneath it, including sites that start with h5.
    offset = 4 - min(int(heading.tag[1]) for heading in headings)
    return re.sub(
        r"(<\s*/?\s*h)([1-6])(?=[\s>])",
        lambda match: match.group(1) + str(min(6, int(match.group(2)) + offset)),
        fragment,
        flags=re.I,
    )


def parse_problem(page: str, source_url: str) -> OjProblem:
    titles = find_elements(page, lambda _tag, attrs: _has_class(attrs, "problem-title"))
    headings = titles[0].find_all(lambda tag, _attrs: tag == "h2") if titles else []
    code_match = re.search(r"/problem/([^/?#]+)", source_url)
    if not headings or not code_match:
        raise OjArchiveError("Yonsei-OJ 문제 제목 또는 코드를 찾지 못했습니다")

    info: dict[str, str] = {}
    entries = find_elements(page, lambda _tag, attrs: _has_class(attrs, "problem-info-entry"))
    for entry in entries:
        names = entry.find_all(lambda _tag, attrs: _has_class(attrs, "pi-name"))
        values = entry.find_all(lambda _tag, attrs: _has_class(attrs, "pi-value"))
        if names and values:
            info[_plain(names[0].inner_html()).rstrip(":").lower()] = _plain(values[0].inner_html())

    allowed = find_elements(page, lambda _tag, attrs: attrs.get("id") == "allowed-langs")
    languages: list[str] = []
    if allowed:
        toggled = allowed[0].find_all(lambda _tag, attrs: _has_class(attrs, "toggled"))
        if toggled:
            languages = [x.strip() for x in _plain(toggled[0].inner_html()).split(",") if x.strip()]

    descriptions = find_elements(
        page, lambda _tag, attrs: _has_class(attrs, "content-description")
    )
    if not descriptions:
        raise OjArchiveError("Yonsei-OJ 문제 본문을 찾지 못했습니다")
    description = next(
        (item for item in descriptions if _has_class(item.attrs, "screen")), descriptions[0]
    )
    statement_html = clean_html(_statement_headings(_statement_fragment(description)), source_url)
    statement = html_to_markdown(statement_html, source_url)
    if not statement:
        raise OjArchiveError("Yonsei-OJ 문제 본문이 비어 있습니다")

    return OjProblem(
        code=code_match.group(1),
        title=_plain(headings[0].inner_html()),
        url=source_url,
        points=info.get("points"),
        time_limit=info.get("time limit"),
        memory_limit=info.get("memory limit"),
        languages=languages,
        statement=statement,
        statement_html=statement_html,
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


def render_contest_html(contest: OjContest) -> str:
    escape = html_mod.escape
    lines = [
        "<h2>Yonsei-OJ 상세 명세</h2>",
        "<ul>",
        f"<li>콘테스트: {escape(contest.title)}</li>",
        f"<li>문제 수: {len(contest.problems)}</li>",
        f'<li>원문: <a href="{escape(contest.url)}">{escape(contest.url)}</a></li>',
        "</ul>",
    ]
    for number, problem in enumerate(contest.problems, 1):
        lines.extend([
            f"<h3>OJ-{number}. {escape(problem.title)} (<code>{escape(problem.code)}</code>)</h3>",
            "<ul>",
            f"<li>배점: {escape(problem.points or '알 수 없음')}</li>",
            f"<li>시간 제한: {escape(problem.time_limit or '알 수 없음')}</li>",
            f"<li>메모리 제한: {escape(problem.memory_limit or '알 수 없음')}</li>",
            f"<li>허용 언어: {escape(', '.join(problem.languages) or '알 수 없음')}</li>",
            f'<li>원문: <a href="{escape(problem.url)}">{escape(problem.url)}</a></li>',
            "</ul>",
            problem.statement_html or markdown_to_html(problem.statement, problem.url),
        ])
    return clean_html("\n".join(lines), contest.url)


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
    return replace(
        assignment,
        instructions=f"{assignment.instructions}\n\n{markdown}".strip(),
        instructions_html=(
            f"{assignment.instructions_html}\n{render_contest_html(contest)}"
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
    "render_contest_html",
    "render_contest_markdown",
]
