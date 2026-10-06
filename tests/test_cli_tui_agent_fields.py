"""Agent form text fields accept mouse focus and submit the edited values."""
import tempfile
import unittest

from tests.test_cli_tui_workflows import agent, workspace, workflow_harness


class AgentFieldTests(unittest.IsolatedAsyncioTestCase):
    async def submit_profile(self, ui):
        await ui.wait_until(lambda: 'New agent · 2 of 2' in ui.screen_text())
        screen = ui.screen_text()
        for text in ('Role:', 'generalist', 'Personality:', 'pragmatic', 'Start agent'):
            self.assertIn(text, screen)
        await ui.key('Down')
        await ui.key('Down')
        await ui.key('Enter')

    async def test_resume_directory_is_prefilled_read_only_and_uses_saved_directory(self):
        with tempfile.TemporaryDirectory() as cwd:
            saved = agent(state='exited', cwd=cwd)
            async with workflow_harness(selected=workspace(agents=[saved]), size=(80, 24)) as ui:
                task = ui.start(ui.workflows.agent_form('resume', saved['agent_id']))
                await ui.wait_until(lambda: 'Resume agent' in ui.screen_text())
                # Default focus skips the locked directory so no input is required.
                self.assertEqual(ui.application.current_buffer.text, '')
                y, row = next((y, row) for y, row in enumerate(ui.rows) if 'Working directory' in row)
                await ui.click(row.index('Working directory'), y + 1)
                self.assertEqual(ui.application.current_buffer.text, cwd)
                await ui._send('\x01\x0b')
                await ui.type_text('/cannot-change')
                self.assertEqual(ui.application.current_buffer.text, cwd)
                await ui.key('Enter')
                await ui.wait_until(task.done)
                self.assertEqual((await task).status, 'completed')
                self.assertIsNone(ui.api.action.call_args.kwargs['body']['cwd'])
                self.assertEqual(ui.controller.workspace['agents'][0]['cwd'], cwd)

    async def test_provider_flags_are_parsed_with_quotes_and_resume_can_clear(self):
        for action in ('spawn', 'resume'):
            saved = dict(agent(state='exited'), provider_args=['--model', 'saved model'])
            async with workflow_harness(selected=workspace(agents=[saved]), size=(80, 24)) as ui:
                task = ui.start(ui.workflows.agent_form(action, saved['agent_id']))
                await ui.wait_until(lambda: 'Provider flags:' in ui.screen_text())
                y, row = next((y, row) for y, row in enumerate(ui.rows) if 'Provider flags:' in row)
                await ui.click(row.index('Provider flags:'), y + 1)
                if action == 'spawn':
                    await ui.type_text('--model "unfinished')
                    await ui.key('Enter')
                    await ui.wait_until(lambda: 'Invalid provider flags' in ui.screen_text())
                    ui.api.action.assert_not_called()
                    y, row = next((y, row) for y, row in enumerate(ui.rows) if 'Provider flags:' in row)
                    await ui.click(row.index('Provider flags:'), y + 1)
                    self.assertEqual(ui.application.current_buffer.text, '--model "unfinished')
                    await ui._send('\x01\x0b')
                    await ui.type_text('--model "custom model" --verbose')
                    expected = ['--model', 'custom model', '--verbose']
                else:
                    self.assertIn('saved model', ui.application.current_buffer.text)
                    await ui._send('\x01\x0b')
                    expected = []
                await ui.key('Enter')
                if action == 'spawn':
                    await self.submit_profile(ui)
                await ui.wait_until(task.done)
                self.assertEqual((await task).status, 'completed')
                self.assertEqual(ui.api.action.call_args.kwargs['body']['provider_args'], expected)

    async def test_click_directory_and_name_edits_spawn_payload(self):
        with tempfile.TemporaryDirectory() as cwd:
            for size in ((120, 35), (80, 24)):
                async with workflow_harness(selected=workspace(agents=[agent(cwd='/tmp')]), size=size) as ui:
                    ui.view.composer.text = 'keep chat draft'
                    task = ui.start(ui.workflows.agent_form('spawn'))
                    await ui.wait_until(lambda: 'Working directory:' in ui.screen_text())
                    y, row = next((y, row) for y, row in enumerate(ui.rows) if 'Working directory:' in row)
                    await ui.click(row.index('Working directory:'), y + 1)
                    self.assertEqual(ui.application.current_buffer.text, '/tmp')
                    await ui._send('\x01\x0b')  # Home, clear line.
                    await ui.type_text(cwd)
                    y, row = next((y, row) for y, row in enumerate(ui.rows) if 'Agent name:' in row)
                    await ui.click(row.index('Agent name:') + 2, y + 1)
                    self.assertEqual(ui.application.current_buffer.text, '')
                    await ui.type_text('worker-custom')
                    ui.api.action.assert_not_called()
                    # Submit using the actual button after editing both inputs by mouse.
                    y, row = next((y, row) for y, row in enumerate(ui.rows) if 'Next' in row)
                    await ui.click(row.index('Next'), y)
                    await self.submit_profile(ui)
                    await ui.wait_until(task.done)
                    self.assertEqual((await task).status, 'completed')
                    self.assertEqual(ui.api.action.call_args.kwargs['body'],
                                     dict(provider='codex', cwd=cwd, name='worker-custom', history_mode='literal',
                                          role='generalist', personality='pragmatic'))
                    self.assertEqual(ui.view.composer.text, 'keep chat draft')

    async def test_directory_validation_retains_name_and_allows_mouse_correction(self):
        async with workflow_harness(selected=workspace()) as ui:
            task = ui.start(ui.workflows.agent_form('spawn'))
            await ui.wait_until(lambda: 'Working directory:' in ui.screen_text())
            y, row = next((y, row) for y, row in enumerate(ui.rows) if 'Agent name:' in row)
            await ui.click(row.index('Agent name:') + 2, y + 1)
            await ui.type_text('my-agent')
            y, row = next((y, row) for y, row in enumerate(ui.rows) if 'Working directory:' in row)
            await ui.click(row.index('Working directory:'), y + 1)
            await ui._send('\x01\x0b')
            await ui.type_text('relative-invalid')
            await ui.key('Enter')
            await ui.wait_until(lambda: 'absolute existing directory' in ui.screen_text())
            self.assertIn('my-agent', ui.screen_text())
            ui.api.action.assert_not_called()
            y, row = next((y, row) for y, row in enumerate(ui.rows) if 'Working directory:' in row)
            await ui.click(row.index('Working directory:'), y + 1)
            await ui._send('\x01\x0b')
            await ui.type_text('/tmp')
            await ui.key('Enter')
            await self.submit_profile(ui)
            await ui.wait_until(task.done)
            self.assertEqual(ui.api.action.call_args.kwargs['body']['cwd'], '/tmp')
            self.assertEqual(ui.api.action.call_args.kwargs['body']['name'], 'my-agent')
