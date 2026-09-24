import html
import json
import unittest

from yonstudy.notebook import MAX_NOTEBOOK_BYTES, parse_notebook_spec


URL = "https://ys.learnus.org/pluginfile.php/123/assignment.ipynb?forcedownload=1"


def notebook(*cells, **extra):
    return json.dumps({"nbformat": 4, "nbformat_minor": 0, "cells": cells, **extra}).encode()


class NotebookSpecTests(unittest.TestCase):
    def test_teacher_guidelines_and_starter_code_keep_cell_order(self):
        # The live Assignment 2 notebook alternates markdown instructions and
        # starter code; the activity page only says to consult the notebook.
        instructions = "# Assignment 2\n\n- Complete only `TODO`.\n- Keep output cells."
        code = "def predict(x):\n    # TODO\n    return x\n\n"
        body = notebook(
            {"cell_type": "markdown", "source": instructions},
            {"cell_type": "code", "source": code.splitlines(keepends=True)},
            {"cell_type": "markdown", "source": ["## Evaluation\n", "Submit your notebook."]},
        )
        markdown, rendered = parse_notebook_spec(body, "assignment2_2026.ipynb", URL)
        self.assertIn(instructions, markdown)
        self.assertIn(code, markdown)
        self.assertLess(markdown.index("Complete only"), markdown.index("def predict"))
        self.assertLess(markdown.index("def predict"), markdown.index("## Evaluation"))
        self.assertIn("assignment2_2026.ipynb", rendered)
        self.assertIn(URL, markdown)
        self.assertIn(html.escape(code), rendered)
        self.assertIn("<h1>Assignment 2</h1>", rendered)
        self.assertIn("<h2>Evaluation</h2>", rendered)
        self.assertIn("<ul>", rendered)
        self.assertIn("<code>TODO</code>", rendered)
        self.assertNotIn('<pre class="notebook-markdown">', rendered)

    def test_notebook_instructions_render_nested_requirements_links_and_tables(self):
        source = """## What you have to do
- Implement all `TODO` parts.
- **IMPORTANT**
  - Keep the output cells.
  - Submit the notebook as executed.

3. Download the notebook.
4. Submit on [LearnUs](https://ys.learnus.org/).

| Item | Points |
| --- | --- |
| Regression | 10 |

The coefficient is $w_i$.

```python
if value < 2:
    return value
```
"""
        markdown, rendered = parse_notebook_spec(notebook(
            {"cell_type": "markdown", "source": source},
        ), "assignment2.ipynb", URL)
        self.assertIn(source, markdown)
        self.assertIn("<strong>IMPORTANT</strong></p>\n<ul>", rendered)
        self.assertEqual(rendered.count("<ul>"), 2)
        self.assertIn('<ol start="3">', rendered)
        self.assertIn('<a href="https://ys.learnus.org/">LearnUs</a>', rendered)
        self.assertIn("<th>Points</th>", rendered)
        self.assertIn("<td>Regression</td>", rendered)
        self.assertIn('$w_i$', rendered)
        self.assertIn("if value &lt; 2:\n    return value", rendered)

    def test_markdown_links_cannot_execute_code_or_add_html_attributes(self):
        source = '[run](javascript:alert(1)) ![bad](data:text/html,evil) [docs](https://example.test/a?x=1&y=2)'
        _, rendered = parse_notebook_spec(notebook(
            {"cell_type": "markdown", "source": source},
        ), "assignment.ipynb", URL)
        self.assertNotIn('href="javascript:', rendered)
        self.assertNotIn('src="data:', rendered)
        self.assertIn('href="https://example.test/a?x=1&amp;y=2"', rendered)

    def test_outputs_metadata_and_execution_count_never_enter_spec(self):
        body = notebook(
            {
                "cell_type": "code", "source": "print('starter')", "execution_count": 123456789,
                "metadata": {"private": "CELL_METADATA_SECRET"},
                "outputs": [{"text": ["OUTPUT_SECRET"], "data": {"text/html": "<script>OUTPUT_SECRET</script>"}}],
            },
            {"cell_type": "markdown", "source": "Instructions", "attachments": {"PRIVATE_ATTACHMENT": {}}},
            metadata={"private": "NOTEBOOK_METADATA_SECRET"},
        )
        markdown, rendered = parse_notebook_spec(body, "starter.ipynb", URL)
        for secret in ("OUTPUT_SECRET", "CELL_METADATA_SECRET", "NOTEBOOK_METADATA_SECRET", "PRIVATE_ATTACHMENT", "123456789"):
            self.assertNotIn(secret, markdown + rendered)

    def test_html_is_displayed_safely_and_code_fences_cannot_close_early(self):
        source = '<script>alert("x")</script>\n<img src=x onerror=alert(1)>'
        code = 'print("```")\n    indented = True'
        markdown, rendered = parse_notebook_spec(notebook(
            {"cell_type": "markdown", "source": source},
            {"cell_type": "code", "source": code},
        ), "starter<unsafe>.ipynb", URL + "&a=b")
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img ", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&amp;a=b", rendered)
        self.assertIn('````\nprint("```")\n    indented = True\n````', markdown)

    def test_raw_cells_are_retained_as_text_and_empty_cells_are_skipped(self):
        markdown, rendered = parse_notebook_spec(notebook(
            {"cell_type": "markdown", "source": []},
            {"cell_type": "raw", "source": ["Raw instructions\n", "  spacing"]},
        ), "starter.ipynb", URL)
        self.assertIn("```text\nRaw instructions\n  spacing\n```", markdown)
        self.assertIn("Raw instructions\n  spacing", rendered)

    def test_invalid_documents_report_useful_errors(self):
        cases = [
            (b"<html>login page</html>", "JSON"),
            (b"\xff", "JSON"),
            (b"[]", "객체"),
            (notebook(nbformat=3), "nbformat 4"),
            (notebook(nbformat_minor=-1), "nbformat_minor"),
            (json.dumps({"nbformat": 4, "cells": {}}).encode(), "cells"),
            (notebook("bad cell"), "1번 셀"),
            (notebook({"cell_type": "code", "source": ["fine", 7]}), "source"),
            (notebook({"cell_type": "unknown", "source": "content"}), "형식"),
            (notebook({"cell_type": "markdown", "source": " "}), "추출할"),
            (b"x" * (MAX_NOTEBOOK_BYTES + 1), "10 MiB"),
        ]
        for body, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                parse_notebook_spec(body, "starter.ipynb", URL)

    def test_unsupported_file_and_unsafe_provenance_are_rejected(self):
        body = notebook({"cell_type": "markdown", "source": "Instructions"})
        with self.assertRaisesRegex(ValueError, "ipynb"):
            parse_notebook_spec(body, "file.zip", URL)
        for url in ("javascript:alert(1)", "https://example.org/\nfile.ipynb", "https://["):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "URL"):
                parse_notebook_spec(body, "starter.ipynb", url)


if __name__ == "__main__":
    unittest.main()
