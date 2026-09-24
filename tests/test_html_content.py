import unittest

from yonstudy.html_content import clean_html, find_elements, html_to_markdown


class HtmlContentTests(unittest.TestCase):
    def test_code_indentation_blank_lines_and_literal_symbols_are_preserved(self):
        code = 'def f(n):\n    if n &lt; 3:\n\n\n        return "```"\n'
        md = html_to_markdown('<pre><code class="language-python">' + code + '</code></pre>')
        self.assertEqual(md, '````python\ndef f(n):\n    if n < 3:\n\n\n        return "```"\n````')
        self.assertEqual(html_to_markdown('<p>Use <code>x&lt;y</code>.</p>'), 'Use `x<y`.')

    def test_inline_code_preserves_superscript_constraints_and_subscripts(self):
        cases = [
            ('1 &lt;= s.length &lt;= 10<sup>4</sup>', '1 <= s.length <= 10^4'),
            ('-2<sup>31</sup> &lt;= nums[i] &lt;= 2<sup>31</sup> - 1', '-2^31 <= nums[i] <= 2^31 - 1'),
            ('x<sup>n+1</sup> + 2<sup>-2</sup>', 'x^(n+1) + 2^-2'),
            ('a<sub>i</sub> + a<sub>n+1</sub>', 'a_i + a_(n+1)'),
            ('104 + 231 + 105', '104 + 231 + 105'),
            ('&lt;sup&gt;4&lt;/sup&gt;', '<sup>4</sup>'),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(html_to_markdown('<code>' + source + '</code>'), '`' + expected + '`')

    def test_preformatted_examples_keep_exponents_line_breaks_and_code_spacing(self):
        source = '<pre><code class="language-python"># Limit: 10<sup>5</sup><br>def f(n):\n\t<span>value</span> = 104  \n\n\n\treturn "&lt;sup&gt;4&lt;/sup&gt;"\n</code></pre>'
        expected = '```python\n# Limit: 10^5\ndef f(n):\n\tvalue = 104  \n\n\n\treturn "<sup>4</sup>"\n```'
        self.assertEqual(html_to_markdown(source), expected)
        self.assertEqual(html_to_markdown('<code>a<br>b</code>'), '`a b`')

    def test_lists_keep_numbering_paragraphs_and_nested_indentation(self):
        md = html_to_markdown('<ol start="3"><li><p>First</p><ul><li>Child</li></ul></li><li value="8">Last</li></ol>')
        self.assertIn('3. First', md)
        self.assertIn('   - Child', md)
        self.assertIn('8. Last', md)
        self.assertNotIn('- \n', md)

    def test_tables_images_links_and_math_survive(self):
        source = '''<h2>Examples</h2><table><tr><th>Input</th><th>Output</th></tr>
        <tr><td><code>a|b</code></td><td>1<br>2</td></tr></table>
        <a href="../download/file.pdf">Instructions</a><img src="figure.png" alt="Tree">
        <script type="math/tex">x^2</script><script>alert('secret')</script>'''
        md = html_to_markdown(source, 'https://example.test/tasks/1/', heading_offset=1)
        self.assertIn('### Examples', md)
        self.assertIn('| Input | Output |\n| --- | --- |', md)
        self.assertIn('`a\\|b`', md)
        self.assertIn('[Instructions](https://example.test/tasks/download/file.pdf)', md)
        self.assertIn('![Tree](https://example.test/tasks/1/figure.png)', md)
        self.assertIn('$x^2$', md)
        self.assertNotIn('secret', md)

    def test_complex_table_keeps_spans_and_html_removes_active_content(self):
        source = '<table><tr><td colspan="2">Merged</td></tr></table><img src="/x.png" onerror="bad()"><a href="javascript:bad()">link</a>'
        md = html_to_markdown(source, 'https://example.test')
        self.assertIn('colspan="2"', md)
        clean = clean_html(source, 'https://example.test')
        self.assertIn('src="https://example.test/x.png"', clean)
        self.assertNotIn('bad()', clean)

    def test_nested_extraction_is_not_truncated_at_horizontal_rule(self):
        source = '<div class="body"><div>Before<hr>After</div><p>Last</p></div><p>Outside</p>'
        nodes = find_elements(source, lambda tag, attrs: attrs.get('class') == 'body')
        self.assertEqual(len(nodes), 1)
        self.assertIn('After', nodes[0].inner_html())
        self.assertIn('Last', nodes[0].text())
        self.assertNotIn('Outside', nodes[0].text())

    def test_editor_spacing_and_omitted_cell_end_tags(self):
        self.assertEqual(html_to_markdown('<p>Please<strong> submit </strong>today.</p>'), 'Please **submit** today.')
        md = html_to_markdown('<table><tr><td>A<td>B<tr><td>C<td>D</table>')
        self.assertIn('| A | B |', md)
        self.assertIn('| C | D |', md)


if __name__ == '__main__':
    unittest.main()
