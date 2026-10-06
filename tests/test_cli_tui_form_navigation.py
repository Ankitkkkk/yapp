"""Form field navigation and directory suggestions through real terminal input."""

from pathlib import Path
import tempfile
import unittest

from cli_tui_dialogs import Field, ModalResult
from tests.test_cli_tui_workflows import workspace, workflow_harness


class FormNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_directory_suggestions_draw_above_short_form_and_accept_mouse_selection(self):
        with tempfile.TemporaryDirectory(prefix='yapp-form-') as directory:
            child = Path(directory) / 'nested folder'
            child.mkdir()
            async with workflow_harness(size=(80, 18)) as ui:
                task = ui.start(ui.dialogs.form('Directory', [
                    Field('cwd', 'Working directory:', directory=True),
                ], submit_label='Save'))
                await ui.wait_until(lambda: 'Working directory:' in ui.screen_text())
                await ui.type_text(str(Path(directory) / 'nested'))
                await ui.wait_until(lambda: 'nested folder/' in ui.screen_text())
                self.assertIn('nested folder/', ui.screen_text())
                y, row = next((y, row) for y, row in enumerate(ui.rows) if 'nested folder/' in row)
                await ui.click(row.index('nested folder/'), y)
                self.assertEqual(ui.application.current_buffer.text, str(child))
                self.assertFalse(task.done())
                await ui.key('Enter')
                self.assertEqual(await task, ModalResult({'cwd': str(child)}))

    async def test_directory_completion_leaves_mid_path_edits_and_missing_paths_unchanged(self):
        with tempfile.TemporaryDirectory(prefix='yapp-form-') as directory:
            child = Path(directory) / 'project alpha'
            child.mkdir()
            async with workflow_harness(size=(80, 18)) as ui:
                task = ui.start(ui.dialogs.form('Directory', [
                    Field('cwd', 'Working directory:', default=str(child), directory=True),
                    Field('name', 'Agent name:'),
                ], submit_label='Save'))
                await ui.wait_until(lambda: 'Working directory:' in ui.screen_text())
                buffer = ui.application.current_buffer
                await ui.key('Home')
                await ui.key('Tab')
                await ui.key('Down')
                self.assertEqual(buffer.text, str(child))
                await ui.type_text('worker')
                await ui.key('Up')
                await ui._send('\x01\x0b')
                missing = str(Path(directory) / 'missing')
                await ui.type_text(missing)
                await ui.key('Tab')
                await ui.key('Down')
                self.assertEqual(buffer.text, missing)
                self.assertEqual(ui.application.current_buffer.text, 'worker')
                await ui.key('Enter')
                self.assertEqual(await task, ModalResult({'cwd': missing, 'name': 'worker'}))

    async def test_tab_completes_prefilled_directory_from_end_without_corruption(self):
        with tempfile.TemporaryDirectory(prefix='yapp-form-') as directory:
            child = Path(directory) / 'project alpha'
            child.mkdir()
            prefix = str(Path(directory) / 'project')
            async with workflow_harness(size=(80, 18)) as ui:
                task = ui.start(ui.dialogs.form('Directory', [
                    Field('cwd', 'Working directory:', default=prefix, directory=True),
                ], submit_label='Save'))
                await ui.wait_until(lambda: 'Working directory:' in ui.screen_text())
                await ui.key('Tab')
                await ui.wait_until(lambda: ui.application.current_buffer.text == str(child))
                self.assertEqual(ui.application.current_buffer.text, str(child))
                await ui.key('Enter')
                await ui.key('Enter')
                self.assertEqual(await task, ModalResult({'cwd': str(child)}))

    async def test_arrows_move_fields_and_tab_cycles_choices_without_losing_edits(self):
        async with workflow_harness(size=(80, 18)) as ui:
            task = ui.start(ui.dialogs.form('New session', [
                Field('provider', 'Orchestrator provider:', choices=('codex', 'claude')),
                Field('cwd', 'Working directory:', default='/tmp'),
                Field('flags', 'Provider flags:'),
            ], submit_label='Create session'))
            await ui.wait_until(lambda: 'Orchestrator provider:' in ui.screen_text())
            provider_control = ui.application.layout.current_control
            await ui.key('Tab')
            self.assertIs(ui.application.layout.current_control, provider_control)
            self.assertIn('‹ claude ›', ui.screen_text())
            await ui.key('Tab')
            self.assertIn('‹ codex ›', ui.screen_text())
            await ui.key('ShiftTab')
            self.assertIn('‹ claude ›', ui.screen_text())
            await ui.key('Down')
            self.assertEqual(ui.application.current_buffer.text, '/tmp')
            await ui.key('End')
            await ui.key('Left')
            await ui.type_text('X')
            await ui.key('Tab')  # No choices in this plain text field.
            self.assertEqual(ui.application.current_buffer.text, '/tmXp')
            await ui.key('Down')
            await ui.type_text('--verbose')
            await ui.key('Up')
            self.assertEqual(ui.application.current_buffer.text, '/tmXp')
            await ui.key('Up')
            self.assertIs(ui.application.layout.current_control, provider_control)
            await ui.key('Enter')
            self.assertEqual(await task, ModalResult({
                'provider': 'claude', 'cwd': '/tmXp', 'flags': '--verbose'}))

    async def test_arrows_skip_locked_fields_and_reach_cancel_with_wraparound(self):
        async with workflow_harness(selected=workspace(), size=(80, 18)) as ui:
            ui.view.composer.text = 'keep draft'
            task = ui.start(ui.dialogs.form('Resume', [
                Field('locked', 'Saved directory:', default='/saved', read_only=True),
                Field('name', 'Agent name:', default='worker'),
                Field('other_locked', 'Saved ID:', default='ag_one', read_only=True),
                Field('flags', 'Provider flags:', default='--verbose'),
            ], submit_label='Resume'))
            await ui.wait_until(lambda: 'Saved directory:' in ui.screen_text())
            self.assertEqual(ui.application.current_buffer.text, 'worker')
            await ui.key('Down')
            self.assertEqual(ui.application.current_buffer.text, '--verbose')
            await ui.key('Down')  # Submit.
            await ui.key('Down')  # Cancel.
            await ui.key('Down')  # Wrap to first editable field.
            self.assertEqual(ui.application.current_buffer.text, 'worker')
            await ui.key('Up')  # Wrap to Cancel.
            await ui.key('Enter')
            self.assertEqual(await task, ModalResult(cancelled=True))
            self.assertEqual(ui.view.composer.text, 'keep draft')

    async def test_new_session_directory_suggestions_cycle_and_submit_selected_path(self):
        with tempfile.TemporaryDirectory(prefix='yapp-form-') as directory:
            root = Path(directory)
            for name in ('project alpha', 'project beta'):
                (root / name).mkdir()
            (root / 'project file').write_text('not a directory')
            async with workflow_harness(size=(80, 18)) as ui:
                task = ui.start(ui.workflows.new_session())
                await ui.wait_until(lambda: 'Session name:' in ui.screen_text())
                await ui.type_text('suggestions')
                await ui.key('Enter')
                await ui.wait_until(lambda: 'Orchestrator provider:' in ui.screen_text())
                await ui.key('Tab')  # Pick kilo from codex/kilo.
                await ui.key('Down')
                self.assertEqual(ui.application.current_buffer.text, str(Path.cwd()))
                await ui._send('\x01\x0b')
                await ui.type_text(str(root / 'project'))
                await ui.wait_until(lambda: 'project alpha/' in ui.screen_text()
                                    and 'project beta/' in ui.screen_text())
                self.assertNotIn('project file', ui.screen_text())
                directory_buffer = ui.application.current_buffer
                await ui.key('Tab')
                self.assertEqual(directory_buffer.text, str(root / 'project alpha'))
                await ui.key('Tab')
                self.assertEqual(directory_buffer.text, str(root / 'project beta'))
                await ui.key('ShiftTab')
                self.assertEqual(directory_buffer.text, str(root / 'project alpha'))
                self.assertIs(ui.application.current_buffer, directory_buffer)
                await ui.key('Down')  # Leave with the selected directory.
                self.assertNotIn('project beta/', ui.screen_text())
                await ui.type_text('--model "custom model"')
                await ui.key('Enter')
                self.assertEqual((await task).status, 'completed')
                self.assertEqual(ui.controller.workspace['orchestrator'], {
                    'enabled': True, 'provider': 'kilo', 'cwd': str(root / 'project alpha'),
                    'provider_args': ['--model', 'custom model']})

    async def test_directory_completion_enter_accepts_without_starting_agent(self):
        with tempfile.TemporaryDirectory(prefix='yapp-form-') as directory:
            child = Path(directory) / 'nested folder'
            child.mkdir()
            async with workflow_harness(selected=workspace(), size=(120, 35)) as ui:
                task = ui.start(ui.workflows.agent_form('spawn'))
                await ui.wait_until(lambda: 'Working directory:' in ui.screen_text())
                await ui.key('Down')
                self.assertEqual(ui.application.current_buffer.text, str(Path.cwd()))
                await ui._send('\x01\x0b')
                await ui.type_text(str(Path(directory) / 'nested'))
                await ui.wait_until(lambda: 'nested folder/' in ui.screen_text())
                await ui.key('Tab')
                await ui.key('Enter')
                self.assertIn('New agent · 1 of 2', ui.screen_text())
                self.assertEqual(ui.application.current_buffer.text, str(child))
                self.assertIsNone(ui.application.current_buffer.complete_state)
                await ui.key('Enter')
                await ui.wait_until(lambda: 'New agent · 2 of 2' in ui.screen_text())
                await ui.key('Enter')
                self.assertEqual((await task).status, 'completed')
                self.assertEqual(ui.controller.workspace['agents'][0]['cwd'], str(child))
