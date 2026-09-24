import html
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

import cli
from yonstudy.export import (
    export_onedrive_tree, render_assignment_html, render_assignments_index, term_folder,
)
from yonstudy.store import Store
from yonstudy.html_content import find_elements


class OneDriveExportTests(unittest.TestCase):
    def test_term_folder_codes(self):
        self.assertEqual(term_folder("2026", "1학기"), "2026-1")
        self.assertEqual(term_folder("2026", "2학기"), "2026-2")
        self.assertEqual(term_folder("2026", "여름계절수업"), "2026-S")
        self.assertEqual(term_folder("2026", "겨울계절수업"), "2026-W")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.save_course(
            {
                "course_id": 1,
                "year": "2026",
                "semester": "2학기",
                "name": "테스트과목",
                "title": "테스트과목 (TST1000)",
                "slug": "TST1000_테스트과목",
            }
        )
        self.store.save_activity(
            {
                "cmid": 10,
                "course_id": 1,
                "modname": "ubboard",
                "title": "Q&A 게시판",
                "url": "https://example.test/board/10",
                "restricted": 0,
            }
        )
        self.store.save_activity(
            {
                "cmid": 11,
                "course_id": 1,
                "modname": "ubfile",
                "title": "Lecture 1-2",
                "section_idx": 1,
                "section_name": "1주차",
                "url": "https://example.test/resource/11",
                "restricted": 0,
            }
        )

    def tearDown(self):
        self.tmp.cleanup()
        self.out.cleanup()

    def test_incremental_material_and_post_export(self):
        digest, size = self.store.put_blob(b"lecture material")
        self.store.save_file(
            {
                "course_id": 1,
                "cmid": 11,
                "role": "resource",
                "name": "강의안.pdf",
                "url": "https://example.test/file/1",
                "sha256": digest,
                "bytes": size,
            }
        )
        self.store.save_post(
            {
                "course_id": 1,
                "cmid": 10,
                "modname": "ubboard",
                "post_id": "42",
                "subject": "질문입니다",
                "writer": "학생",
                "written_at": "2026-09-01 12:00",
                "url": "https://example.test/post/42",
                "body": "질문 본문",
            }
        )
        self.store.commit()

        first = export_onedrive_tree(
            self.store, self.out.name, year="2026", semester="2학기"
        )
        self.assertEqual(first.material_files, 1)
        self.assertEqual(first.posts, 1)
        self.assertEqual(first.copied_files, 3)
        course_dir = Path(self.out.name) / "2026-2" / "TST1000_테스트과목"
        for category in ("게시판_첨부", "QNA_공지"):
            self.assertTrue((course_dir / category).is_dir())
        self.assertTrue((course_dir / "강좌정보.md").is_file())
        self.assertFalse((course_dir / "강의자료").exists())
        files = [p for p in Path(self.out.name).rglob("*") if p.is_file()]
        material = next(p for p in files if p.name.endswith("강의안__f1.pdf"))
        self.assertEqual(material.parent, course_dir)
        self.assertTrue(material.name.startswith("W01-L02__강의자료__"))
        post = next(p for p in files if p.suffix == ".md" and "QNA_공지" in p.parts)
        self.assertIn("질문 본문", post.read_text(encoding="utf-8"))

        second = export_onedrive_tree(
            self.store, self.out.name, year="2026", semester="2학기"
        )
        self.assertEqual(second.copied_files, 0)

    def test_cli_can_export_every_archived_term(self):
        self.store.save_course(
            {
                "course_id": 2,
                "year": "2025",
                "semester": "2학기",
                "name": "이전과목",
                "title": "이전과목",
                "slug": "OLD1000_이전과목",
            }
        )
        self.store.commit()
        args = SimpleNamespace(
            store=self.tmp.name, destination=self.out.name,
            year=None, semester=None, all_terms=True, dry_run=False,
        )

        with patch("builtins.print"):
            code = cli.cmd_export(args)

        self.assertEqual(code, 0)
        self.assertTrue((Path(self.out.name) / "2025-2" / "OLD1000_이전과목").is_dir())
        self.assertTrue((Path(self.out.name) / "2026-2" / "TST1000_테스트과목").is_dir())

    def test_assignment_reading_indexes_link_to_existing_html_and_keep_personal_status(self):
        self.store.save_activity({
            "cmid": 12, "course_id": 1, "modname": "assign",
            "title": "Team #1", "section_idx": 2, "section_name": "2주차",
            "url": "https://example.test/assignment/12",
        })
        self.store.save_submission({
            "cmid": 12, "course_id": 1, "modname": "assign", "title": "Team #1",
            "status": "미제출", "submitted": 0, "instructions": "## Instructions\n\n- Read the notebook.",
        })
        self.store.db.execute(
            "INSERT INTO assignment_preference(cmid,requirement,reason,updated_at) VALUES (?,?,?,?)",
            (12, "not_required", "팀장이 대표 제출", "2026-09-22T12:00:00"),
        )
        self.store.commit()
        result = export_onedrive_tree(self.store, self.out.name, year="2026", semester="2학기")
        self.assertEqual(result.assignment_specs, 1)
        self.assertEqual(result.assignment_indexes, 2)
        root = Path(self.out.name)
        term_index = root / "2026-2" / "과제목록.html"
        index_html = term_index.read_text()
        self.assertIn("본인 제출 불필요", index_html)
        self.assertIn("LearnUs 상태: 미제출", index_html)
        links = re.findall('href="([^"]+)"', index_html)
        self.assertTrue(links[0].endswith(".html"))
        for link in links:
            self.assertTrue((term_index.parent / unquote(html.unescape(link))).is_file(), link)
        doc = (term_index.parent / unquote(html.unescape(links[0]))).read_text()
        self.assertIn("Instructions", [node.text() for node in find_elements(doc, lambda tag, attrs: tag == "h2")])
        self.assertIn("팀장이 대표 제출", doc)
        self.assertIn('href="../index.html"', doc)
        course_index = next(root.rglob("강좌정보.md")).read_text()
        self.assertIn("본인 제출 불필요", course_index)
        self.assertIn("LearnUs: 미제출", course_index)
        second = export_onedrive_tree(self.store, self.out.name, year="2026", semester="2학기")
        self.assertEqual(second.copied_files, 0)

    def test_cached_notebook_html_gets_readable_structure_without_resync(self):
        source = "## What you have to do\n\n- Complete **TODO**.\n- Submit the notebook."
        rendered = render_assignment_html({"title": "AI"}, {
            "title": "Assignment 2", "instructions_html": '<pre class="notebook-markdown">' + html.escape(source) + '</pre>',
        }).decode()
        self.assertIn("What you have to do", [node.text() for node in find_elements(rendered, lambda tag, attrs: tag == "h2")])
        self.assertIn("<strong>TODO</strong>", rendered)
        self.assertIn('<meta name="viewport"', rendered)
        self.assertIn("@media print", rendered)
        self.assertNotIn('<pre class="notebook-markdown">', rendered)

    def test_reading_index_escapes_titles_and_filename_delimiters(self):
        rendered = render_assignments_index([{
            "title": '<img src=x onerror="bad">', "html_path": "Team #1/task?.html",
            "markdown_path": "Team #1/task?.md",
        }]).decode()
        self.assertIn('href="Team%20%231/task%3F.html"', rendered)
        self.assertNotIn("<img", rendered)
        self.assertIn("&lt;img", rendered)

    def test_legacy_oj_description_is_rendered_as_problems_with_navigation(self):
        source = ('## Yonsei-OJ 상세 명세\n\n### OJ-1. Factorial\n\n'
                  '#### Problem\n\nCompute `n!`.\n\n#### Example\n\n'
                  '```\n5\n120\n```\n\n#### Skeleton code\n\n'
                  '```python\ndef factorial(n):\n    pass\n```')
        rendered = render_assignment_html({"title": "자료구조"}, {
            "title": "Recursion", "instructions_html": '<h2>Yonsei-OJ 상세 명세</h2><pre>' + html.escape(source) + '</pre>',
        }).decode()
        headings = find_elements(rendered, lambda tag, attrs: tag in {"h2", "h3", "h4"})
        self.assertEqual([node.text() for node in headings], ["Yonsei-OJ 상세 명세", "OJ-1. Factorial", "Problem", "Example", "Skeleton code"])
        code = find_elements(rendered, lambda tag, attrs: tag == "pre")
        self.assertEqual([node.text() for node in code], ["5\n120", "def factorial(n):\n    pass"])
        targets = {node.attrs["id"] for node in find_elements(rendered, lambda tag, attrs: bool(attrs.get("id")))}
        links = find_elements(rendered, lambda tag, attrs: tag == "a" and (attrs.get("href") or "").startswith("#"))
        self.assertEqual(len(links), 2)
        self.assertTrue(all(link.attrs["href"][1:] in targets for link in links))

    def test_regular_code_is_not_interpreted_as_legacy_oj_document(self):
        sample = '## Just a comment\nprint("hello")\n    keep_spacing = 1'
        rendered = render_assignment_html({"title": "자료구조"}, {
            "title": "Arrays", "instructions_html": '<pre>' + html.escape(sample) + '</pre>',
        }).decode()
        self.assertEqual(find_elements(rendered, lambda tag, attrs: tag == "pre")[0].text(), sample)


if __name__ == "__main__":
    unittest.main()
