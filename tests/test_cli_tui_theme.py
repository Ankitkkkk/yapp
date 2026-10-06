"""Real rendered surfaces must remain legible across terminal palettes."""

import asyncio
import os
import unittest
from unittest.mock import patch

from prompt_toolkit.output import ColorDepth

from cli_tui_dialogs import Field
from cli_tui_theme import terminal_color_depth
from tests._tui_harness import tui_harness


def contrast(foreground, background):
    def luminance(color):
        values = [int(color[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
        return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))
    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + .05) / (dark + .05)


class ThemeTests(unittest.IsolatedAsyncioTestCase):
    async def test_compact_choice_focus_is_visible_and_readable(self):
        async with tui_harness() as ui:
            task = asyncio.create_task(ui.dialogs.form('Provider', [
                Field('provider', 'Provider:', choices=('codex', 'claude')),
                Field('name', 'Agent name:')], submit_label='Next'))
            try:
                await ui.wait_until(lambda: '‹ codex ›' in ui.screen_text())
                y, row = next((y, row) for y, row in enumerate(ui.rows) if '‹ codex ›' in row)
                x = row.index('codex')
                def attributes():
                    cell = ui.application.renderer.last_rendered_screen.data_buffer[y][x]
                    return ui.application._merged_style.get_attrs_for_style_str(cell.style)
                focused = attributes()
                self.assertGreaterEqual(contrast(focused.color, focused.bgcolor), 4.5)
                await ui.key('Down')
                self.assertNotEqual(focused.bgcolor, attributes().bgcolor)
                await ui.key('Up')
                self.assertEqual(focused.bgcolor, attributes().bgcolor)
            finally:
                ui.dialogs.cancel()
                await task

    async def test_empty_canvas_paints_every_cell_without_terminal_defaults(self):
        for size in ((120, 35), (80, 18)):
            with self.subTest(size=size):
                async with tui_harness(size=size) as ui:
                    screen = ui.application.renderer.last_rendered_screen
                    missing = []
                    for y in range(size[1]):
                        for x in range(size[0]):
                            attrs = ui.application._merged_style.get_attrs_for_style_str(
                                screen.data_buffer[y][x].style)
                            if len(attrs.bgcolor or '') != 6:
                                missing.append((x, y, attrs.bgcolor))
                    self.assertFalse(bool(missing), 'Unpainted/default background cells: ' + str(missing[:8]))

    async def test_dialog_focus_uses_readable_distinct_dark_surface(self):
        async with tui_harness() as ui:
            task = asyncio.create_task(ui.dialogs.form('New agent', [
                Field('name', 'Agent name:', default='reviewer'),
                Field('cwd', 'Working directory:', default='/work/project')], submit_label='Start agent'))
            try:
                await ui.wait_until(lambda: 'reviewer' in ui.screen_text())
                y, row = next((y, row) for y, row in enumerate(ui.rows) if 'reviewer' in row)
                cell = ui.application.renderer.last_rendered_screen.data_buffer[y][row.index('reviewer')]
                attrs = ui.application._merged_style.get_attrs_for_style_str(cell.style)
                self.assertIn(attrs.bgcolor, ('313244', '45475a'))
                self.assertGreaterEqual(contrast(attrs.color, attrs.bgcolor), 4.5)
                self.assertFalse(attrs.underline)
                self.assertFalse(attrs.reverse)
                await ui.key('Down')
                cell = ui.application.renderer.last_rendered_screen.data_buffer[y][row.index('reviewer')]
                unfocused = ui.application._merged_style.get_attrs_for_style_str(cell.style)
                self.assertNotEqual(attrs.bgcolor, unfocused.bgcolor)
            finally:
                ui.dialogs.cancel()
                await task

    async def test_focused_dialog_button_has_one_readable_highlight(self):
        async with tui_harness() as ui:
            task = asyncio.create_task(ui.dialogs.confirm('Continue?', default=True))
            try:
                await ui.wait_until(lambda: 'Continue?' in ui.screen_text())
                y, row = next((y, row) for y, row in enumerate(ui.rows) if '<' in row and 'Yes' in row)
                start, end = row.index('<'), row.index('>')
                backgrounds = set()
                for x in range(start, end + 1):
                    cell = ui.application.renderer.last_rendered_screen.data_buffer[y][x]
                    attrs = ui.application._merged_style.get_attrs_for_style_str(cell.style)
                    backgrounds.add(attrs.bgcolor)
                    self.assertGreaterEqual(contrast(attrs.color, attrs.bgcolor), 4.5)
                    self.assertFalse(attrs.reverse)
                self.assertEqual(backgrounds, {'b4befe'})
                await ui.key('Tab')
                cell = ui.application.renderer.last_rendered_screen.data_buffer[y][start]
                self.assertNotIn(ui.application._merged_style.get_attrs_for_style_str(cell.style).bgcolor,
                                 backgrounds)
            finally:
                ui.dialogs.cancel()
                await task


class ColorDepthTests(unittest.TestCase):
    def test_advertised_rgb_and_explicit_overrides(self):
        cases = [({'TERM': 'xterm-kitty'}, ColorDepth.DEPTH_24_BIT),
                 ({'COLORTERM': 'truecolor'}, ColorDepth.DEPTH_24_BIT),
                 ({'COLORTERM': '24BIT'}, ColorDepth.DEPTH_24_BIT),
                 ({'TERM': 'xterm-256color'}, None),
                 ({'TERM': 'dumb'}, None),
                 ({'TERM': 'xterm-kitty', 'PROMPT_TOOLKIT_COLOR_DEPTH': 'DEPTH_8_BIT'},
                  ColorDepth.DEPTH_8_BIT),
                 ({'COLORTERM': 'truecolor', 'NO_COLOR': '1'}, ColorDepth.DEPTH_1_BIT)]
        for environment, expected in cases:
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True):
                self.assertEqual(terminal_color_depth(), expected)
