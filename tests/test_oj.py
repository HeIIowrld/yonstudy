import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yonstudy.lti import LtiAssignment
from yonstudy.export import render_assignment_markdown
from yonstudy.oj import (
    OjContest,
    OjParticipationRequired,
    OjProblem,
    YonseiOjClient,
    enrich_with_yonsei_oj,
    parse_problem,
)


PROBLEM_PAGE = """
<div class="problem-title"><h2>1. Recursion - Factorial</h2></div>
<div class="problem-info-entry"><span class="pi-name">Points:</span>
<span class="pi-value">5 (partial)</span></div>
<div class="problem-info-entry"><span class="pi-name">Time limit:</span>
<span class="pi-value">0.2s</span></div>
<div class="problem-info-entry"><span class="pi-name">Memory limit:</span>
<span class="pi-value">13M</span></div>
<div id="allowed-langs"><div class="toggled">Python</div></div>
<div class="content-description screen"><div>
<h5>Problem</h5><p>Calculate <code>N!</code> recursively.</p>
<h5>Input</h5><p><code>0 ≤ N ≤ 20</code></p>
<h5>Skeleton code</h5><pre><code>def factorial(n):
    pass
</code></pre></div><hr></div>
"""


class ProblemParserTests(unittest.TestCase):
    def test_problem_metadata_statement_and_skeleton_are_normalized(self):
        problem = parse_problem(
            PROBLEM_PAGE,
            "https://yonsei-oj.duckdns.org:508/problem/ds1of1",
        )

        self.assertEqual(problem.code, "ds1of1")
        self.assertEqual(problem.title, "1. Recursion - Factorial")
        self.assertEqual(problem.points, "5 (partial)")
        self.assertEqual(problem.time_limit, "0.2s")
        self.assertEqual(problem.memory_limit, "13M")
        self.assertEqual(problem.languages, ["Python"])
        self.assertIn("#### Problem", problem.statement)
        self.assertIn("`N!`", problem.statement)
        self.assertIn("```python\ndef factorial(n):", problem.statement)


class ContestClientTests(unittest.TestCase):
    def test_contest_is_matched_to_gradescope_title_and_problems_are_fetched(self):
        listing = '<a href="/contest/dscontest1">1. Recursion</a>'
        contest = '<a href="/problem/ds1of1">1</a>'

        with tempfile.TemporaryDirectory() as root:
            client = YonseiOjClient(Path(root) / "cookies.txt")
            pages = {
                "/contests/": listing,
                "/contest/dscontest1": contest,
                "/problem/ds1of1": PROBLEM_PAGE,
            }
            with patch.object(client, "authenticated_page", side_effect=lambda path: pages[path]):
                result = client.fetch_contest("1. Recursion Coding Assignment")

        self.assertEqual(result.slug, "dscontest1")
        self.assertEqual(result.title, "1. Recursion")
        self.assertEqual([problem.code for problem in result.problems], ["ds1of1"])

    def test_join_is_never_posted_without_explicit_auto_join(self):
        listing = '<a href="/contest/dscontest1">1. Recursion</a>'
        contest = (
            '<form action="/contest/dscontest1/join" method="post">'
            '<input name="csrfmiddlewaretoken" value="token"></form>'
        )
        with tempfile.TemporaryDirectory() as root:
            client = YonseiOjClient(Path(root) / "cookies.txt")
            pages = {"/contests/": listing, "/contest/dscontest1": contest}
            with patch.object(client, "authenticated_page", side_effect=lambda path: pages[path]), \
                    patch.object(client, "_request") as request:
                with self.assertRaises(OjParticipationRequired):
                    client.fetch_contest("1. Recursion Coding Assignment")
                request.assert_not_called()

    def test_joined_contest_recovers_problem_codes_from_ranking(self):
        listing = '<a href="/contest/dscontest1">1. Recursion</a>'
        contest = '<form action="/contest/dscontest1/leave" method="post"></form>'
        ranking = '<a href="/contest/dscontest1/rank/ds1of1/">1</a>'
        with tempfile.TemporaryDirectory() as root:
            client = YonseiOjClient(Path(root) / "cookies.txt")
            pages = {
                "/contests/": listing,
                "/contest/dscontest1": contest,
                "/contest/dscontest1/ranking/": ranking,
                "/problem/ds1of1": PROBLEM_PAGE,
            }
            with patch.object(client, "authenticated_page", side_effect=lambda path: pages[path]):
                result = client.fetch_contest("1. Recursion Coding Assignment")

        self.assertEqual([problem.code for problem in result.problems], ["ds1of1"])


class AssignmentEnrichmentTests(unittest.TestCase):
    def test_oj_problem_specs_are_appended_for_draft_generation(self):
        assignment = LtiAssignment(
            provider="Gradescope",
            title="1. Recursion Coding Assignment",
            source_url="https://gradescope.com/assignment/1",
            instructions="Visit https://yonsei-oj.duckdns.org:508/",
            instructions_html="<p>Visit Yonsei-OJ.</p>",
            question_count=3,
            total_points="85",
        )
        contest = OjContest(
            slug="dscontest1",
            title="1. Recursion",
            url="https://yonsei-oj.duckdns.org:508/contest/dscontest1",
            problems=[
                OjProblem(
                    code="ds1of1",
                    title="Factorial",
                    url="https://yonsei-oj.duckdns.org:508/problem/ds1of1",
                    points="5",
                    time_limit="0.2s",
                    memory_limit="13M",
                    languages=["Python"],
                    statement="#### Problem\n\nCalculate factorial.",
                )
            ],
        )
        with patch("yonstudy.oj.YonseiOjClient.fetch_contest", return_value=contest):
            enriched, actual = enrich_with_yonsei_oj(
                assignment, assignment.title, "/tmp/unused-oj-cookie"
            )

        self.assertIs(actual, contest)
        self.assertIn("## Yonsei-OJ 상세 명세", enriched.instructions)
        self.assertIn("`ds1of1`", enriched.instructions)
        self.assertIn("허용 언어: Python", enriched.instructions)
        self.assertIn("Yonsei-OJ 상세 명세", enriched.instructions_html)

        rendered = render_assignment_markdown(
            {"title": "자료구조"},
            {
                "title": assignment.title,
                "modname": "lti",
                "fields_json": (
                    '{"External provider":"Yonsei-OJ",'
                    '"External contest":"1. Recursion",'
                    '"External problem count":"1"}'
                ),
                "instructions": enriched.instructions,
            },
        ).decode("utf-8")
        self.assertIn("- 외부 제공자: Yonsei-OJ", rendered)
        self.assertIn("- 외부 문제 수: 1개", rendered)


if __name__ == "__main__":
    unittest.main()
