import base64
import io
import json
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from email.message import Message
from unittest.mock import Mock, patch

from yonstudy.leetcode import (
    LeetCodeArchiveError,
    LeetCodeClient,
    LeetCodeProblem,
    _AssetRedirectHandler,
    enrich_with_leetcode,
    problem_slugs,
)
from yonstudy.lti import LtiAssignment


def assignment(text="https://leetcode.com/problems/valid-parentheses/"):
    return LtiAssignment(
        provider="Gradescope", title="Stacks", source_url="https://www.gradescope.com/",
        instructions=text + "\n\n- 제출 항목: 파일 업로드",
        instructions_html="<p>원래 과제 안내</p>", question_count=1, total_points="5",
    )


def response(slug="valid-parentheses", content=None):
    return io.BytesIO(json.dumps({"data": {"question": {
        "title": "Valid Parentheses", "titleSlug": slug, "difficulty": "Easy",
        "content": content if content is not None else (
            '<p>Check a string containing brackets.</p>'
            '<p><strong>Example 1:</strong></p><pre>Input: s = "()"\nOutput: true</pre>'
            '<p><strong>Constraints:</strong></p><ul><li>1 &lt;= s.length &lt;= 10000</li></ul>'
            '<img src="/images/brackets.png" alt="Brackets">'
        ),
        "codeSnippets": [
            {"lang": "C++", "langSlug": "cpp", "code": "class Solution {};"},
            {"lang": "Python3", "langSlug": "python3", "code": "class Solution:\n    def isValid(self, s: str) -> bool:\n        pass"},
        ],
        "solution": "private editorial must not appear",
        "submissions": [{"code": "private submitted solution"}],
    }}}).encode())


ASSET_URL = "https://assets.leetcode.com/uploads/example.png"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jN1kAAAAASUVORK5CYII=")


def image_response(content=PNG, mime="image/png", url=ASSET_URL):
    result = io.BytesIO(content)
    result.headers = Message()
    result.headers["Content-Type"] = mime
    result.geturl = lambda: url
    return result


class LeetCodeClientTests(unittest.TestCase):
    def test_public_statement_examples_constraints_and_python_stub(self):
        client = LeetCodeClient()
        client.opener = Mock()
        client.opener.open.return_value = response()

        problem = client.fetch_problem("valid-parentheses")

        self.assertIn('Input: s = "()"', problem.statement)
        self.assertIn("1 <= s.length <= 10000", problem.statement)
        self.assertIn("![Brackets](https://leetcode.com/images/brackets.png)", problem.statement)
        self.assertIn("def isValid", problem.starter_code)
        self.assertNotIn("private", repr(problem))
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://leetcode.com/graphql/")
        self.assertNotIn("Cookie", request.headers)
        query = json.loads(request.data)["query"]
        self.assertNotIn("solution", query.lower())
        self.assertNotIn("submission", query.lower())

    def test_explicit_language_selects_that_public_stub(self):
        client = LeetCodeClient()
        client.opener = Mock()
        client.opener.open.return_value = response()
        self.assertEqual(client.fetch_problem("valid-parentheses", language="cpp").starter_code, "class Solution {};")

    def test_temporary_error_retries_once(self):
        client = LeetCodeClient()
        client.opener = Mock()
        client.opener.open.side_effect = [urllib.error.URLError("offline"), response()]
        self.assertEqual(client.fetch_problem("valid-parentheses").title, "Valid Parentheses")
        self.assertEqual(client.opener.open.call_count, 2)

    def test_access_denied_and_missing_public_content_are_reported(self):
        client = LeetCodeClient()
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError("https://leetcode.com/graphql/", 403, "Forbidden", {}, None)
        with self.assertRaisesRegex(LeetCodeArchiveError, "403"):
            client.fetch_problem("valid-parentheses")
        self.assertEqual(client.opener.open.call_count, 1)
        client.opener.open.side_effect = None
        client.opener.open.return_value = response(content=" ")
        with self.assertRaisesRegex(LeetCodeArchiveError, "본문"):
            client.fetch_problem("valid-parentheses")

    def test_wrong_problem_response_is_rejected(self):
        client = LeetCodeClient()
        client.opener = Mock()
        client.opener.open.return_value = response(slug="other-problem")
        with self.assertRaises(LeetCodeArchiveError):
            client.fetch_problem("valid-parentheses")

    def test_public_example_image_is_embedded_only_in_html_without_cookies(self):
        client = LeetCodeClient()
        client.opener = Mock()
        client.opener.open.return_value = response(content=f'<p>Example</p><img src="{ASSET_URL}" alt="Example">')
        client.image_opener = Mock()
        client.image_opener.open.return_value = image_response()

        problem = client.fetch_problem("valid-parentheses")

        self.assertIn(f"![Example]({ASSET_URL})", problem.statement)
        self.assertIn('src="data:image/png;base64,' + base64.b64encode(PNG).decode() + '"', problem.statement_html)
        request = client.image_opener.open.call_args.args[0]
        self.assertEqual(request.full_url, ASSET_URL)
        self.assertNotIn("Cookie", request.headers)
        self.assertTrue(request.get_header("User-agent").startswith("Mozilla/5.0"))

    def test_untrusted_and_non_https_images_are_not_downloaded(self):
        client = LeetCodeClient()
        client.image_opener = Mock()
        urls = [
            "https://leetcode.com/images/example.png", "http://assets.leetcode.com/example.png",
            "https://assets.leetcode.com.evil.test/example.png", "https://user@assets.leetcode.com/example.png",
            "https://assets.leetcode.com:8443/example.png",
        ]
        source = "".join(f'<img src="{url}">' for url in urls)
        self.assertEqual(client._embed_public_images(source), source)
        client.image_opener.open.assert_not_called()

    def test_image_failure_bad_type_signature_size_or_redirect_keeps_original(self):
        source = f'<img src="{ASSET_URL}" alt="Example">'
        for failure in [
            urllib.error.URLError("offline"), image_response(mime="text/html"),
            image_response(content=b"<html>not an image</html>"),
            image_response(content=PNG + b"x" * (2 * 1024 * 1024)),
            image_response(url="https://another.test/image.png"),
        ]:
            with self.subTest(failure=type(failure).__name__):
                client = LeetCodeClient()
                client.image_opener = Mock()
                if isinstance(failure, Exception):
                    client.image_opener.open.side_effect = failure
                else:
                    client.image_opener.open.return_value = failure
                self.assertEqual(client._embed_public_images(source), source)

    def test_image_duplicates_count_and_total_bytes_are_bounded(self):
        client = LeetCodeClient()
        client.image_opener = Mock()
        source = ''.join(f'<img src="https://assets.leetcode.com/{index}.png">' for index in [0, 0, 1, 2])
        client.image_opener.open.side_effect = lambda request, **kw: image_response(url=request.full_url)
        with patch("yonstudy.leetcode._MAX_IMAGES", 2):
            result = client._embed_public_images(source)
        self.assertEqual(client.image_opener.open.call_count, 2)
        self.assertEqual(result.count("data:image/png;base64,"), 3)
        self.assertIn('src="https://assets.leetcode.com/2.png"', result)

        client.image_opener.open.reset_mock()
        with patch("yonstudy.leetcode._MAX_IMAGE_TOTAL_BYTES", len(PNG)):
            result = client._embed_public_images(source)
        self.assertEqual(client.image_opener.open.call_count, 1)
        self.assertEqual(result.count("data:image/png;base64,"), 2)

    def test_image_redirect_to_other_host_is_rejected_before_following(self):
        handler = _AssetRedirectHandler()
        request = urllib.request.Request(ASSET_URL)
        for url in ["https://example.test/image.png", "http://assets.leetcode.com/image.png"]:
            with self.subTest(url=url), self.assertRaises(urllib.error.URLError):
                handler.redirect_request(request, None, 302, "Found", {}, url)
        redirected = handler.redirect_request(request, None, 302, "Found", {}, ASSET_URL + "?version=2")
        self.assertEqual(redirected.full_url, ASSET_URL + "?version=2")


class LeetCodeEnrichmentTests(unittest.TestCase):
    def test_only_explicit_problem_links_are_followed_once(self):
        original = assignment(
            "https://leetcode.com/problems/valid-parentheses/description/\n"
            "[same](https://leetcode.com/problems/valid-parentheses/?env=abc)\n"
            "https://leetcode.com.evil.test/problems/do-not-fetch/\n"
            "https://example.test/problems/not-leetcode/\n"
            "https://leetcode.com/discuss/123"
        )
        self.assertEqual(problem_slugs(original), ["valid-parentheses"])

    def test_append_spec_preserves_original_and_is_idempotent(self):
        original = assignment()
        client = Mock()
        client.fetch_problem.return_value = LeetCodeProblem(
            slug="valid-parentheses", title="Valid Parentheses", url="https://leetcode.com/problems/valid-parentheses/",
            difficulty="Easy", statement="Check brackets.\n\n- Constraint: n <= 10000",
            statement_html="<p>Check brackets.</p><ul><li>Constraint: n &lt;= 10000</li></ul>",
            starter_code="class Solution:\n    pass",
        )
        enriched, warnings = enrich_with_leetcode(original, client=client)
        self.assertEqual(warnings, [])
        self.assertTrue(enriched.instructions.startswith(original.instructions))
        self.assertIn("Constraint: n <= 10000", enriched.instructions)
        self.assertIn("```python\nclass Solution:", enriched.instructions)
        self.assertIn("<pre><code>class Solution:", enriched.instructions_html)
        self.assertEqual(enriched.question_count, original.question_count)
        self.assertEqual(enriched.total_points, original.total_points)
        # 다른 공급자의 뒤쪽 명세도 재수집 시 유지한다.
        with_oj = replace(enriched, instructions=enriched.instructions + "\n\n## OJ\nOJ prompt")
        repeated, _ = enrich_with_leetcode(with_oj, client=client)
        self.assertEqual(repeated.instructions.count("## LeetCode 문제 명세"), 1)
        self.assertIn("OJ prompt", repeated.instructions)

    def test_one_failed_problem_does_not_drop_successful_problem_or_original(self):
        original = assignment("https://leetcode.com/problems/first/\nhttps://leetcode.com/problems/second/")
        client = Mock()
        client.fetch_problem.side_effect = [
            LeetCodeArchiveError("접근할 수 없습니다"),
            LeetCodeProblem("second", "Second", "https://leetcode.com/problems/second/", "Easy", "Second statement", "<p>Second statement</p>"),
        ]
        enriched, warnings = enrich_with_leetcode(original, client=client)
        self.assertEqual(len(warnings), 1)
        self.assertIn("first", warnings[0])
        self.assertIn("Second statement", enriched.instructions)
        self.assertIn("제출 항목: 파일 업로드", enriched.instructions)

    def test_all_failures_preserve_existing_assignment(self):
        original = assignment()
        client = Mock()
        client.fetch_problem.side_effect = LeetCodeArchiveError("offline")
        enriched, warnings = enrich_with_leetcode(original, client=client)
        self.assertIs(enriched, original)
        self.assertEqual(len(warnings), 1)

    def test_partial_failure_keeps_old_spec_only_for_current_source_links(self):
        original = assignment("https://leetcode.com/problems/first/\nhttps://leetcode.com/problems/second/\nhttps://leetcode.com/problems/removed/")
        client = Mock()
        client.fetch_problem.side_effect = [
            LeetCodeProblem(slug, slug.title(), f"https://leetcode.com/problems/{slug}/", "Easy", f"Old {slug} body", f"<p>Old {slug} body</p>")
            for slug in ("first", "second", "removed")
        ]
        previous, _ = enrich_with_leetcode(original, client=client)
        updated = replace(previous, instructions=previous.instructions.replace("https://leetcode.com/problems/removed/\n", "", 1))
        client.fetch_problem.reset_mock()
        client.fetch_problem.side_effect = [
            LeetCodeArchiveError("offline"),
            LeetCodeProblem("second", "Second", "https://leetcode.com/problems/second/", "Easy", "Fresh second body", "<p>Fresh second body</p>"),
        ]

        enriched, warnings = enrich_with_leetcode(updated, client=client)

        self.assertIn("이전 수집본 유지", warnings[0])
        self.assertIn("Old first body", enriched.instructions)
        self.assertIn("Old first body", enriched.instructions_html)
        self.assertIn("Fresh second body", enriched.instructions)
        self.assertNotIn("Old second body", enriched.instructions)
        self.assertNotIn("Old removed body", enriched.instructions)
        self.assertEqual([call.args[0] for call in client.fetch_problem.call_args_list], ["first", "second"])

    def test_removing_all_source_links_removes_cached_problem_specs(self):
        client = Mock()
        client.fetch_problem.return_value = LeetCodeProblem(
            "valid-parentheses", "Brackets", "https://leetcode.com/problems/valid-parentheses/",
            "Easy", "Old statement", "<p>Old statement</p>",
        )
        previous, _ = enrich_with_leetcode(assignment(), client=client)
        updated = replace(previous, instructions=previous.instructions.replace(
            "https://leetcode.com/problems/valid-parentheses/", "Link removed", 1,
        ))
        client.fetch_problem.reset_mock()
        enriched, warnings = enrich_with_leetcode(updated, client=client)
        client.fetch_problem.assert_not_called()
        self.assertEqual(warnings, [])
        self.assertNotIn("Old statement", enriched.instructions)
        self.assertNotIn("Old statement", enriched.instructions_html)


if __name__ == "__main__":
    unittest.main()
