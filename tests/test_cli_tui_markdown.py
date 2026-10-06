"""Markdown presentation and lossless raw-copy mode through the terminal view."""

import unittest

from prompt_toolkit.utils import get_cwidth

from cli_tui_markdown import markdown_lines
from tests._tui_harness import tui_harness


def content_text(control, width=70, height=30):
    content = control.create_content(width, height)
    return '\n'.join(''.join(part[1] for part in content.get_line(i)).rstrip()
                     for i in range(content.line_count))


class MarkdownRendererTests(unittest.TestCase):
    def text(self, source, width):
        lines = markdown_lines(source, width)
        text = '\n'.join(''.join(part[1] for part in line).rstrip() for line in lines)
        self.assertTrue(all(get_cwidth(line) <= width for line in text.splitlines()))
        return text

    def test_narrow_tables_retain_values_instead_of_ellipsis(self):
        source = '| Name | Value |\n| --- | --- |\n| 主机 | localhost |\n| mode | **ready** |'
        for width in (70, 20, 12):
            with self.subTest(width=width):
                text = self.text(source, width)
                self.assertIn('主机', text)
                self.assertIn('localhost', ''.join(text.split()))
                self.assertIn('ready', ''.join(text.split()))
                self.assertNotIn('**', text)

    def test_header_only_narrow_table_remains_visible(self):
        text = self.text('| First | Second |\n| --- | --- |', 12)
        self.assertIn('First', text)
        self.assertIn('Second', text)

    def test_nested_quotes_keep_their_text_on_narrow_panels(self):
        text = self.text('> ' * 12 + '**nested quote**', 20)
        self.assertIn('nested quote', text)
        self.assertNotIn('**', text)

    def test_deeply_nested_lists_do_not_lose_leaf_text(self):
        for depth_limit in (12, 40):
            with self.subTest(depth=depth_limit):
                source = '\n'.join('  ' * depth + '- item' for depth in range(depth_limit))
                source += '\n' + '  ' * depth_limit + '- **leaf**'
                text = self.text(source, 20)
                self.assertIn('leaf', text)

    def test_escaped_markdown_and_entities_render_as_literal_characters(self):
        self.assertEqual(self.text(r'\*literal\* &amp; done', 70), '*literal* & done')

    def test_images_stay_in_order_as_text_without_losing_their_url(self):
        text = self.text('before ![diagram](https://example.com/plot.png) after', 70)
        self.assertLess(text.index('before'), text.index('diagram'))
        self.assertLess(text.index('diagram'), text.index('after'))
        self.assertIn('https://example.com/plot.png', text)

    def test_partial_code_and_unknown_language_preserve_literal_content(self):
        for fence in ('```python', '```unknown-language'):
            with self.subTest(fence=fence):
                text = self.text(fence + '\n    value = "**literal**"', 70)
                self.assertIn('    value = "**literal**"', text)
                self.assertNotIn(fence, text)

    def test_markup_never_emits_terminal_controls_or_treats_html_as_markup(self):
        source = '**safe**\x1b[31m\x00\n&#27;[2J <script>literal</script>'
        for raw in (False, True):
            text = '\n'.join(''.join(part[1] for part in line)
                             for line in markdown_lines(source, 70, raw=raw))
            self.assertNotIn('\x1b', text)
            self.assertNotIn('\x00', text)
            self.assertIn('<script>literal</script>', text)

    def test_code_and_unicode_wrap_without_losing_characters(self):
        code = '界界 e\u0301 ' + 'abcdefgh' * 5
        text = self.text('```text\n' + code + '\n```', 20)
        self.assertEqual(''.join(text.split()), ''.join(code.split()))


class MarkdownViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_bullet_notice_keeps_its_message_visible_in_status_strip(self):
        async with tui_harness(size=(120, 35)) as ui:
            ui.notice('- **Ready now**')
            await ui.wait_render()
            self.assertIn('Ready now', ui.rows[-2])
            self.assertNotIn('**Ready now**', ui.rows[-2])
            await ui.key('F7')
            self.assertIn('- **Ready now**', ui.rows[-2])

    async def test_f7_round_trip_preserves_scrolled_markdown_position(self):
        table = '| ' + ' | '.join(f'H{i}' for i in range(10)) + ' |\n'
        table += '|' + '---|' * 10 + '\n'
        table += '\n'.join('|' + '|'.join(f'v{row}_{column}' for column in range(10)) + '|'
                           for row in range(8))
        for surface in ('conversation', 'activity', 'help', 'inspector'):
            with self.subTest(surface=surface):
                async with tui_harness(size=(80, 24)) as ui:
                    if surface == 'conversation':
                        ui.client.handle_event({'type': 'message', 'data': {
                            'id': 1, 'sender': 'agent', 'text': table}})
                        ui.state.viewport.anchor(1, ui.client.messages, line_offset=35)
                        offset = lambda: ui.state.viewport.line_offset
                    elif surface == 'activity':
                        ui.notice(table)
                        ui.view.show_activity()
                        ui.view._activity_line = 35
                        offset = lambda: ui.view._activity_line
                    elif surface == 'help':
                        ui.view.show_help(table)
                        ui.view._help_line = 35
                        offset = lambda: ui.view._help_line
                    else:
                        ui.controller._select({'id': 'w', 'channel': 'general', 'agents': [{
                            'agent_id': 'a', 'registry_name': 'agent', 'history_note': table}]})
                        ui.state.selected_agent_id = 'a'
                        ui.view.show_inspector()
                        ui.view._inspector_line = 35
                        offset = lambda: ui.view._inspector_line
                    await ui.wait_render()
                    self.assertEqual(offset(), 35)
                    await ui.key('F7')
                    await ui.key('F7')
                    self.assertEqual(offset(), 35)

    async def test_chat_renders_markdown_styles_and_preserves_code(self):
        source = ('## Connection setup\n\nUse **both passwords** and *separate settings*.\n\n'
                  '- Configure `redis.password`\n- Check the connection\n\n'
                  '> Keep credentials outside source control.\n\n'
                  '```python\nif ready:\n    connect(password="secret")\n```\n\n'
                  '[Guide](https://example.com/guide)')
        async with tui_harness(size=(120, 45)) as ui:
            ui.client.handle_event({'type': 'message', 'data': {
                'id': 1, 'sender': 'agent', 'text': source}})
            await ui.wait_render()
            screen = ui.screen_text()
            self.assertIn('Connection setup', screen)
            self.assertNotIn('## Connection setup', screen)
            self.assertNotIn('**both passwords**', screen)
            self.assertNotIn('```', screen)
            self.assertIn('    connect(password="secret")', screen)
            self.assertIn('https://example.com/guide', screen)
            for word, attribute in (('Connection setup', 'bold'),
                                    ('both passwords', 'bold'),
                                    ('separate settings', 'italic')):
                y, row = next((y, row) for y, row in enumerate(ui.rows) if word in row)
                cell = ui.application.renderer.last_rendered_screen.data_buffer[y][row.index(word)]
                attrs = ui.application._merged_style.get_attrs_for_style_str(cell.style)
                self.assertTrue(getattr(attrs, attribute), word)
            self.assertEqual(ui.client.messages[1]['text'], source)

    async def test_f7_shows_original_markdown_and_restores_styled_chat_and_draft(self):
        source = '# Copy source\n\n**Bold** and `code`\n\n```python\n    value = "x"\n```'
        for size in ((120, 35), (80, 24)):
            with self.subTest(size=size):
                async with tui_harness(size=size) as ui:
                    ui.client.handle_event({'type': 'message', 'data': {
                        'id': 1, 'sender': 'agent', 'text': source}})
                    await ui.type_text('unfinished')
                    await ui.key('Left')
                    cursor = ui.view.composer.buffer.cursor_position
                    self.assertNotIn('# Copy source', ui.screen_text())
                    await ui.key('F7')
                    start = next(i for i, row in enumerate(ui.rows) if '# Copy source' in row)
                    self.assertEqual('\n'.join(ui.rows[start:start + len(source.splitlines())]), source)
                    await ui.key('F7')
                    self.assertNotIn('# Copy source', ui.screen_text())
                    self.assertNotIn('```', ui.screen_text())
                    self.assertEqual(ui.view.composer.text, 'unfinished')
                    self.assertEqual(ui.view.composer.buffer.cursor_position, cursor)
                    self.assertEqual(ui.focused_control, 'composer')
                    self.assertEqual(ui.client.messages[1]['text'], source)

    async def test_activity_renders_markdown_and_f7_keeps_raw_notice_source(self):
        source = '## Unread result\n\n**Ready**\n\n```text\n    original indentation\n```'
        async with tui_harness(size=(120, 35)) as ui:
            ui.notice(source)
            await ui.key('F5')
            rendered = content_text(ui.view.activity)
            self.assertIn('Unread result', rendered)
            self.assertNotIn('## Unread result', rendered)
            self.assertNotIn('**Ready**', rendered)
            self.assertNotIn('```', rendered)
            await ui.key('F7')
            self.assertEqual(content_text(ui.view.activity), source)
            await ui.key('F7')
            self.assertNotIn('**Ready**', content_text(ui.view.activity))

    async def test_help_formats_markdown_but_preserves_command_lines(self):
        async with tui_harness(size=(120, 35)) as ui:
            ui.view.show_help('## Help topic\nUse **care**.\n/one  First command\n/two  Next command')
            await ui.wait_render()
            text = content_text(ui.view.help)
            self.assertNotIn('## Help topic', text)
            self.assertNotIn('**care**', text)
            self.assertIn('/one  First command\n/two  Next command', text)
            await ui.key('F7')
            self.assertIn('## Help topic', content_text(ui.view.help))
            self.assertIn('**care**', content_text(ui.view.help))
            await ui.key('F7')
            self.assertNotIn('**care**', content_text(ui.view.help))
            self.assertTrue(ui.view.help_visible)

    async def test_markdown_updates_reflow_after_message_edit_and_resize(self):
        async with tui_harness(size=(120, 35)) as ui:
            ui.client.handle_event({'type': 'message', 'data': {
                'id': 1, 'sender': 'agent', 'text': '## Original\n\n**before**'}})
            await ui.wait_render()
            self.assertIn('Original', ui.screen_text())
            ui.client.handle_event({'type': 'message', 'data': {
                'id': 1, 'sender': 'agent', 'text': '## Updated\n\n**after**'}})
            await ui.resize(80, 24)
            self.assertNotIn('Original', ui.screen_text())
            self.assertIn('Updated', ui.screen_text())
            self.assertNotIn('## Updated', ui.screen_text())
            await ui.key('F7')
            self.assertIn('## Updated', ui.screen_text())
            self.assertIn('**after**', ui.screen_text())

    async def test_agent_notes_and_notice_strip_render_markdown(self):
        async with tui_harness(size=(120, 35)) as ui:
            ui.controller._select({'id': 'one', 'channel': 'general', 'agents': [{
                'agent_id': 'ag_one', 'registry_name': 'worker', 'last_state': 'running',
                'history_note': '**History loaded**', 'last_error': 'Check `settings`'}]})
            ui.state.selected_agent_id = 'ag_one'
            ui.view.show_inspector()
            ui.notice('**Ready now**')
            await ui.wait_render()
            self.assertIn('History loaded', ui.screen_text())
            self.assertNotIn('**History loaded**', ui.screen_text())
            self.assertNotIn('`settings`', ui.screen_text())
            self.assertNotIn('**Ready now**', ui.screen_text())
            await ui.key('F7')
            self.assertIn('**History loaded**', ui.screen_text())
            self.assertIn('**Ready now**', ui.screen_text())
