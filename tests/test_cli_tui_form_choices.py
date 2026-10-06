"""Compact form choices remain reachable through keyboard and mouse input."""
import unittest

from agent_profiles import PERSONALITY_CHOICES, ROLE_CHOICES
from cli_tui_dialogs import Field, ModalResult
from tests.test_cli_tui_workflows import agent, workspace, workflow_harness


class FormChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_many_providers_leave_fields_and_actions_visible_on_short_terminal(self):
        async with workflow_harness(selected=workspace(), size=(80, 18)) as ui:
            ui.controller.providers = [f'provider-{i:02}' for i in range(20)]
            task = ui.start(ui.workflows.agent_form('spawn'))
            await ui.wait_until(lambda: 'Provider:' in ui.screen_text())
            screen = ui.screen_text()
            for caption in ('Working directory:', 'Agent name:', 'History mode',
                            'Provider flags:', 'Next', 'Cancel'):
                self.assertIn(caption, screen)
            self.assertIn('provider-00', screen)
            self.assertNotIn('provider-01', screen)
            await ui.key('ShiftTab')
            self.assertIn('provider-19', ui.screen_text())
            await ui.key('Down')
            info = ui.application.layout.current_window.render_info
            self.assertGreaterEqual(info.window_width, 60)
            await ui.key('Escape')
            self.assertEqual((await task).status, 'cancelled')

    async def test_add_agent_cycles_provider_history_and_profile_options(self):
        async with workflow_harness(selected=workspace(), size=(120, 35)) as ui:
            ui.controller.providers = ['claude', 'codex', 'gemini', 'kilo', 'custom-provider']
            task = ui.start(ui.workflows.agent_form('spawn'))
            await ui.wait_until(lambda: 'Provider:' in ui.screen_text())
            for value in ui.controller.providers:
                self.assertIn('‹ ' + value + ' ›', ui.screen_text())
                await ui.key('Tab')
            self.assertIn('‹ literal ›', ui.screen_text())
            y, row = next((y, row) for y, row in enumerate(ui.rows) if '‹ claude ›' in row)
            await ui.click(row.index('›'), y)
            self.assertIn('‹ codex ›', ui.screen_text())
            await ui.focus_field('cwd')
            await ui._send('\x01\x0b')
            await ui.type_text('/tmp')
            await ui.key('Enter')
            await ui.wait_until(lambda: 'New agent · 2 of 2' in ui.screen_text())
            for choices in (ROLE_CHOICES, PERSONALITY_CHOICES):
                for value in choices:
                    self.assertIn('‹ ' + value + ' ›', ui.screen_text())
                    await ui.key('Tab')
                await ui.key('Down')
            await ui.key('Escape')
            await ui.wait_until(task.done)
            ui.api.action.assert_not_called()

    async def test_shared_choice_fields_cycle_without_expanding_all_alternatives(self):
        cases = [('Provider:', ('provider-first', 'provider-second', 'provider-third')),
                 ('History mode:', ('none', 'literal')),
                 ('Launch mode:', ('ordinary', 'fresh')),
                 ('Role:', ROLE_CHOICES),
                 ('Personality:', PERSONALITY_CHOICES)]
        for label, choices in cases:
            with self.subTest(label=label):
                async with workflow_harness(size=(80, 18)) as ui:
                    task = ui.start(ui.dialogs.form('Choose option', [
                        Field('choice', label, default=choices[-1], choices=choices),
                    ], submit_label='Apply'))
                    await ui.wait_until(lambda: 'Choose option' in ui.screen_text())
                    self.assertIn('‹ ' + choices[-1] + ' ›', ui.screen_text())
                    for value in choices[:-1]:
                        self.assertNotIn('‹ ' + value + ' ›', ui.screen_text())
                    await ui.key('ShiftTab')
                    await ui.key('Enter')
                    self.assertEqual(await task, ModalResult({'choice': choices[-2]}))

    async def test_compact_many_choice_form_keeps_actions_errors_and_mouse_selection(self):
        async with workflow_harness(size=(80, 18)) as ui:
            choices = tuple(f'provider-{number:02}' for number in range(20))
            task = ui.start(ui.dialogs.form('Long form', [
                Field('provider', 'Provider:', choices=choices),
                Field('name', 'Agent name:', required=True),
                Field('mode', 'History mode:', default='literal', choices=('none', 'literal')),
            ], submit_label='Save', error='Earlier failure'))
            await ui.wait_until(lambda: 'Long form' in ui.screen_text())
            self.assertIn('provider-00', ui.screen_text())
            self.assertIn('Save', ui.screen_text())
            self.assertIn('Cancel', ui.screen_text())
            self.assertIn('Earlier failure', ui.screen_text())
            for _ in range(19):
                await ui.key('Tab')
            self.assertIn('provider-19', ui.screen_text())
            self.assertIn('Earlier failure', ui.screen_text())
            await ui.key('Enter')
            self.assertFalse(task.done())
            self.assertIn('Agent name is required', ui.screen_text())
            self.assertIn('Agent name:', ui.screen_text())
            await ui.type_text('reviewer')
            await ui.key('Down')
            y, row = next((y, row) for y, row in enumerate(ui.rows) if '‹ literal ›' in row)
            await ui.click(row.index('‹'), y)
            self.assertIn('‹ none ›', ui.screen_text())
            await ui.key('Enter')
            self.assertEqual(await task, ModalResult({'provider': 'provider-19',
                                                      'name': 'reviewer', 'mode': 'none'}))

    async def test_resize_and_escape_restore_draft_cursor_and_normal_mode(self):
        async with workflow_harness(selected=workspace(agents=[agent()]), size=(120, 35)) as ui:
            ui.view.composer.text = 'keep this draft'
            ui.view.composer.buffer.cursor_position = 4
            await ui.key('Escape')
            task = ui.start(ui.dialogs.form('Profile choices', [
                Field('role', 'Role:', choices=ROLE_CHOICES),
                Field('personality', 'Personality:', choices=PERSONALITY_CHOICES),
            ], submit_label='Save'))
            await ui.wait_until(lambda: 'Profile choices' in ui.screen_text())
            await ui.resize(80, 18)
            self.assertIn('‹ ' + ROLE_CHOICES[0] + ' ›', ui.screen_text())
            await ui.key('Tab')
            await ui.key('Down')
            self.assertIn('‹ ' + PERSONALITY_CHOICES[0] + ' ›', ui.screen_text())
            await ui.key('ShiftTab')
            await ui.resize(120, 35)
            self.assertIn('‹ ' + ROLE_CHOICES[1] + ' ›', ui.screen_text())
            self.assertIn('‹ ' + PERSONALITY_CHOICES[-1] + ' ›', ui.screen_text())
            await ui.key('Escape')
            self.assertEqual(await task, ModalResult(cancelled=True))
            self.assertEqual(ui.focused_control, 'composer')
            self.assertEqual(ui.view.composer.text, 'keep this draft')
            self.assertEqual(ui.view.composer.buffer.cursor_position, 4)
            self.assertEqual(ui.view.composer_mode, 'NORMAL')
