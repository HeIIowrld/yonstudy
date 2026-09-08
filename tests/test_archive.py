import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from yonstudy.archive import Archiver, SessionExpired, _match_progress_rows
from yonstudy.parse import Activity, Course, Post, ProgressRow
from yonstudy.store import Store


class ArchiveProgressTests(unittest.TestCase):
    def test_duplicate_titles_are_matched_by_week(self):
        activities = [
            Activity(
                cmid=10,
                modname="vod",
                title="강의",
                url="https://example.test/10",
                section_idx=1,
                section_name="1주차",
            ),
            Activity(
                cmid=20,
                modname="vod",
                title="강의",
                url="https://example.test/20",
                section_idx=2,
                section_name="2주차",
            ),
        ]
        rows = [
            ProgressRow("1", "강의", "10:00", "10:00", "100%"),
            ProgressRow("2", "강의", "20:00", "05:00", "25%"),
        ]

        matched = _match_progress_rows(activities, rows)

        self.assertEqual(matched[10].progress, "100%")
        self.assertEqual(matched[20].progress, "25%")

    def test_missing_progress_row_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_activity(
                {
                    "cmid": 10,
                    "course_id": 1,
                    "modname": "vod",
                    "title": "강의",
                }
            )
            store.save_vod(
                {
                    "cmid": 10,
                    "course_id": 1,
                    "duration_sec": 600,
                    "watched_sec": 300,
                    "progress_pct": 50,
                }
            )
            store.commit()
            archiver = Archiver(None, store, verbose=False)
            course = SimpleNamespace(course_id=1)
            activity = SimpleNamespace(cmid=10)

            archiver._sync_vod(course, activity, {}, "unused", False, False)
            store.commit()

            row = dict(store.query("SELECT * FROM vod WHERE cmid=10")[0])
            self.assertEqual(row["duration_sec"], 600)
            self.assertEqual(row["watched_sec"], 300)
            self.assertEqual(row["progress_pct"], 50)


class CourseEnrollmentSyncTests(unittest.TestCase):
    @staticmethod
    def course_list(*course_ids: int) -> str:
        rows = "".join(
            f"""
            <tr>
              <td>2026</td><td>2학기</td>
              <td><span class="badge badge-course">교과</span>
                <a href="/course/view.php?id={course_id}" class="coursefullname">
                  강좌{course_id} (TST{course_id}.01-00)
                </a>
              </td>
            </tr>
            """
            for course_id in course_ids
        )
        return f"""
        <a href="/login/logout.php">로그아웃</a>
        <table><tbody class="my-course-lists">{rows}</tbody></table>
        """

    def test_missing_course_is_marked_unenrolled_and_can_return(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course(
                {
                    "course_id": 2,
                    "year": "2026",
                    "semester": "2학기",
                    "name": "철회한 강좌",
                    "title": "철회한 강좌",
                }
            )
            client = MagicMock()
            client.request.side_effect = [
                self.course_list(1),
                self.course_list(1, 2),
            ]
            archiver = Archiver(client, store, verbose=False)

            archiver.sync_courses()
            withdrawn = store.query(
                "SELECT enrolled,unenrolled_at FROM course WHERE course_id=2"
            )[0]
            self.assertEqual(withdrawn["enrolled"], 0)
            self.assertIsNotNone(withdrawn["unenrolled_at"])

            archiver.sync_courses()
            returned = store.query(
                "SELECT enrolled,unenrolled_at FROM course WHERE course_id=2"
            )[0]
            self.assertEqual(returned["enrolled"], 1)
            self.assertIsNone(returned["unenrolled_at"])

    def test_unrecognized_course_page_does_not_withdraw_every_course(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({"course_id": 1, "name": "강좌"})
            client = MagicMock()
            client.request.return_value = '<a href="/login/logout.php">로그아웃</a>'

            with self.assertRaises(RuntimeError):
                Archiver(client, store, verbose=False).sync_courses()

            enrolled = store.query(
                "SELECT enrolled FROM course WHERE course_id=1"
            )[0]["enrolled"]
            self.assertEqual(enrolled, 1)

    def test_authenticated_but_empty_roster_does_not_withdraw_every_course(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({"course_id": 1, "name": "강좌"})
            store.commit()
            client = MagicMock()
            client.request.return_value = (
                '<a href="/login/logout.php">로그아웃</a>'
                '<tbody class="my-course-lists"></tbody>'
            )

            with self.assertRaises(RuntimeError):
                Archiver(client, store, verbose=False).sync_courses()

            self.assertEqual(
                store.query("SELECT enrolled FROM course WHERE course_id=1")[0]["enrolled"],
                1,
            )

    def test_roster_sync_prunes_withdrawn_course_from_cached_monitor_queue(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({"course_id": 2, "name": "철회 강좌"})
            store.commit()
            store.write_json(
                "monitor_state.json",
                {
                    "viewing_queue": [{"course_id": 2, "cmid": 20}],
                    "attendance": [{"course_id": 2, "cmid": 20}],
                    "new_videos": [{"course_id": 2, "cmid": 20}],
                },
            )
            client = MagicMock()
            client.request.return_value = self.course_list(1)

            Archiver(client, store, verbose=False).sync_courses()

            state = json.loads(
                (store.root / "monitor_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["viewing_queue"], [])
            self.assertEqual(state["attendance"], [])
            self.assertEqual(state["new_videos"], [])
            self.assertIn("roster_reconciled_at", state)


class ActivityPresenceSyncTests(unittest.TestCase):
    @staticmethod
    def course() -> Course:
        return Course(1, "2026", "2학기", "교과", "테스트 (TST1000.01-00)",
                      "테스트", "TST1000", "01")

    @staticmethod
    def page(*cmids: int) -> str:
        return "".join(
            f"""
            <li id='module-{cmid}' class='url activity modtype_url'>
              <a href='/mod/url/view.php?id={cmid}'>
                <span class='instancename'>링크 {cmid}</span>
              </a>
            </li>
            """
            for cmid in cmids
        )

    def test_removed_activity_is_hidden_and_reappearance_restores_it(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({"course_id": 1, "name": "테스트"})
            for cmid in (10, 20):
                store.save_activity({
                    "cmid": cmid, "course_id": 1, "modname": "url",
                    "title": f"링크 {cmid}", "present": 1,
                })
            store.commit()
            archiver = Archiver(MagicMock(), store, verbose=False)

            with patch.object(archiver, "_fetch_course_page", return_value=self.page(10)):
                archiver.sync_course(
                    self.course(), probe_vod=False, fetch_subtitles=False,
                    fetch_files=False, fetch_boards=False,
                )
            removed = store.query(
                "SELECT present,removed_at FROM activity WHERE cmid=20"
            )[0]
            self.assertEqual(removed["present"], 0)
            self.assertIsNotNone(removed["removed_at"])
            self.assertIsNotNone(
                store.query("SELECT detail_synced_at FROM course WHERE course_id=1")[0][0]
            )

            with patch.object(
                archiver, "_fetch_course_page", return_value=self.page(10, 20)
            ):
                archiver.sync_course(
                    self.course(), probe_vod=False, fetch_subtitles=False,
                    fetch_files=False, fetch_boards=False,
                )
            returned = store.query(
                "SELECT present,removed_at FROM activity WHERE cmid=20"
            )[0]
            self.assertEqual(returned["present"], 1)
            self.assertIsNone(returned["removed_at"])

    def test_partial_activity_parse_never_reconciles_existing_rows(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({"course_id": 1, "name": "테스트"})
            store.save_activity({
                "cmid": 99, "course_id": 1, "modname": "url",
                "title": "기존 활동", "present": 1,
            })
            store.commit()
            archiver = Archiver(MagicMock(), store, verbose=False)
            malformed = (
                "<li id='module-99' class='activity url'>"
                "<span class='instancename'>모듈 종류 없음</span></li>"
            )

            with (
                patch.object(archiver, "_fetch_course_page", return_value=malformed),
                self.assertRaises(RuntimeError),
            ):
                archiver.sync_course(
                    self.course(), probe_vod=False, fetch_subtitles=False,
                    fetch_files=False, fetch_boards=False,
                )

            self.assertEqual(
                store.query("SELECT present FROM activity WHERE cmid=99")[0]["present"],
                1,
            )


class SessionExpiryTests(unittest.TestCase):
    def test_login_redirect_does_not_overwrite_submission(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_submission(
                {
                    "cmid": 10,
                    "course_id": 1,
                    "modname": "assign",
                    "title": "과제",
                    "submitted": 1,
                    "status": "제출 완료",
                }
            )
            client = MagicMock()
            client.request.return_value = (
                '<body class="format-site path-login html_login">'
                '<form action="/login/index.php"><input name="username"></form>'
                "</body>"
            )
            course = SimpleNamespace(
                course_id=1,
                url="https://example.test/course/1",
            )
            activity = SimpleNamespace(
                cmid=10,
                modname="assign",
                title="과제",
                url="https://example.test/assign/10",
            )

            with self.assertRaises(SessionExpired):
                Archiver(client, store, verbose=False)._sync_submission(
                    course, activity, "unused", False
                )

            saved = store.query("SELECT submitted,status FROM submission WHERE cmid=10")[0]
            self.assertEqual(saved["submitted"], 1)
            self.assertEqual(saved["status"], "제출 완료")


class UbdocResourceTests(unittest.TestCase):
    def test_ubfile_wrapper_follows_viewer_before_downloading_ubdoc(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            activity_url = "https://ys.learnus.org/mod/ubfile/view.php?id=4549896"
            viewer_url = "https://ys.learnus.org/mod/ubfile/viewer.php?id=4549896"
            ubdoc_url = (
                "https://ys.learnus.org/local/ubdoc/"
                "?id=4549896&tp=m&pg=ubfile"
            )
            download_url = (
                "https://ys.learnus.org/local/ubdoc/download.php"
                "?id=4549896&tp=m&pg=ubfile&item=&fid="
            )
            wrapper = (
                '<html><a class="btn" '
                f'href="{viewer_url}">열기</a></html>'
            ).encode()
            client = MagicMock()
            client.get_bytes.side_effect = [
                (wrapper, activity_url),
                (b"<html>viewer</html>", ubdoc_url),
                (b"%PDF-1.7\noriginal", download_url),
            ]
            client.request.return_value = json.dumps({
                "state_code": "100", "file_download": "1",
                "file_realname": "2 Recursion (part 1).pdf",
                "file_url": download_url,
            })
            course = SimpleNamespace(
                course_id=297395,
                url="https://ys.learnus.org/course/view.php?id=297395",
            )
            activity = SimpleNamespace(
                cmid=4549896, title="[9/8] 2 Recursion (Part 1)",
                url=activity_url,
            )

            Archiver(client, store, verbose=False)._sync_resource(
                course, activity, "2026-2/CSE_자료구조"
            )
            store.commit()

            saved = store.file_record(activity_url, "resource")
            self.assertEqual(saved["name"], "2 Recursion (part 1).pdf")
            self.assertEqual(client.get_bytes.call_args_list[1].args[0], viewer_url)
            self.assertEqual(client.get_bytes.call_args_list[2].args[0], download_url)

    def test_ubdoc_viewer_downloads_original_file_from_worker_response(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            viewer_url = (
                "https://ys.learnus.org/local/ubdoc/"
                "?id=4549896&tp=m&pg=ubfile"
            )
            download_url = (
                "https://ys.learnus.org/local/ubdoc/download.php"
                "?id=4549896&tp=m&pg=ubfile&item=&fid="
            )
            client = MagicMock()
            client.get_bytes.side_effect = [
                (b"<html>ubdoc viewer</html>", viewer_url),
                (b"%PDF-1.7\noriginal", download_url),
            ]
            client.request.return_value = json.dumps(
                {
                    "state_code": "100",
                    "file_download": "1",
                    "file_realname": "2 Recursion (part 1).pdf",
                    "file_url": download_url,
                }
            )
            course = SimpleNamespace(
                course_id=297395,
                url="https://ys.learnus.org/course/view.php?id=297395",
            )
            activity = SimpleNamespace(
                cmid=4549896,
                title="[9/8] 2 Recursion (Part 1)",
                url="https://ys.learnus.org/mod/ubfile/view.php?id=4549896",
            )

            Archiver(client, store, verbose=False)._sync_resource(
                course, activity, "2026-2/CSE_자료구조"
            )
            store.commit()

            saved = store.file_record(activity.url, "resource")
            self.assertIsNotNone(saved)
            self.assertEqual(saved["name"], "2 Recursion (part 1).pdf")
            self.assertEqual(
                store.blob_path(saved["sha256"]).read_bytes(),
                b"%PDF-1.7\noriginal",
            )
            client.request.assert_called_once_with(
                "https://ys.learnus.org/local/ubdoc/worker.php",
                data={
                    "job": "checkState",
                    "id": "4549896",
                    "tp": "m",
                    "pg": "ubfile",
                    "item": "",
                    "fid": "",
                },
                referer=viewer_url,
            )
            self.assertEqual(client.get_bytes.call_args_list[1].args[0], download_url)
            self.assertEqual(
                client.get_bytes.call_args_list[1].kwargs["referer"], viewer_url
            )

    def test_ubdoc_does_not_bypass_disabled_download(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            viewer_url = (
                "https://ys.learnus.org/local/ubdoc/"
                "?id=10&tp=m&pg=ubfile"
            )
            client = MagicMock()
            client.get_bytes.return_value = (b"<html>ubdoc viewer</html>", viewer_url)
            client.request.return_value = json.dumps(
                {"state_code": "100", "file_download": "0"}
            )
            course = SimpleNamespace(course_id=1, url="https://example.test/course/1")
            activity = SimpleNamespace(
                cmid=10,
                title="다운로드 제한 문서",
                url="https://ys.learnus.org/mod/ubfile/view.php?id=10",
            )

            Archiver(client, store, verbose=False)._sync_resource(
                course, activity, "2026-2/TST"
            )

            self.assertIsNone(store.file_record(activity.url, "resource"))
            self.assertEqual(client.get_bytes.call_count, 1)


class BoardRetryTests(unittest.TestCase):
    def test_failed_new_article_is_retried_instead_of_cached_empty(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            course = SimpleNamespace(
                course_id=1,
                url="https://example.test/course/1",
                title="테스트 과목",
            )
            activity = SimpleNamespace(
                cmid=10,
                url="https://example.test/board/10?id=10",
                title="공지",
            )
            listed = Post(
                post_id="42",
                subject="중요 공지",
                url="https://example.test/post/42",
            )
            first_client = MagicMock()
            first_client.request.side_effect = ["listing", TimeoutError("temporary")]
            with patch(
                "yonstudy.archive.P.parse_ubboard_list", return_value=([listed], 1)
            ):
                Archiver(first_client, store, verbose=False)._sync_board(
                    course, activity, "unused", 1, False
                )

            self.assertFalse(store.has_post(10, "ubboard", "42"))

            second_client = MagicMock()
            second_client.request.side_effect = ["listing", "article"]
            complete = Post(
                post_id="42",
                subject="중요 공지",
                url="https://example.test/post/42",
                body="공지 본문",
            )
            with (
                patch(
                    "yonstudy.archive.P.parse_ubboard_list", return_value=([listed], 1)
                ),
                patch(
                    "yonstudy.archive.P.parse_ubboard_article", return_value=complete
                ),
            ):
                Archiver(second_client, store, verbose=False)._sync_board(
                    course, activity, "unused", 1, False
                )

            self.assertEqual(store.post_record(10, "ubboard", "42")["body"], "공지 본문")

    def test_refresh_preserves_original_discovery_time(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            original = {
                "course_id": 1,
                "cmid": 10,
                "modname": "ubboard",
                "post_id": "42",
                "subject": "공지",
                "body": "처음 본문",
                "fetched_at": "2026-01-01T00:00:00",
                "checked_at": "2026-01-01T00:00:00",
            }
            store.save_post(original)
            refreshed = {**original, "body": "수정 본문"}
            refreshed.pop("checked_at")
            store.save_post(refreshed)
            store.commit()

            row = store.post_record(10, "ubboard", "42")
            self.assertEqual(row["fetched_at"], "2026-01-01T00:00:00")
            self.assertNotEqual(row["checked_at"], "2026-01-01T00:00:00")
            self.assertEqual(row["body"], "수정 본문")

    def test_old_forum_thread_is_reopened_for_new_replies(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_post(
                {
                    "course_id": 1,
                    "cmid": 10,
                    "modname": "forum",
                    "post_id": "t7",
                    "thread_id": "7",
                    "subject": "토론",
                    "fetched_at": "2026-01-01T00:00:00",
                    "checked_at": "2026-01-01T00:00:00",
                }
            )
            store.commit()
            client = MagicMock()
            client.request.side_effect = ["forum listing", "discussion"]
            course = SimpleNamespace(
                course_id=1,
                url="https://example.test/course/1",
                title="테스트 과목",
            )
            activity = SimpleNamespace(
                cmid=10,
                url="https://example.test/forum/10",
                title="토론방",
            )
            reply = Post(post_id="8", subject="답글", body="새 답글")

            with (
                patch(
                    "yonstudy.archive.P.parse_forum_discussions",
                    return_value=[("7", "토론")],
                ),
                patch(
                    "yonstudy.archive.P.parse_forum_discussion",
                    return_value=[reply],
                ),
            ):
                Archiver(client, store, verbose=False)._sync_forum(
                    course, activity, "unused", False
                )

            self.assertEqual(store.post_record(10, "forum", "8")["body"], "새 답글")
            self.assertEqual(client.request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
