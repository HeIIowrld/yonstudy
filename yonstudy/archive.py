"""강좌를 순회하며 활동, 진도, 제출, 자막을 증분 수집한다."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

from . import parse as P
from .client import LEARNUS, LearnUsClient
from .store import Store, _now

MAX_INLINE_FILE = int(os.environ.get("YONSTUDY_MAX_FILE_MB", "512")) * 1024 * 1024
POST_REFRESH_AFTER = timedelta(days=7)
FORUM_REFRESH_AFTER = timedelta(days=1)


def _activity_week(a) -> str:
    """진도표의 주차 값과 비교할 활동 주차를 정규화한다."""
    section_name = getattr(a, "section_name", "") or ""
    section_idx = getattr(a, "section_idx", None)
    match = re.search(r"(?:week\s*)?(\d{1,2})\s*(?:주차)?", section_name, re.I)
    if match:
        return str(int(match.group(1)))
    return str(int(section_idx)) if section_idx is not None else ""


def _match_progress_rows(activities, rows) -> dict[int, P.ProgressRow]:
    """제목이 같은 강의도 주차를 포함해 안전하게 진도 행과 연결한다."""
    vods = [a for a in activities if a.modname == "vod"]
    buckets: dict[tuple[str, str], list[P.ProgressRow]] = {}
    for row in rows:
        week = str(int(row.week)) if str(row.week).isdigit() else str(row.week).strip()
        buckets.setdefault((week, row.title.strip()), []).append(row)

    matched: dict[int, P.ProgressRow] = {}
    used: set[int] = set()
    for activity in vods:
        candidates = buckets.get((_activity_week(activity), activity.title.strip()), [])
        if candidates:
            row = candidates.pop(0)
            matched[activity.cmid] = row
            used.add(id(row))

    # 주차 표기가 없는 강좌는 제목이 양쪽에서 하나씩만 남을 때만 연결한다.
    remaining_rows: dict[str, list[P.ProgressRow]] = {}
    for row in rows:
        if id(row) not in used:
            remaining_rows.setdefault(row.title.strip(), []).append(row)
    remaining_vods: dict[str, list] = {}
    for activity in vods:
        if activity.cmid not in matched:
            remaining_vods.setdefault(activity.title.strip(), []).append(activity)
    for title, candidates in remaining_rows.items():
        activities_with_title = remaining_vods.get(title, [])
        if len(candidates) == len(activities_with_title) == 1:
            matched[activities_with_title[0].cmid] = candidates[0]
    return matched


def _refresh_due(
    value: str | None, after: timedelta = POST_REFRESH_AFTER
) -> bool:
    if not value:
        return True
    try:
        return datetime.fromisoformat(_now()) - datetime.fromisoformat(value) >= after
    except ValueError:
        return True


class SessionExpired(RuntimeError):
    """세션이 실제로 끊긴 경우. 남은 강좌를 계속 시도해 봐야 의미가 없다."""


def _is_login_response(body: str, final_url: str = "") -> bool:
    """로그인 리다이렉트 결과인지 본문과 최종 URL로 판정한다."""
    path = final_url.lower().split("?", 1)[0].rstrip("/")
    if path.endswith(("/login", "/login/index.php")):
        return True
    return bool(
        re.search(r"\bclass=['\"][^'\"]*\bhtml_login\b", body, re.I)
        or (
            re.search(r"<form\b[^>]*\baction=['\"][^'\"]*/login/", body, re.I)
            and re.search(r"\bname=['\"](?:username|user_id)['\"]", body, re.I)
        )
    )


class Archiver:
    def __init__(
        self, client: LearnUsClient, store: Store, verbose: bool = True,
        file_sink=None,
    ):
        self.c = client
        self.s = store
        self.verbose = verbose
        self.file_sink = file_sink
        self._user_id: str | None = None

    def _request(self, url: str, **kwargs) -> str:
        body = self.c.request(url, **kwargs)
        if _is_login_response(body):
            raise SessionExpired("LearnUs 세션이 만료되었습니다")
        return body

    def _get_bytes(self, url: str, **kwargs) -> tuple[bytes, str]:
        body, final = self.c.get_bytes(url, **kwargs)
        head = body[:16_384].decode("utf-8", "ignore")
        if _is_login_response(head, final):
            raise SessionExpired("LearnUs 세션이 만료되었습니다")
        return body, final

    @property
    def user_id(self) -> str | None:
        """내 Moodle userid. VPL 제출 화면·포럼 내 글 조회에 필요하다."""
        if self._user_id is None:
            try:
                self._user_id = P.find_user_id(
                    self._request(f"{LEARNUS}/user/profile.php")
                ) or ""
            except SessionExpired:
                raise
            except Exception:
                self._user_id = ""
        return self._user_id or None

    def say(self, *a) -> None:
        if self.verbose:
            print(*a, flush=True)

    # 강좌 목록

    def sync_courses(self) -> list[P.Course]:
        page = self._request(
            f"{LEARNUS}/local/ubion/user/index.php?year=all&semester=all"
        )
        if "/login/logout.php" not in page:
            raise SessionExpired("세션이 만료되었습니다. `yonstudy login`을 먼저 실행하세요.")
        if not P.has_course_list(page):
            raise RuntimeError("강좌 목록을 확인하지 못했습니다")
        courses = P.parse_course_list(page)
        if not courses and self.s.query("SELECT 1 FROM course LIMIT 1"):
            raise RuntimeError("기존 강좌가 있지만 새 강좌 목록이 비어 있어 동기화를 중단합니다")
        synced_at = _now()
        for course in courses:
            row = asdict(course)
            row.update(
                slug=course.slug,
                archived_at=synced_at,
                enrolled=1,
                unenrolled_at=None,
            )
            self.s.save_course(row)
        self.s.reconcile_enrollment(
            {course.course_id for course in courses}, synced_at=synced_at
        )
        self.s.commit()
        self.s.prune_monitor_state()
        self.say(f"강좌 {len(courses)}개 동기화")
        return courses

    # 강좌 상세

    def sync_course(
        self,
        course: P.Course,
        *,
        probe_vod: bool = True,
        fetch_subtitles: bool = True,
        fetch_files: bool = True,
        fetch_boards: bool = True,
        board_pages: int = 3,
    ) -> dict:
        cdir = f"{course.year}-{course.semester}/{course.slug}"
        self.say(f"\n[{course.year} {course.semester}] {course.name} (cid={course.course_id})")

        page = self._fetch_course_page(course)
        activities, _ = P.parse_course_page(page)
        listed_cmids = P.find_activity_ids(page)
        parsed_cmids = {activity.cmid for activity in activities}
        if listed_cmids != parsed_cmids:
            missing = sorted(listed_cmids - parsed_cmids)
            raise RuntimeError(
                "강좌 활동 목록을 완전히 해석하지 못했습니다"
                + (f" (cmid={missing[:5]})" if missing else "")
            )

        synced_at = _now()
        for a in activities:
            row = asdict(a)
            row.pop("viewer_url", None)
            row.update(
                course_id=course.course_id,
                restricted=int(a.restricted),
                seen_at=synced_at,
                present=1,
                removed_at=None,
            )
            self.s.save_activity(row)
        self.s.reconcile_activities(
            course.course_id, parsed_cmids, synced_at=synced_at
        )
        self.s.commit()

        counts: dict[str, int] = {}
        for a in activities:
            counts[a.modname] = counts.get(a.modname, 0) + 1
        self.say("  활동: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))

        # 시청 시간은 강좌 페이지가 아니라 진도 리포트를 기준으로 한다.
        progress_by_cmid: dict[int, P.ProgressRow] = {}
        try:
            rep = self._request(
                f"{LEARNUS}/report/ubcompletion/user_progress.php?id={course.course_id}",
                referer=course.url,
            )
            _, rows = P.parse_progress_report(rep)
            progress_by_cmid = _match_progress_rows(activities, rows)
            done = sum(r.done for r in rows)
            if rows:
                self.say(f"  진도: {done}/{len(rows)} 완료")
        except SessionExpired:
            raise
        except Exception as exc:  # 진도 리포트가 없는 강좌도 있다
            self.s.log("progress", str(course.course_id), False, str(exc))

        # 뷰어에서 HLS, 자막, 배속, 진도 기간만 확인한다.
        vods = [a for a in activities if a.modname == "vod"]
        for a in vods:
            self._sync_vod(course, a, progress_by_cmid, cdir, probe_vod, fetch_subtitles)

        # 과제 모듈마다 페이지가 달라 공통 파서로 정규화한다.
        for a in (x for x in activities if x.is_submission and not x.restricted):
            self._sync_submission(course, a, cdir, fetch_files)

        # 공지와 Q&A는 ubboard와 forum 모두에 올라온다.
        if fetch_boards:
            for a in (x for x in activities if x.modname == "ubboard" and not x.restricted):
                self._sync_board(course, a, cdir, board_pages, fetch_files)
            for a in (x for x in activities if x.modname == "forum" and not x.restricted):
                self._sync_forum(course, a, cdir, fetch_files)

        # 포럼의 내 글은 강좌 단위 페이지에서 따로 받는다.
        if any(x.modname == "forum" for x in activities):
            self._sync_forum_posts(course, cdir)

        # ubfile, folder, resource에 붙은 자료
        if fetch_files:
            for a in (x for x in activities if x.modname in P.RESOURCE_MODULES and not x.restricted):
                self._sync_resource(course, a, cdir)

        self.s.mark_course_detail_synced(course.course_id)
        self.s.commit()
        self.s.write_json(
            f"courses/{cdir}/course.json",
            {
                "course": asdict(course),
                "activities": [asdict(a) for a in activities],
                "archived_at": _now(),
            },
        )
        return counts

    def _fetch_course_page(self, course, tries: int = 4) -> str:
        """로그인 리다이렉트는 세션 만료로, 나머지 이상 응답은 재시도 대상으로 본다."""
        delay = 20.0
        last = ""
        for attempt in range(1, tries + 1):
            try:
                page, final = self.c.fetch(course.url)
            except Exception as exc:
                last = str(exc)
                page, final = "", ""
            if "/login/logout.php" in page:
                return page
            if _is_login_response(page, final):
                raise SessionExpired("LearnUs 세션이 만료되었습니다")
            if attempt < tries:
                self.say(f"    응답 이상({last or '내용 불일치'}) — {delay:.0f}초 대기 후 재시도 {attempt}/{tries}")
                # 차단은 분 단위로 지속된다(실측: 75초 대기로는 안 풀림). 넉넉히 쉬고
                # 토큰 창도 비워서 재개 직후 다시 몰아치지 않게 한다.
                self.c.cool_down(delay)
                delay *= 3  # 20 → 60 → 180 → 540초

        # 여기까지 왔으면 세션 상태를 직접 확인한다.
        # 확인 자체가 실패하면 '만료'라고 단정하지 않는다.
        try:
            alive, _ = self.c.session_info()
        except Exception:
            raise RuntimeError(f"서버 응답 이상이 계속됩니다 ({last}). 잠시 후 다시 실행하세요.")
        if not alive:
            raise SessionExpired("LearnUs 세션이 만료되었습니다")
        raise RuntimeError(f"강좌 페이지를 받지 못했습니다 ({last or '알 수 없는 응답'})")

    def _sync_vod(self, course, a, progress_by_cmid, cdir, probe, fetch_subtitles) -> None:
        prow = progress_by_cmid.get(a.cmid)
        row = {
            "cmid": a.cmid,
            "course_id": course.course_id,
            "probed_at": _now(),
        }
        # 진도표 요청 실패나 일시적인 제목 불일치가 기존 진도를 NULL로 지우지 않게
        # 실제로 대응되는 행을 찾았을 때만 진도 필드를 갱신한다.
        if prow:
            raw_progress = prow.progress.strip().rstrip("%").strip()
            row.update(
                duration_sec=prow.duration_sec,
                watched_sec=prow.watched_sec,
                progress_pct=(
                    float(raw_progress)
                    if raw_progress.replace(".", "").isdigit()
                    else None
                ),
            )
        if probe:
            try:
                html = self._request(a.viewer_url, referer=course.url)
                v = P.parse_vod_viewer(html, a.cmid)
                row.update(
                    uuid=v.uuid,
                    hls_url=v.hls_url,
                    poster=v.poster,
                    subtitle_langs=",".join(v.subtitle_langs),
                    is_progress=(
                        None if v.progress.get("is_progress") is None
                        else int(bool(v.progress.get("is_progress")))
                    ),
                    progress_period=(
                        None if v.progress.get("progress_period") is None
                        else int(bool(v.progress.get("progress_period")))
                    ),
                    can_log_progress=int(v.can_log_progress),
                    max_rate=v.max_rate,
                    seek_restricted=int(v.seek_restricted),
                    trackid=str(v.progress.get("trackid") or ""),
                    attempt=v.progress.get("attempt"),
                    checker_url=v.progress.get("checker_url"),
                    status=v.status,
                )
                if fetch_subtitles and v.uuid:
                    for lang in v.subtitle_langs or ["ko"]:
                        self._fetch_subtitle(course, a, v.uuid, lang, cdir)
                self.s.log("vod", str(a.cmid), True)
            except SessionExpired:
                raise
            except Exception as exc:
                row["status"] = "error"
                self.s.log("vod", str(a.cmid), False, str(exc))
        self.s.save_vod(row)

    def _fetch_subtitle(self, course, a, uuid: str, lang: str, cdir: str) -> None:
        url = f"{LEARNUS}/mod/vod/subtitle_auto.php?uuid={uuid}&language={lang}"
        if self.s.has_file(url, "subtitle"):
            return
        try:
            body, _ = self._get_bytes(url, referer=a.viewer_url)
        except SessionExpired:
            raise
        except Exception as exc:
            self.s.log("subtitle", str(a.cmid), False, str(exc))
            return
        if not body.lstrip().startswith(b"WEBVTT"):
            return
        digest, size = self.s.put_blob(body)
        safe = re.sub(r'[\\/:*?"<>|]', "_", a.title)[:60].strip()
        name = f"{safe}.{lang}.vtt"
        self.s.link_into_course(digest, f"{cdir}/subtitles", name)
        self.s.save_file(
            {
                "course_id": course.course_id, "cmid": a.cmid, "role": "subtitle",
                "name": name, "url": url, "sha256": digest, "bytes": size,
                "saved_at": _now(),
            }
        )
        self.s.save_transcript(
            {
                "cmid": a.cmid, "source": "learnus_auto", "lang": lang,
                "path": f"courses/{cdir}/subtitles/{name}",
                "segments": body.count(b"-->"), "created_at": _now(),
            }
        )
        self.say(f"    자막 {a.title[:40]!r} [{lang}] {size//1024}KB")

    def _sync_submission(self, course, a, cdir, fetch_files) -> None:
        """제출형 활동 아카이빙 — 모듈 종류에 관계없이 같은 테이블로 모은다."""
        try:
            html = self._request(a.url, referer=course.url)
        except SessionExpired:
            raise
        except Exception as exc:
            self.s.log(a.modname, str(a.cmid), False, str(exc))
            return
        # VPL은 view.php가 아니라 제출 화면에 다운로드 링크가 있다.
        # view.php 안에 내 userid가 박혀 있으므로 그것으로 한 번 더 들어간다.
        if a.modname == "vpl":
            uid = P.find_user_id(html)
            if uid:
                try:
                    html = self._request(
                        f"{LEARNUS}/mod/vpl/forms/submissionview.php?id={a.cmid}&userid={uid}",
                        referer=a.url,
                    )
                except SessionExpired:
                    raise
                except Exception as exc:
                    self.s.log("vpl", str(a.cmid), False, str(exc))

        d = P.parse_submission(html, a.cmid, a.modname)
        f = d.fields
        first = lambda *keys: next((f[k] for k in keys if f.get(k)), None)  # noqa: E731
        self.s.save_submission(
            {
                "cmid": a.cmid, "course_id": course.course_id,
                "modname": a.modname, "title": a.title,
                "status": d.status_text or None,
                "grading_status": first("Grading status", "채점 상황", "채점 상태"),
                "due_at": first("Due date", "종료 일시", "마감 일시", "Close the quiz"),
                "last_modified": first("Last modified", "최종 수정 일시", "Completed on"),
                "grade": first("Grade", "성적", "Marks", "점수"),
                "fields_json": json.dumps(f, ensure_ascii=False),
                "submitted": int(d.submitted), "seen_at": _now(),
            }
        )
        mark = "제출함" if d.submitted else "미제출"
        self.say(
            f"    [{a.modname}] {a.title[:34]!r} — {mark}"
            + (f", 제출파일 {len(d.submitted_files)}개" if d.submitted_files else "")
        )
        if fetch_files:
            sub = f"submissions/{a.modname}"
            for name, url in d.submitted_files:
                self._fetch_file(course, a, url, name, "submission", cdir, sub)
            for name, url in d.intro_files:
                self._fetch_file(course, a, url, name, "introattachment", cdir, sub)

    def _sync_board(self, course, a, cdir, max_pages: int, fetch_files: bool) -> None:
        """ubboard 게시판 — 목록을 페이지별로 훑고 새 글의 본문을 받는다.

        목록은 매번 다시 읽어 새 글과 메타데이터 변경을 찾는다. 실패했거나 변경된
        본문은 즉시 재시도하고, 오래된 본문도 주기적으로 다시 확인한다.
        """
        page_no, last_page, new_posts, total = 1, 1, 0, 0
        # 과거 용량 제한이나 일시적 오류로 첨부 blob만 비어 있을 수 있다.
        # 이 경우에만 기존 글도 다시 열어 첨부 URL을 복구한다.
        retry_attachments = fetch_files and self.s.has_missing_file(a.cmid, "post")
        while page_no <= last_page:
            url = a.url if page_no == 1 else f"{a.url}&page={page_no}"
            try:
                html = self._request(url, referer=course.url)
            except SessionExpired:
                raise
            except Exception as exc:
                self.s.log("ubboard", f"{a.cmid}p{page_no}", False, str(exc))
                break
            listing, parsed_last = P.parse_ubboard_list(html)
            if page_no == 1:
                last_page = min(parsed_last, max_pages) if max_pages else parsed_last
            total += len(listing)

            for post in listing:
                existing = self.s.post_record(a.cmid, "ubboard", post.post_id)
                known_post = existing is not None
                listing_changed = bool(
                    existing
                    and any(
                        existing[key] != getattr(post, key)
                        for key in ("subject", "writer", "written_at", "replies")
                    )
                )
                needs_refresh = bool(
                    not existing
                    or not existing["body"]
                    or listing_changed
                    or retry_attachments
                    or (
                        page_no == 1
                        and _refresh_due(existing["checked_at"] or existing["fetched_at"])
                    )
                )
                if not needs_refresh:
                    continue
                try:
                    body_html = self._request(post.url, referer=url)
                    post = P.parse_ubboard_article(body_html, post)
                except SessionExpired:
                    raise
                except Exception as exc:
                    self.s.log("ubboard_article", post.post_id, False, str(exc))
                    # 목록 행만 저장하면 다음 실행부터 known_post로 분류되어 본문을
                    # 영원히 재시도하지 못한다. 기존 정상 본문도 실패 응답으로 덮지 않는다.
                    continue
                if known_post and existing["body"] and not post.body:
                    self.s.log(
                        "ubboard_article", post.post_id, False,
                        "기존 본문이 있지만 새 응답에서 본문을 찾지 못했습니다",
                    )
                    continue
                self._save_post(course, a, post, "ubboard")
                if not known_post:
                    new_posts += 1
                if fetch_files:
                    for name, furl in post.attachments:
                        self._fetch_file(course, a, furl, name, "post", cdir, "boards")
            page_no += 1

        if total:
            self.s.commit()
            self.say(f"    [게시판] {a.title[:26]!r} 글 {total}건 (신규 {new_posts})")

    def _sync_forum(self, course, a, cdir, fetch_files: bool) -> None:
        """forum — 토론 목록 → 각 토론의 모든 글."""
        try:
            html = self._request(a.url, referer=course.url)
        except SessionExpired:
            raise
        except Exception as exc:
            self.s.log("forum", str(a.cmid), False, str(exc))
            return
        threads = P.parse_forum_discussions(html)
        saved = 0
        for thread_id, _title in threads:
            marker = self.s.post_record(a.cmid, "forum", f"t{thread_id}")
            if marker and not _refresh_due(
                marker["checked_at"] or marker["fetched_at"], FORUM_REFRESH_AFTER
            ):
                continue
            try:
                page = self._request(
                    f"{LEARNUS}/mod/forum/discuss.php?d={thread_id}", referer=a.url
                )
            except SessionExpired:
                raise
            except Exception as exc:
                self.s.log("forum_discuss", thread_id, False, str(exc))
                continue
            posts = P.parse_forum_discussion(page)
            if not posts:
                self.s.log("forum_discuss", thread_id, False, "글 본문을 찾지 못했습니다")
                continue
            for post in posts:
                post.url = f"{LEARNUS}/mod/forum/discuss.php?d={thread_id}#p{post.post_id}"
                self._save_post(course, a, post, "forum", thread_id=thread_id)
                saved += 1
                if fetch_files:
                    for name, furl in post.attachments:
                        self._fetch_file(course, a, furl, name, "post", cdir, "boards")
            # 토론 자체를 받았다는 표시 — 다음 실행에서 건너뛴다.
            self._save_post(
                course, a, P.Post(post_id=f"t{thread_id}", subject=_title), "forum",
                thread_id=thread_id,
            )
        if threads:
            self.s.commit()
            self.say(f"    [포럼] {a.title[:26]!r} 토론 {len(threads)}개, 글 {saved}건")

    def _save_post(self, course, a, post, modname: str, thread_id: str = "") -> None:
        self.s.save_post(
            {
                "course_id": course.course_id, "cmid": a.cmid, "modname": modname,
                "post_id": post.post_id, "thread_id": thread_id,
                "no": post.no, "subject": post.subject, "writer": post.writer,
                "written_at": post.written_at, "hits": post.hits,
                "replies": post.replies, "url": post.url, "body": post.body,
                "fetched_at": _now(),
            }
        )

    def _sync_forum_posts(self, course, cdir) -> None:
        """/mod/forum/user.php?id={userid}&course={cid} 가 내가 쓴 글을 모아 준다."""
        uid = self.user_id
        if not uid:
            return
        try:
            html = self._request(
                f"{LEARNUS}/mod/forum/user.php?id={uid}&course={course.course_id}",
                referer=course.url,
            )
        except SessionExpired:
            raise
        except Exception as exc:
            self.s.log("forum", str(course.course_id), False, str(exc))
            return
        posts = P.parse_forum_posts(html)
        if not posts:
            return
        self.s.write_json(f"courses/{cdir}/forum_posts.json", posts)
        self.say(f"    [forum] 내가 쓴 글 {len(posts)}건")

    def _sync_resource(self, course, a, cdir) -> None:
        """mod/ubfile 등 자료 활동.

        view.php가 pluginfile로 바로 이동할 수 있어 먼저 바이트로 받고, HTML인
        경우에만 본문에서 파일 링크를 찾는다. ubdoc 문서 뷰어는
        worker.php에서 원본 이름·다운로드 URL을 받아 별도로 처리한다.
        """
        # 이미 받아둔 자료면 네트워크를 아예 건드리지 않는다.
        existing = self.s.file_record(a.url, "resource")
        if self._file_is_current(course, a, existing, "resource", a.title):
            return
        try:
            body, final = self._get_bytes(a.url, referer=course.url)
            if urlsplit(final).path.rstrip("/") == "/local/ubdoc":
                body, name = self._download_ubdoc(
                    final, fallback_name=a.title
                )
                self._save_bytes(
                    course, a, body, a.url, name, "resource", cdir, "materials"
                )
                return
        except SessionExpired:
            raise
        except Exception as exc:
            self.s.log("resource", str(a.cmid), False, str(exc))
            return

        if "/pluginfile.php/" in final:
            name = unquote(final.rsplit("/", 1)[-1].split("?")[0]) or f"{a.title}.bin"
            self._save_bytes(course, a, body, a.url, name, "resource", cdir, "materials")
            return

        head = body[:512].lstrip().lower()
        if head.startswith(b"<!doc") or b"<html" in head:
            page = body.decode("utf-8", "ignore")
            viewer_url = P.find_ubfile_viewer(page)
            if viewer_url:
                try:
                    _, viewer_final = self._get_bytes(
                        viewer_url, referer=a.url
                    )
                    if urlsplit(viewer_final).path.rstrip("/") != "/local/ubdoc":
                        raise RuntimeError(
                            "ubfile 문서 뷰어가 ubdoc으로 연결되지 않았습니다"
                        )
                    original, name = self._download_ubdoc(
                        viewer_final, fallback_name=a.title
                    )
                    self._save_bytes(
                        course, a, original, a.url, name,
                        "resource", cdir, "materials",
                    )
                except SessionExpired:
                    raise
                except Exception as exc:
                    self.s.log("resource", str(a.cmid), False, str(exc))
                return
            for name, url in P.parse_pluginfiles(page):
                self._fetch_file(course, a, url, name, "resource", cdir, "materials")
        else:
            self._save_bytes(course, a, body, a.url, a.title, "resource", cdir, "materials")

    def _download_ubdoc(
        self, viewer_url: str, *, fallback_name: str
    ) -> tuple[bytes, str]:
        """Coursemos ubdoc 뷰어에서 다운로드가 허용된 원본을 받는다."""
        parsed = urlsplit(viewer_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        params = {
            key: (query.get(key) or [""])[0]
            for key in ("id", "tp", "pg", "item", "fid")
        }
        if not params["id"]:
            raise RuntimeError("ubdoc 문서 ID가 없습니다")

        result_text = self._request(
            urljoin(viewer_url, "/local/ubdoc/worker.php"),
            data={"job": "checkState", **params},
            referer=viewer_url,
        )
        try:
            result = json.loads(result_text)
            state = int(result.get("state_code"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("ubdoc 문서 상태 응답을 해석하지 못했습니다") from exc

        if state == 100:
            if str(result.get("file_download", "0")) != "1":
                raise RuntimeError("ubdoc 원본 다운로드가 허용되지 않았습니다")
            download_url = result.get("file_url")
            path = "/local/ubdoc/download.php"
            name = (
                result.get("file_realname")
                or result.get("file_name")
                or fallback_name
            )
        elif state == 300:
            # 뷰어 변환이 불가능한 파일은 공식 UI도 adownload로
            # 원본 다운로드 링크를 노출한다.
            download_url = None
            path = "/local/ubdoc/adownload.php"
            name = result.get("file_name") or fallback_name
        else:
            message = result.get("state_message") or f"상태 {state}"
            raise RuntimeError(f"ubdoc 문서가 준비되지 않았습니다: {message}")

        if not download_url:
            download_url = urlunsplit(
                (parsed.scheme, parsed.netloc, path, parsed.query, "")
            )
        download_url = urljoin(viewer_url, str(download_url))
        target = urlsplit(download_url)
        if target.scheme not in {"http", "https"} or target.netloc != parsed.netloc:
            raise RuntimeError("ubdoc 다운로드 URL의 출처가 올바르지 않습니다")

        body, _ = self._get_bytes(download_url, referer=viewer_url)
        if not body:
            raise RuntimeError("ubdoc 원본 파일이 비어 있습니다")
        return body, str(name)

    def _fetch_file(self, course, a, url, name, role, cdir, subdir) -> None:
        existing = self.s.file_record(url, role)
        if self._file_is_current(course, a, existing, role, name):
            return
        try:
            body, _ = self._get_bytes(url, referer=a.url)
        except SessionExpired:
            raise
        except Exception as exc:
            self.s.log("file", url[:120], False, str(exc))
            return
        self._save_bytes(course, a, body, url, name, role, cdir, subdir)

    def _file_is_current(self, course, a, existing, role, name) -> bool:
        if not existing:
            return False
        if self.file_sink is None:
            if existing["remote_status"] in {"error", "missing"}:
                return False
            return bool(existing["sha256"] or (existing["bytes"] or 0) > MAX_INLINE_FILE)
        desired = self.file_sink.file_path(
            year=course.year, semester=course.semester,
            course_slug=course.slug, activity_title=a.title,
            file_id=existing["id"], name=existing["name"] or name, role=role,
            section_idx=getattr(a, "section_idx", None),
            section_name=getattr(a, "section_name", None),
            open_from=getattr(a, "open_from", None), saved_at=existing["saved_at"],
        )
        previous = existing["remote_path"]
        if role == "resource" and previous and previous != desired:
            move = getattr(self.file_sink, "move", None)
            if move is not None and move(previous, desired, size=existing["bytes"]):
                self.s.update_file_remote(existing["url"], role, desired, "ok")
                return True
        relative = desired if role == "resource" else (previous or desired)
        if not self.file_sink.exists(relative, existing["bytes"]):
            return False
        self.s.update_file_remote(existing["url"], role, relative, "ok")
        return True

    def _save_bytes(self, course, a, body, url, name, role, cdir, subdir) -> None:
        safe = re.sub(r'[\\/:*?"<>|]', "_", name)[:80]
        if self.file_sink is not None:
            digest = hashlib.sha256(body).hexdigest()
            size = len(body)
            self.s.save_file(
                {
                    "course_id": course.course_id, "cmid": a.cmid, "role": role,
                    "name": safe, "url": url, "sha256": digest, "bytes": size,
                    "saved_at": _now(),
                }
            )
            record = self.s.file_record(url, role)
            desired = self.file_sink.file_path(
                year=course.year, semester=course.semester,
                course_slug=course.slug, activity_title=a.title,
                file_id=record["id"], name=safe, role=role,
                section_idx=getattr(a, "section_idx", None),
                section_name=getattr(a, "section_name", None),
                open_from=getattr(a, "open_from", None), saved_at=record["saved_at"],
            )
            relative = desired if role == "resource" else (record["remote_path"] or desired)
            try:
                uploaded = self.file_sink.upload_bytes(relative, body)
            except Exception:
                self.s.update_file_remote(url, role, relative, "error")
                raise
            self.s.update_file_remote(url, role, relative, "ok")
            action = "remote 저장" if uploaded else "remote에 이미 있음"
            self.say(f"      {action} {safe} ({size//1024}KB)")
            return
        if len(body) > MAX_INLINE_FILE:
            self.s.save_file(
                {
                    "course_id": course.course_id, "cmid": a.cmid, "role": role,
                    "name": name, "url": url, "sha256": None, "bytes": len(body),
                    "saved_at": _now(),
                }
            )
            self.say(f"      (건너뜀, {len(body)//1024//1024}MB) {name}")
            return
        digest, size = self.s.put_blob(body)
        self.s.link_into_course(digest, f"{cdir}/{subdir}", safe)
        self.s.save_file(
            {
                "course_id": course.course_id, "cmid": a.cmid, "role": role,
                "name": safe, "url": url, "sha256": digest, "bytes": size,
                "saved_at": _now(),
            }
        )
        self.say(f"      파일 {safe} ({size//1024}KB)")
