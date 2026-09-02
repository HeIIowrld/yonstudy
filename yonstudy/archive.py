"""아카이버 — 강좌를 순회하며 활동/진도/과제/자막을 수집한다.

원칙:
  * 읽기 전용. 글쓰기·제출·설정 변경 요청은 이 모듈에 없다.
  * 증분. 이미 받은 파일(url+role)은 건너뛴다.
  * 영상 본체는 기본적으로 받지 않는다 (`--with-video`로만 활성).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict
from urllib.parse import unquote

from . import parse as P
from .client import LEARNUS, LearnUsClient
from .store import Store, _now

MAX_INLINE_FILE = int(os.environ.get("YONSTUDY_MAX_FILE_MB", "512")) * 1024 * 1024


class SessionExpired(RuntimeError):
    """세션이 실제로 끊긴 경우. 남은 강좌를 계속 시도해 봐야 의미가 없다."""

class Archiver:
    def __init__(self, client: LearnUsClient, store: Store, verbose: bool = True):
        self.c = client
        self.s = store
        self.verbose = verbose
        self._user_id: str | None = None

    @property
    def user_id(self) -> str | None:
        """내 Moodle userid. VPL 제출 화면·포럼 내 글 조회에 필요하다."""
        if self._user_id is None:
            try:
                self._user_id = P.find_user_id(self.c.request(f"{LEARNUS}/user/profile.php")) or ""
            except Exception:
                self._user_id = ""
        return self._user_id or None

    def say(self, *a) -> None:
        if self.verbose:
            print(*a, flush=True)

    # ------------------------------------------------------------------
    # 강좌 목록
    # ------------------------------------------------------------------

    def sync_courses(self) -> list[P.Course]:
        page = self.c.request(
            f"{LEARNUS}/local/ubion/user/index.php?year=all&semester=all"
        )
        if "/login/logout.php" not in page:
            raise SessionExpired("세션이 만료되었습니다. `yonstudy login`을 먼저 실행하세요.")
        courses = P.parse_course_list(page)
        for course in courses:
            row = asdict(course)
            row.update(slug=course.slug, archived_at=_now())
            self.s.save_course(row)
        self.s.commit()
        self.say(f"강좌 {len(courses)}개 동기화")
        return courses

    # ------------------------------------------------------------------
    # 강좌 1개
    # ------------------------------------------------------------------

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

        for a in activities:
            row = asdict(a)
            row.pop("viewer_url", None)
            row.update(course_id=course.course_id, restricted=int(a.restricted), seen_at=_now())
            self.s.save_activity(row)
        self.s.commit()

        counts: dict[str, int] = {}
        for a in activities:
            counts[a.modname] = counts.get(a.modname, 0) + 1
        self.say("  활동: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))

        # 진도 리포트 — 동영상별 시청시간의 단일 진실 소스
        progress_by_title: dict[str, P.ProgressRow] = {}
        try:
            rep = self.c.request(
                f"{LEARNUS}/report/ubcompletion/user_progress.php?id={course.course_id}",
                referer=course.url,
            )
            _, rows = P.parse_progress_report(rep)
            progress_by_title = {r.title.strip(): r for r in rows}
            done = sum(r.done for r in rows)
            if rows:
                self.say(f"  진도: {done}/{len(rows)} 완료")
        except Exception as exc:  # 진도 리포트가 없는 강좌도 있다
            self.s.log("progress", str(course.course_id), False, str(exc))

        # 동영상: 뷰어를 열어 HLS/자막/배속정책/진도기간을 확인 (본체는 받지 않음)
        vods = [a for a in activities if a.modname == "vod"]
        for a in vods:
            self._sync_vod(course, a, progress_by_title, cdir, probe_vod, fetch_subtitles)

        # 제출형 활동 — assign 뿐 아니라 turnitin/vpl/quiz/feedback/choice/forum 전부
        for a in (x for x in activities if x.is_submission and not x.restricted):
            self._sync_submission(course, a, cdir, fetch_files)

        # 게시판(공지·Q&A)과 포럼 — 강의 내용의 상당 부분이 여기 있다.
        if fetch_boards:
            for a in (x for x in activities if x.modname == "ubboard" and not x.restricted):
                self._sync_board(course, a, cdir, board_pages, fetch_files)
            for a in (x for x in activities if x.modname == "forum" and not x.restricted):
                self._sync_forum(course, a, cdir, fetch_files)

        # 포럼은 "제출" 개념이 없다. 강좌 단위로 내가 쓴 글도 따로 모아 둔다.
        if any(x.modname == "forum" for x in activities):
            self._sync_forum_posts(course, cdir)

        # 자료 — ubfile / folder / resource
        if fetch_files:
            for a in (x for x in activities if x.modname in P.RESOURCE_MODULES and not x.restricted):
                self._sync_resource(course, a, cdir)

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

    # ------------------------------------------------------------------

    def _fetch_course_page(self, course, tries: int = 4) -> str:
        """강좌 페이지를 받되, 세션 만료와 일시적 차단을 구분한다.

        실측으로 배운 것 두 가지:
          * `logout.php` 문자열이 없다고 세션 만료가 아니다. 응답이 이상하기만 해도
            그렇게 보이므로, 멀쩡한 세션에서 44개 강좌가 연속 실패한 적이 있다.
          * LearnUs는 요청이 몰리면 **HTTP 400을 잠깐 돌려준다.** 이건 레이트 리밋이지
            세션 만료가 아니다. 실제로 400을 만난 두 강좌 모두 잠시 뒤 정상 로드됐다.
        그래서 로그인 리다이렉트만 만료로 보고, 나머지는 넉넉히 쉬었다 다시 시도한다.
        """
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
            if "login" in final.rsplit("/", 1)[-1] or 'class="html_login"' in page:
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

    def _sync_vod(self, course, a, progress_by_title, cdir, probe, fetch_subtitles) -> None:
        prow = progress_by_title.get(a.title.strip())
        row = {
            "cmid": a.cmid,
            "course_id": course.course_id,
            "duration_sec": prow.duration_sec if prow else None,
            "watched_sec": prow.watched_sec if prow else None,
            "progress_pct": (
                float(prow.progress.rstrip("%")) if prow and prow.progress.rstrip("%").replace(".", "").isdigit() else None
            ),
            "probed_at": _now(),
        }
        if probe:
            try:
                html = self.c.request(a.viewer_url, referer=course.url)
                v = P.parse_vod_viewer(html, a.cmid)
                row.update(
                    uuid=v.uuid,
                    hls_url=v.hls_url,
                    poster=v.poster,
                    subtitle_langs=",".join(v.subtitle_langs),
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
            except Exception as exc:
                row["status"] = "error"
                self.s.log("vod", str(a.cmid), False, str(exc))
        self.s.save_vod(row)

    def _fetch_subtitle(self, course, a, uuid: str, lang: str, cdir: str) -> None:
        url = f"{LEARNUS}/mod/vod/subtitle_auto.php?uuid={uuid}&language={lang}"
        if self.s.has_file(url, "subtitle"):
            return
        try:
            body, _ = self.c.get_bytes(url, referer=a.viewer_url)
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
            html = self.c.request(a.url, referer=course.url)
        except Exception as exc:
            self.s.log(a.modname, str(a.cmid), False, str(exc))
            return
        # VPL은 view.php가 아니라 제출 화면에 다운로드 링크가 있다.
        # view.php 안에 내 userid가 박혀 있으므로 그것으로 한 번 더 들어간다.
        if a.modname == "vpl":
            uid = P.find_user_id(html)
            if uid:
                try:
                    html = self.c.request(
                        f"{LEARNUS}/mod/vpl/forms/submissionview.php?id={a.cmid}&userid={uid}",
                        referer=a.url,
                    )
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

        목록은 매번 다시 읽어야 새 글을 알 수 있지만, 본문은 이미 받은 글을 건너뛴다.
        """
        page_no, last_page, new_posts, total = 1, 1, 0, 0
        # 과거 용량 제한이나 일시적 오류로 첨부 blob만 비어 있을 수 있다.
        # 이 경우에만 기존 글도 다시 열어 첨부 URL을 복구한다.
        retry_attachments = fetch_files and self.s.has_missing_file(a.cmid, "post")
        while page_no <= last_page:
            url = a.url if page_no == 1 else f"{a.url}&page={page_no}"
            try:
                html = self.c.request(url, referer=course.url)
            except Exception as exc:
                self.s.log("ubboard", f"{a.cmid}p{page_no}", False, str(exc))
                break
            listing, parsed_last = P.parse_ubboard_list(html)
            if page_no == 1:
                last_page = min(parsed_last, max_pages) if max_pages else parsed_last
            total += len(listing)

            for post in listing:
                known_post = self.s.has_post(a.cmid, "ubboard", post.post_id)
                if known_post and not retry_attachments:
                    continue
                try:
                    body_html = self.c.request(post.url, referer=url)
                    post = P.parse_ubboard_article(body_html, post)
                except Exception as exc:
                    self.s.log("ubboard_article", post.post_id, False, str(exc))
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
            html = self.c.request(a.url, referer=course.url)
        except Exception as exc:
            self.s.log("forum", str(a.cmid), False, str(exc))
            return
        threads = P.parse_forum_discussions(html)
        saved = 0
        for thread_id, _title in threads:
            if self.s.has_post(a.cmid, "forum", f"t{thread_id}"):
                continue
            try:
                page = self.c.request(
                    f"{LEARNUS}/mod/forum/discuss.php?d={thread_id}", referer=a.url
                )
            except Exception as exc:
                self.s.log("forum_discuss", thread_id, False, str(exc))
                continue
            posts = P.parse_forum_discussion(page)
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
            html = self.c.request(
                f"{LEARNUS}/mod/forum/user.php?id={uid}&course={course.course_id}",
                referer=course.url,
            )
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

        ubfile/resource의 view.php는 HTML을 주지 않고 pluginfile로 **바로 리다이렉트**한다
        (실측 확인). 따라서 먼저 바이트로 받아 보고, HTML이면 그때 링크를 파싱한다.
        """
        # 이미 받아둔 자료면 네트워크를 아예 건드리지 않는다.
        existing = self.s.file_record(a.url, "resource")
        if existing and (existing["sha256"] or (existing["bytes"] or 0) > MAX_INLINE_FILE):
            return
        try:
            body, final = self.c.get_bytes(a.url, referer=course.url)
        except Exception as exc:
            self.s.log("resource", str(a.cmid), False, str(exc))
            return

        if "/pluginfile.php/" in final:
            name = unquote(final.rsplit("/", 1)[-1].split("?")[0]) or f"{a.title}.bin"
            self._save_bytes(course, a, body, a.url, name, "resource", cdir, "materials")
            return

        head = body[:512].lstrip().lower()
        if head.startswith(b"<!doc") or b"<html" in head:
            for name, url in P.parse_pluginfiles(body.decode("utf-8", "ignore")):
                self._fetch_file(course, a, url, name, "resource", cdir, "materials")
        else:
            self._save_bytes(course, a, body, a.url, a.title, "resource", cdir, "materials")

    def _fetch_file(self, course, a, url, name, role, cdir, subdir) -> None:
        existing = self.s.file_record(url, role)
        if existing and (existing["sha256"] or (existing["bytes"] or 0) > MAX_INLINE_FILE):
            return
        try:
            body, _ = self.c.get_bytes(url, referer=a.url)
        except Exception as exc:
            self.s.log("file", url[:120], False, str(exc))
            return
        self._save_bytes(course, a, body, url, name, role, cdir, subdir)

    def _save_bytes(self, course, a, body, url, name, role, cdir, subdir) -> None:
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
        safe = re.sub(r'[\\/:*?"<>|]', "_", name)[:80]
        self.s.link_into_course(digest, f"{cdir}/{subdir}", safe)
        self.s.save_file(
            {
                "course_id": course.course_id, "cmid": a.cmid, "role": role,
                "name": safe, "url": url, "sha256": digest, "bytes": size,
                "saved_at": _now(),
            }
        )
        self.say(f"      파일 {safe} ({size//1024}KB)")
