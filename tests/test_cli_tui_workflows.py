"""Guided workflows through real controller, dialogs, renderer and pipe input."""
import asyncio
import copy
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from cli import ChatClient
from cli_api import CLIError
import cli_tui_dialogs
from cli_view_contracts import SubmitOutcome
from cli_workspace_chat import WorkspaceChatController
from cli_workspaces import WorkspaceAPI
from tests._tui_harness import tui_harness


def workspace(ident='ws_one', name='One', *, agents=(), archived=False):
    return dict(id=ident, name=name, channel=ident, agents=list(agents), archived=archived,
                created_at='2026-09-13T00:00:00Z', updated_at='2026-09-13T00:00:00Z')


def agent(ident='ag_one', *, cwd='/tmp', state='exited'):
    return dict(agent_id=ident, registry_name=ident, provider='codex', cwd=cwd,
                last_state=state, native_session_id='native', tmux_session='inert',
                history_mode='literal', history_state='done', history_note='literal delivered',
                unread_count=0, last_error=None, last_launch=None)


@asynccontextmanager
async def workflow_harness(*, rows=(), selected=None, plain=False, no_resume=True, size=(120, 35)):
    records = {row['id']: copy.deepcopy(row) for row in rows}
    if selected:
        records[selected['id']] = copy.deepcopy(selected)
    api = Mock(spec=WorkspaceAPI)
    api.list.side_effect = lambda *, include_archived=False: {'workspaces': [copy.deepcopy(w)
        for w in records.values() if include_archived or not w['archived']]}
    api.get.side_effect = lambda ident: copy.deepcopy(records[ident])
    def create(name, *, orchestrator=None):
        records['ws_created'] = workspace('ws_created', name)
        if orchestrator is not None:
            records['ws_created']['orchestrator'] = dict(orchestrator, enabled=True)
        return copy.deepcopy(records['ws_created'])
    api.create.side_effect = create
    def rename(ident, name):
        records[ident]['name'] = name
        return copy.deepcopy(records[ident])
    api.rename.side_effect = rename
    def action(ident, action, agent_id=None, body=None):
        ws = records[ident]
        if action == 'remove':
            ws['agents'] = [a for a in ws['agents'] if a['agent_id'] != agent_id]
            return copy.deepcopy(ws)
        if action in ('archive', 'unarchive'):
            ws['archived'] = action == 'archive'
            return copy.deepcopy(ws)
        if action == 'spawn':
            value = agent('ag_created', cwd=body['cwd'], state='starting')
            value.update(provider=body['provider'], history_mode=body['history_mode'])
            if body.get('role') or body.get('personality'):
                value['profile'] = {'role': body.get('role', 'generalist'),
                                    'personality': body.get('personality', 'pragmatic')}
            ws['agents'].append(value)
            return copy.deepcopy(value)
        if agent_id:
            value = next(a for a in ws['agents'] if a['agent_id'] == agent_id)
            if action == 'resume':
                value['last_state'] = 'starting'
            if action == 'history':
                value['history_mode'] = body['mode']
            return copy.deepcopy(value)
        return copy.deepcopy(ws)
    api.action.side_effect = action
    api.unread.return_value = {'agents': []}
    client = ChatClient('http://127.0.0.1:18300')
    client.channels = ['general', 'other']
    controller = WorkspaceChatController(client, api, plain_channel=plain,
                                         no_resume=no_resume, providers=['codex', 'kilo'])
    if selected:
        controller._select(copy.deepcopy(selected))
    async with tui_harness(size=size, controller=controller) as ui:
        ui.records = records
        ui.bind_submit(client.submit_outcome)
        ui.workflows = cli_tui_dialogs.TuiWorkflows(client, controller, ui.view, ui.dialogs,
                                                  ui.state, composer_actions=ui.composer_actions)
        ui.callbacks.update(navigate=ui.workflows.navigate, run_action=ui.workflows.run_action)
        controller.presentation.confirm_selection = ui.workflows.confirm_selection
        ui.tasks = []
        def start(coro):
            task = asyncio.create_task(coro)
            ui.tasks.append(task)
            return task
        ui.start = start
        try:
            yield ui
        finally:
            ui.dialogs.cancel()
            for task in ui.tasks:
                task.cancel()
            await controller.cancel_selection()
            await asyncio.gather(*ui.tasks, return_exceptions=True)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_remove_stopped_agent_confirmation_and_return_to_message(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            ui.state.selected_agent_id = 'ag_one'
            ui.view.composer.text = 'keep my draft'
            await ui.key('F3')
            await self.modal(ui, 'Choose agent')
            await ui.key('Enter')
            await self.modal(ui, 'Agent actions: ag_one')
            await ui.type_text('Remove agent')
            await ui.key('Enter')
            await self.modal(ui, 'Remove')
            self.assertIn('tmux', ui.screen_text())
            await ui.key('Escape')
            ui.api.action.assert_not_called()
            self.assertEqual(len(ui.controller.workspace['agents']), 1)
            task = ui.start(ui.workflows.run_action('remove', target_id='ag_one'))
            await self.modal(ui, 'Remove')
            await ui.key('Left')
            await ui.key('Enter')
            await task
            ui.api.action.assert_called_once_with('ws_one', 'remove', 'ag_one')
            self.assertEqual(ui.controller.workspace['agents'], [])
            self.assertEqual(ui.view.composer.text, 'keep my draft')
            self.assertEqual(ui.focused_control, 'composer')

    async def test_escape_from_agent_menu_and_details_returns_to_typing(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            ui.state.selected_agent_id = 'ag_one'
            ui.view.composer.text = 'draft stays'
            ui.view.composer.buffer.cursor_position = 5
            await ui.key('F3')
            await self.modal(ui, 'Choose agent')
            await ui.key('Enter')
            await self.modal(ui, 'Agent actions: ag_one')
            await ui.type_text('Inspect agent')
            await ui.key('Enter')
            await ui.wait_until(lambda: ui.view.inspecting)
            await ui.key('Escape')
            self.assertEqual(ui.focused_control, 'composer')
            self.assertFalse(ui.view.inspecting)
            await ui.type_text('!')
            self.assertEqual(ui.view.composer.text, 'draft! stays')
            # Cancel a menu opened from the agent pane, then leave that pane.
            ui.view.focus_named('agents')
            await ui.key('F3')
            await self.modal(ui, 'Choose agent')
            await ui.key('Enter')
            await self.modal(ui, 'Agent actions: ag_one')
            await ui.key('Escape')
            await ui.key('Escape')
            self.assertEqual(ui.focused_control, 'composer')
            ui.view.focus_named('agent_actions')
            await ui.key('Escape')
            self.assertEqual(ui.focused_control, 'composer')

    async def modal(self, ui, text):
        await ui.wait_until(lambda: ui.dialogs.future is not None and text in ui.screen_text())

    async def test_archived_decline_returns_to_mandatory_navigation(self):
        self.assertTrue(hasattr(cli_tui_dialogs, 'TuiWorkflows'), 'guided workflows missing')
        async with workflow_harness(rows=[workspace('ws_archived', 'Billing', archived=True)]) as ui:
            task = ui.start(ui.workflows.navigate(mandatory=True))
            await self.modal(ui, 'Show archived')
            self.assertIn('New session', ui.screen_text())
            ui.api.action.assert_not_called()
            await ui.activate_named('show_archived')
            await ui.wait_until(lambda: 'Billing' in ui.screen_text())
            await ui.select_row('ws_archived')
            await self.modal(ui, 'Unarchive it? [y/N]')
            await ui._send('n')
            await self.modal(ui, 'Show archived')
            self.assertFalse(task.done())
            self.assertIsNone(ui.controller.workspace)
            self.assertEqual(ui.state.selected_session_id, 'ws_archived')
            self.assertEqual(ui.state.notices.lines.count('Archived session was not selected'), 1)
            ui.api.action.assert_not_called()
            await ui.key('Escape')
            self.assertEqual((await task).status, 'cancelled')
            self.assertIn(('quit',), ui.calls)

    async def test_help_during_failed_mutation_closes_before_retry_form(self):
        import threading
        async with workflow_harness(selected=workspace()) as ui:
            entered, release = threading.Event(), threading.Event()
            def rename(ident, name):
                entered.set()
                release.wait(2)
                raise CLIError('rename failed exactly')
            ui.api.rename.side_effect = rename
            task = ui.start(ui.workflows.run_action('rename_session'))
            await self.modal(ui, 'Rename session')
            await ui.key('Enter')
            try:
                await ui.wait_until(entered.is_set)
                await ui.key('F1')
                self.assertTrue(ui.view.help_visible)
                release.set()
                await ui.wait_until(lambda: ui.dialogs.future is not None)
                await ui.wait_render()
                self.assertFalse(ui.view.help_visible)
                self.assertIn('rename failed exactly', ui.screen_text())
                self.assertEqual(ui.api.rename.call_count, 1)
                await ui.key('Escape')
                self.assertEqual((await task).status, 'cancelled')
                self.assertEqual(ui.api.rename.call_count, 1)
            finally:
                release.set()

    async def test_hide_help_keeps_newer_open_modal_focus(self):
        async with workflow_harness(selected=workspace()) as ui:
            await ui.key('F1')
            task = ui.start(ui.dialogs.form('Later form', [cli_tui_dialogs.Field('name', 'Name:')],
                                           submit_label='Save'))
            await ui.wait_until(lambda: ui.dialogs.future is not None)
            modal_focus = ui.application.layout.current_control
            ui.view.hide_help()
            self.assertIs(ui.application.layout.current_control, modal_focus)
            await ui.key('Escape')
            self.assertTrue((await task).cancelled)

    async def test_failed_selection_returns_to_navigation_with_search(self):
        async with workflow_harness(rows=[workspace('ws_target', 'Target')]) as ui:
            ui.api.get.side_effect = CLIError('cannot select target')
            task = ui.start(ui.workflows.navigate(mandatory=True))
            await self.modal(ui, 'Show archived')
            await ui.select_row('ws_target')
            await self.modal(ui, 'Show archived')
            self.assertFalse(task.done())
            self.assertEqual(ui.state.search, 'WS_TARGET')
            self.assertEqual(ui.state.selected_session_id, 'ws_target')
            self.assertEqual(ui.state.notices.lines.count('cannot select target'), 1)
            await ui.key('Escape')
            await task

    async def test_navigation_more_actions_targets_named_active_session(self):
        async with workflow_harness(selected=workspace(), rows=[workspace('ws_other', 'Other')]) as ui:
            task = ui.start(ui.workflows.navigate())
            await self.modal(ui, 'Show archived')
            await ui.paste('ws_other')
            await ui.activate_named('more_actions')
            await self.modal(ui, 'Session actions: One')
            self.assertIn('Rename session', ui.screen_text())
            self.assertIn('Archive session', ui.screen_text())
            await ui.paste('Rename session')
            await ui.key('Enter')
            await self.modal(ui, 'Rename session')
            await ui._send('\x01\x0b')
            await ui.paste('Renamed active')
            await ui.key('Enter')
            await self.modal(ui, 'Show archived')
            ui.api.rename.assert_called_once_with('ws_one', 'Renamed active')
            self.assertEqual(ui.controller.workspace['id'], 'ws_one')
            ui.api.action.assert_not_called()
            await ui.key('Escape')
            await task

    async def test_f3_can_choose_another_agent_after_previous_selection(self):
        for size in ((120, 35), (90, 24)):
            with self.subTest(size=size):
                async with workflow_harness(selected=workspace(agents=[agent(), agent('ag_two')]),
                                            size=size) as ui:
                    ui.view.composer.text = 'draft stays'
                    ui.state.selected_agent_id = 'ag_one'
                    for ident in ('ag_two', 'ag_one'):
                        await ui.key('F3')
                        await self.modal(ui, 'Choose agent')
                        await ui.type_text(ident)
                        await ui.key('Enter')
                        await self.modal(ui, 'Agent actions: ' + ident)
                        self.assertEqual(ui.state.selected_agent_id, ident)
                        await ui.type_text('Inspect agent')
                        await ui.key('Enter')
                        await ui.wait_until(lambda: ui.view.inspecting)
                        await ui.key('Escape')
                        self.assertEqual(ui.focused_control, 'composer')
                    # Cancelling the picker preserves the current selection.
                    await ui.key('F3')
                    await self.modal(ui, 'Choose agent')
                    await ui.key('Escape')
                    self.assertEqual(ui.state.selected_agent_id, 'ag_one')
                    self.assertEqual(ui.view.composer.text, 'draft stays')
                    ui.api.action.assert_not_called()
                    # Explicit row actions continue targeting the highlighted agent.
                    ui.view.focus_named('agents')
                    await ui.key('Enter')
                    await self.modal(ui, 'Agent actions: ag_one')
                    await ui.key('Escape')

    async def test_agent_actions_menu_real_f3_enter_and_mouse(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            ui.state.selected_agent_id = 'ag_one'
            for open_menu in ('F3', 'Enter', 'mouse'):
                if open_menu == 'F3':
                    await ui.key('F3')
                    await self.modal(ui, 'Choose agent')
                    await ui.key('Enter')
                elif open_menu == 'Enter':
                    ui.view.focus_named('agents')
                    await ui.key('Enter')
                else:
                    await ui.wait_render()
                    info = ui.view.agent_actions.window.render_info
                    self.assertIn('Actions', ui.rows[info._y_offset])
                    await ui.click(info._x_offset + 5, info._y_offset)
                await self.modal(ui, 'Agent actions: ag_one')
                for label in ('Resume agent', 'Stop agent', 'Attach', 'Unread', 'Retry unread',
                              'History settings', 'Inspect agent'):
                    self.assertIn(label, ui.screen_text())
                await ui.key('Escape')
                await self.modal(ui, 'Choose agent')
                await ui.key('Escape')
            await ui.key('F3')
            await self.modal(ui, 'Choose agent')
            await ui.key('Enter')
            await self.modal(ui, 'Agent actions: ag_one')
            await ui.paste('History settings')
            await ui.key('Enter')
            await self.modal(ui, 'History settings')
            await ui.key('Enter')
            await ui.wait_until(lambda: ui.api.action.call_count == 1)
            ui.api.action.assert_called_once_with('ws_one', 'history', 'ag_one', body={'mode': 'literal'})

    async def test_archive_confirmation_explains_checkpoint_and_stop(self):
        async with workflow_harness(selected=workspace(agents=[agent(state='running')])) as ui:
            task = ui.start(ui.workflows.run_action('archive_session'))
            await self.modal(ui, 'Archive session? [y/N]')
            self.assertIn('checkpointed and stopped', ui.screen_text())
            self.assertIn('1 running agent', ui.screen_text())
            await ui.key('Escape')
            await task
            ui.api.action.assert_not_called()

    async def test_stale_agent_form_no_mutation(self):
        async with workflow_harness(selected=workspace(agents=[agent()]),
                                    rows=[workspace('ws_other', agents=[agent()])]) as ui:
            task = ui.start(ui.workflows.agent_form('resume', 'ag_one'))
            await self.modal(ui, 'Resume agent')
            await ui.controller.select_session('ws_other')
            ui.api.reset_mock()
            await ui.key('Enter')
            self.assertEqual((await task).status, 'cancelled')
            self.assertEqual(ui.api.mock_calls, [])
            self.assertIn('Selection changed', ui.state.notices.lines[-1])

    async def test_stale_stop_confirmation_no_mutation(self):
        async with workflow_harness(selected=workspace(agents=[agent(state='running')]),
                                    rows=[workspace('ws_other', agents=[agent(state='running')])]) as ui:
            task = ui.start(ui.workflows.run_action('stop', target_id='ag_one'))
            await self.modal(ui, 'Stop ag_one?')
            await ui.controller.select_session('ws_other')
            ui.api.reset_mock()
            await ui._send('y')
            self.assertEqual((await task).status, 'cancelled')
            self.assertEqual(ui.api.mock_calls, [])
            self.assertIn('Selection changed', ui.state.notices.lines[-1])

    async def test_stale_palette_no_mutation(self):
        async with workflow_harness(selected=workspace(agents=[agent()]),
                                    rows=[workspace('ws_other', agents=[agent()])]) as ui:
            ui.state.selected_agent_id = 'ag_one'
            task = ui.start(ui.workflows.show_palette())
            await self.modal(ui, 'Commands')
            await ui.controller.select_session('ws_other')
            ui.api.reset_mock()
            await ui.paste('New session')
            await ui.key('Enter')
            self.assertTrue(task.done(), 'Stale palette must not open another form')
            self.assertEqual((await task).status, 'cancelled')
            self.assertEqual(ui.api.mock_calls, [])
            self.assertIn('Selection changed', ui.state.notices.lines[-1])

    async def test_wide_help_blocks_f2_through_f5(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            await ui.key('F1')
            self.assertTrue(ui.view.help_visible)
            ui.api.reset_mock()
            for key in ('F2', 'F3', 'F4', 'F5'):
                await ui.key(key)
                self.assertTrue(ui.view.help_visible)
                self.assertIsNone(ui.dialogs.future)
                self.assertFalse(ui.view.activity_visible)
                self.assertIs(ui.application.layout.current_control, ui.view.help)
                self.assertEqual(ui.api.mock_calls, [])
            await ui.key('F1')

    async def test_removed_agent_while_stop_chooser_open_cancels(self):
        async with workflow_harness(selected=workspace(agents=[agent(state='running')])) as ui:
            task = ui.start(ui.workflows.run_action('stop'))
            await self.modal(ui, 'Choose agent')
            ui.controller.workspace['agents'] = []
            ui.api.reset_mock()
            await ui.key('Enter')
            self.assertEqual((await task).status, 'cancelled')
            self.assertIn('Selection changed', ui.state.notices.lines[-1])
            self.assertEqual(ui.api.mock_calls, [])

    async def test_plain_hash_channel_admission_uses_normalized_identity(self):
        async with workflow_harness(plain=True) as ui:
            ui.state.drafts.set(('channel', 'other'), 'existing')
            for i in range(49):
                ui.state.drafts.set(('channel', str(i)), 'saved')
            task = ui.start(ui.workflows.run_action('create_channel'))
            await self.modal(ui, 'Create channel')
            await ui.paste('#other')
            await ui.key('Enter')
            self.assertEqual((await task).status, 'completed')
            self.assertEqual(ui.composer_actions.key, ('channel', 'other'))
            self.assertEqual(ui.view.composer.text, 'existing')

    async def test_malformed_list_response_retains_rows_and_marks_stale(self):
        async with workflow_harness(rows=[workspace()]) as ui:
            ui.api.list.side_effect = None
            ui.api.list.return_value = {'warning': 'missing rows'}
            result = await ui.workflows.refresh_sessions()
            self.assertEqual(result.status, 'failed')
            self.assertTrue(ui.view.sessions_stale)
            self.assertEqual(ui.view.session_rows()[0]['id'], 'ws_one')
            self.assertEqual(len(ui.state.notices.lines), 1)

    async def test_plain_quit_description_has_no_checkpoint(self):
        async with workflow_harness(plain=True) as ui:
            choice = next(c for c in ui.view.action_choices() if c['id'] == 'quit')
            self.assertEqual(choice['description'], 'Disconnect')

    async def test_empty_agent_chooser_can_add_and_composer_clear_is_visible(self):
        async with workflow_harness(selected=workspace()) as ui:
            await ui.key('F3')
            await self.modal(ui, 'Choose agent')
            self.assertIn('Add agent', ui.screen_text())
            title_y = next(y for y, row in enumerate(ui.rows) if 'Choose agent' in row)
            y, row = next((y, row) for y, row in enumerate(ui.rows)
                          if y > title_y and '<Add agent >' in row)
            await ui.click(row.index('<Add agent >') + 1, y)
            await self.modal(ui, 'New agent')
            await ui.key('Escape')
            await ui.paste('clear me')
            await ui.activate_named('clear_draft')
            await self.modal(ui, 'Clear draft?')
            await ui._send('y')
            await ui.wait_until(lambda: ui.view.composer.text == '')

    async def test_plain_channel_menu_exposes_create_switch_history(self):
        async with workflow_harness(plain=True) as ui:
            await ui.key('F2')
            await self.modal(ui, 'Channel actions')
            for label in ('Create channel', 'Switch channel', 'History'):
                self.assertIn(label, ui.screen_text())
            await ui.key('Escape')

    async def test_navigation_no_active_session_disables_more_actions(self):
        async with workflow_harness(rows=[workspace()]) as ui:
            task = ui.start(ui.workflows.navigate(mandatory=True))
            await self.modal(ui, 'Show archived')
            self.assertIn('More actions: Select a session first', ui.screen_text())
            await ui.activate_named('more_actions')
            self.assertFalse(task.done())
            self.assertIn('Show archived', ui.screen_text())
            await ui.key('Escape')
            await task

    async def test_loading_buttons_explain_wait_and_archived_toggle_state(self):
        import threading
        async with workflow_harness(rows=[workspace()]) as ui:
            entered, release = threading.Event(), threading.Event()
            original = ui.api.list.side_effect
            def listing(*, include_archived=False):
                entered.set()
                release.wait(2)
                return original(include_archived=include_archived)
            ui.api.list.side_effect = listing
            task = ui.start(ui.workflows.navigate())
            try:
                await self.modal(ui, 'Loading sessions')
                self.assertIn('Show archived [off]', ui.screen_text())
                await ui.activate_named('show_archived')
                self.assertIn('Wait before changing Show archived', ui.state.notices.lines[-1])
                await ui.activate_named('refresh')
                self.assertIn('Refresh is already in progress', ui.state.notices.lines[-1])
                release.set()
                await ui.wait_until(lambda: not ui.view._sessions_loading)
                await ui.activate_named('show_archived')
                await ui.wait_until(lambda: 'Show archived [on]' in ui.screen_text())
                await ui.key('Escape')
                await task
            finally:
                release.set()

    async def test_committed_plain_channel_change_force_binds_full_destination(self):
        async with workflow_harness(plain=True) as ui:
            for i in range(49):
                ui.state.drafts.set(('channel', str(i)), 'saved')
            original = ui.client.submit_outcome
            entered, release = asyncio.Event(), asyncio.Event()
            async def submit(text):
                entered.set()
                await release.wait()
                return await original(text)
            ui.client.submit_outcome = submit
            task = ui.start(ui.workflows.run_action('create_channel'))
            await self.modal(ui, 'Create channel')
            await ui.paste('other')
            await ui.key('Enter')
            await ui.wait_until(entered.is_set)
            await ui.paste('fiftieth')
            release.set()
            self.assertEqual((await task).status, 'completed')
            self.assertEqual(ui.composer_actions.key, ('channel', 'other'))
            self.assertEqual(ui.state.drafts.get(('channel', 'general')), 'fiftieth')
            self.assertEqual(len(ui.state.drafts), 50)

    async def test_exact_form_labels_and_archived_context(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            for action, expected in [('resume', 'Working directory (read-only):'),
                                     ('history', 'History mode:')]:
                task = ui.start(ui.workflows.agent_form(action, 'ag_one'))
                await self.modal(ui, expected)
                if action == 'resume':
                    self.assertIn('Resume automatically uses the saved working directory.', ui.screen_text())
                await ui.key('Escape')
                await task
            task = ui.start(ui.workflows.confirm_selection('Unarchive it? [y/N]',
                workspace=workspace('ws_archived', archived=True)))
            await self.modal(ui, 'Unarchive it? [y/N]')
            self.assertLess(ui.screen_text().index('(archived)'), ui.screen_text().index('Unarchive it?'))
            await ui.key('Escape')
            await task

    async def test_clear_draft_mouse_control_keeps_composer_full_width(self):
        async with workflow_harness(selected=workspace(), size=(80,18)) as ui:
            await ui.paste('mouse draft')
            self.assertGreaterEqual(ui.view.composer.window.render_info.window_width, 75)
            self.assertGreaterEqual(ui.view.conversation_window.render_info.window_height, 8)
            for y, row in enumerate(ui.rows):
                if 'Clear draft' in row:
                    await ui.click(row.index('Clear draft'), y)
                    break
            else:
                self.fail('Missing visible Clear draft')
            await self.modal(ui, 'Clear draft?')
            await ui._send('n')
            self.assertEqual(ui.view.composer.text, 'mouse draft')

    async def test_archive_from_more_actions_finishes_after_mandatory_selection(self):
        async with workflow_harness(selected=workspace()) as ui:
            task = ui.start(ui.workflows.navigate())
            await self.modal(ui, 'Show archived')
            await ui.activate_named('more_actions')
            await self.modal(ui, 'Session actions: One')
            await ui.paste('Archive session')
            await ui.key('Enter')
            await self.modal(ui, 'Archive session?')
            await ui._send('y')
            await self.modal(ui, 'Show archived')
            await ui.activate_named('new_session')
            await self.modal(ui, 'Session name:')
            await ui.key('Enter')
            await self.modal(ui, 'Orchestrator provider:')
            await ui.focus_field('cwd')
            await ui.key('Enter')
            await ui.wait_until(lambda: (ui.controller.workspace or {}).get('id') == 'ws_created')
            await ui.wait_render()
            self.assertTrue(task.done(), 'Completed mandatory selection must not reopen outer navigation')
            self.assertEqual((await task).status, 'completed')
            self.assertIsNone(ui.dialogs.future)

    async def test_busy_selection_returns_to_navigation_without_quit(self):
        import threading
        async with workflow_harness(rows=[workspace('ws_first'), workspace('ws_second')]) as ui:
            entered, release = threading.Event(), threading.Event()
            original = ui.api.get.side_effect
            def get(ident):
                entered.set()
                release.wait(2)
                return original(ident)
            ui.api.get.side_effect = get
            selecting = ui.start(ui.controller.select_session('ws_first'))
            await ui.wait_until(entered.is_set)
            task = ui.start(ui.workflows.navigate(mandatory=True))
            try:
                await self.modal(ui, 'Show archived')
                await ui.select_row('ws_second')
                await self.modal(ui, 'Show archived')
                self.assertFalse(task.done())
                self.assertNotIn(('quit',), ui.calls)
                self.assertEqual(ui.state.selected_session_id, 'ws_second')
                self.assertEqual(ui.state.notices.lines.count('Session selection already in progress.'), 1)
                release.set()
                await selecting
                await ui.key('Escape')
                await task
            finally:
                release.set()

    async def test_busy_retry_waits_for_palette_without_requesting_quit(self):
        import threading
        async with workflow_harness(rows=[workspace('ws_target')]) as ui:
            entered, release = threading.Event(), threading.Event()
            def get(ident):
                entered.set()
                release.wait(2)
                raise CLIError('selection failed exactly')
            ui.api.get.side_effect = get
            task = ui.start(ui.workflows.navigate(mandatory=True))
            await self.modal(ui, 'Show archived')
            await ui.select_row('ws_target')
            try:
                await ui.wait_until(entered.is_set)
                await ui.key('F4')
                await self.modal(ui, 'Commands')
                future = ui.dialogs.future
                release.set()
                await ui.wait_until(lambda: not ui.controller.selection_pending)
                await ui.wait_render()
                self.assertNotIn(('quit',), ui.calls)
                self.assertFalse(task.done())
                self.assertIs(ui.dialogs.future, future)
                self.assertFalse(future.cancelled())
                await ui.key('Escape')
                await self.modal(ui, 'Show archived')
                self.assertNotIn(('quit',), ui.calls)
                ui.api.get.assert_called_once_with('ws_target')
                await ui.key('Escape')
                self.assertEqual((await task).status, 'cancelled')
                self.assertEqual(ui.calls.count(('quit',)), 1)
            finally:
                release.set()

    async def test_postarchive_busy_entry_waits_then_opens_mandatory_navigation(self):
        import threading
        async with workflow_harness(selected=workspace()) as ui:
            entered, release = threading.Event(), threading.Event()
            original = ui.api.action.side_effect
            def action(*args, **kwargs):
                if args[1] == 'archive':
                    entered.set()
                    release.wait(2)
                return original(*args, **kwargs)
            ui.api.action.side_effect = action
            task = ui.start(ui.workflows.run_action('archive_session'))
            await self.modal(ui, 'Archive session?')
            await ui._send('y')
            try:
                await ui.wait_until(entered.is_set)
                await ui.key('F4')
                await self.modal(ui, 'Commands')
                future = ui.dialogs.future
                release.set()
                await ui.wait_until(lambda: ui.controller.workspace is None)
                await ui.wait_render()
                self.assertFalse(task.done(), 'Mandatory navigation must wait for the existing dialog')
                self.assertIs(ui.dialogs.future, future)
                self.assertNotIn(('quit',), ui.calls)
                await ui.key('Escape')
                await self.modal(ui, 'Show archived')
                self.assertNotIn(('quit',), ui.calls)
                await ui.key('Escape')
                await task
                self.assertEqual(ui.calls.count(('quit',)), 1)
            finally:
                release.set()

    async def test_cancel_waiting_navigation_does_not_cancel_other_modal_waiter(self):
        async with workflow_harness() as ui:
            owner = ui.start(ui.dialogs.form('Existing form', [cli_tui_dialogs.Field('name', 'Name:')],
                                            submit_label='Save'))
            await self.modal(ui, 'Existing form')
            future = ui.dialogs.future
            navigation = ui.start(ui.workflows.navigate(mandatory=True))
            await ui.wait_render()
            self.assertFalse(navigation.done())
            navigation.cancel()
            await asyncio.gather(navigation, return_exceptions=True)
            self.assertFalse(future.cancelled())
            self.assertFalse(owner.done())
            self.assertIs(ui.dialogs.future, future)
            await ui.key('Escape')
            self.assertTrue((await owner).cancelled)
            self.assertNotIn(('quit',), ui.calls)

    async def _competing_navigation_commit(self, *, after_archive, delay_commit):
        import threading
        selected = workspace() if after_archive else None
        async with workflow_harness(selected=selected,
                                    rows=[workspace('ws_fail'), workspace('ws_ok')]) as ui:
            entered, release = threading.Event(), threading.Event()
            competing, commit_release = threading.Event(), threading.Event()
            get = ui.api.get.side_effect
            action = ui.api.action.side_effect
            def delayed_get(ident):
                if ident == 'ws_fail':
                    entered.set()
                    release.wait(3)
                    raise CLIError('selection failed exactly')
                if ident == 'ws_ok' and delay_commit:
                    competing.set()
                    commit_release.wait(3)
                return get(ident)
            def delayed_action(*args, **kwargs):
                if args[1] == 'archive':
                    entered.set()
                    release.wait(3)
                return action(*args, **kwargs)
            ui.api.get.side_effect = delayed_get
            ui.api.action.side_effect = delayed_action
            if after_archive:
                task = ui.start(ui.workflows.run_action('archive_session'))
                await self.modal(ui, 'Archive session?')
                await ui._send('y')
            else:
                task = ui.start(ui.workflows.navigate(mandatory=True))
                await self.modal(ui, 'Show archived')
                await ui.select_row('ws_fail')
            try:
                await ui.wait_until(entered.is_set)
                await ui.key('F2')
                await self.modal(ui, 'Show archived')
                release.set()
                if after_archive:
                    await ui.wait_until(lambda: ui.controller.workspace is None)
                else:
                    await ui.wait_until(lambda: not ui.controller.selection_pending)
                await ui.wait_render()
                self.assertFalse(task.done())
                await ui.select_row('ws_ok')
                if delay_commit:
                    await ui.wait_until(competing.is_set)
                    await ui.wait_render()
                    self.assertIsNone(ui.dialogs.future,
                                      'Older navigation must not reopen during the competing candidate read')
                    self.assertFalse(task.done())
                    commit_release.set()
                await ui.wait_until(lambda: (ui.controller.workspace or {}).get('id') == 'ws_ok')
                await ui.wait_render()
                self.assertIsNone(ui.dialogs.future, 'Committed competing navigation must satisfy the older request')
                self.assertTrue(task.done())
                self.assertEqual((await task).status, 'completed')
                await ui.key('Escape')
                self.assertNotIn(('quit',), ui.calls)
                self.assertEqual(ui.controller.workspace['id'], 'ws_ok')
            finally:
                release.set()
                commit_release.set()

    async def test_failed_mandatory_navigation_accepts_competing_f2_commit(self):
        for delayed in (False, True):
            with self.subTest(delayed=delayed):
                await self._competing_navigation_commit(after_archive=False, delay_commit=delayed)

    async def test_postarchive_navigation_accepts_competing_f2_commit(self):
        for delayed in (False, True):
            with self.subTest(delayed=delayed):
                await self._competing_navigation_commit(after_archive=True, delay_commit=delayed)

    async def test_cancel_other_modal_owner_keeps_mandatory_navigation_alive(self):
        async with workflow_harness() as ui:
            owner = ui.start(ui.dialogs.form('Owned form', [cli_tui_dialogs.Field('name', 'Name:')],
                                            submit_label='Save'))
            await self.modal(ui, 'Owned form')
            navigation = ui.start(ui.workflows.navigate(mandatory=True))
            await ui.wait_render()
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
            await ui.wait_render()
            self.assertFalse(navigation.done(), 'Cancelling the modal owner must not cancel waiting navigation')
            await self.modal(ui, 'Show archived')
            self.assertNotIn(('quit',), ui.calls)
            await ui.key('Escape')
            self.assertEqual((await navigation).status, 'cancelled')
            self.assertEqual(ui.calls.count(('quit',)), 1)

    async def test_busy_confirmation_waits_through_competing_selection_commit(self):
        import threading
        async with workflow_harness(selected=workspace(), no_resume=False,
                                    rows=[workspace('ws_ok', agents=[agent()])]) as ui:
            entered, release = threading.Event(), threading.Event()
            action = ui.api.action.side_effect
            def delayed_checkpoint(*args, **kwargs):
                if args[1] == 'checkpoint':
                    entered.set()
                    release.wait(3)
                return action(*args, **kwargs)
            ui.api.action.side_effect = delayed_checkpoint
            selecting = ui.start(ui.controller.select_session('ws_ok'))
            await self.modal(ui, 'Resume 1 stopped agents?')
            navigation = ui.start(ui.workflows.navigate(mandatory=True))
            try:
                await ui.wait_render()
                await ui._send('n')
                await ui.wait_until(entered.is_set)
                await ui.wait_render()
                self.assertFalse(navigation.done())
                self.assertIsNone(ui.dialogs.future,
                                  'Preparation completion must not reopen navigation before checkpoint finishes')
                release.set()
                self.assertEqual((await selecting).status, 'completed')
                self.assertEqual((await asyncio.wait_for(navigation, 2)).status, 'completed')
                self.assertIsNone(ui.dialogs.future)
                await ui.key('Escape')
                self.assertNotIn(('quit',), ui.calls)
            finally:
                release.set()

    async def test_busy_navigation_does_not_wait_for_long_lived_modal_owner(self):
        async with workflow_harness() as ui:
            ui.dialogs.owner_is_short_lived = lambda owner: False
            modal_closed, lifetime_end = asyncio.Event(), asyncio.Event()
            async def lifetime():
                await ui.dialogs.form('Lifetime form', [cli_tui_dialogs.Field('name', 'Name:')],
                                      submit_label='Save')
                modal_closed.set()
                await lifetime_end.wait()
            owner = ui.start(lifetime())
            await self.modal(ui, 'Lifetime form')
            navigation = ui.start(ui.workflows.navigate(mandatory=True))
            try:
                await ui.wait_render()
                await ui.key('Escape')
                await asyncio.wait_for(modal_closed.wait(), 2)
                await ui.wait_render()
                self.assertFalse(owner.done())
                self.assertIsNotNone(ui.dialogs.future,
                                     'Mandatory navigation must not wait for an application-lifetime owner')
                await self.modal(ui, 'Show archived')
                self.assertNotIn(('quit',), ui.calls)
                await ui.key('Escape')
                self.assertEqual((await navigation).status, 'cancelled')
            finally:
                lifetime_end.set()

    async def test_busy_navigation_rechecks_commit_before_waiting_modal_owner(self):
        async with workflow_harness(rows=[workspace('ws_ok')]) as ui:
            release = asyncio.Event()
            async def workflow():
                await ui.dialogs.form('Other action', [cli_tui_dialogs.Field('name', 'Name:')],
                                      submit_label='Save')
                await release.wait()
            owner = ui.start(workflow())
            await self.modal(ui, 'Other action')
            navigation = ui.start(ui.workflows.navigate(mandatory=True))
            try:
                await ui.wait_render()
                self.assertEqual((await ui.controller.select_session('ws_ok')).status, 'completed')
                await ui.key('Escape')
                await ui.wait_render()
                self.assertFalse(owner.done())
                self.assertTrue(navigation.done(), 'A committed selection satisfies navigation before further waits')
                self.assertEqual((await navigation).status, 'completed')
                self.assertIsNone(ui.dialogs.future)
                self.assertNotIn(('quit',), ui.calls)
            finally:
                release.set()

    async def test_stale_agent_highlight_still_opens_add_agent_actions(self):
        for agents in ([agent('ag_remaining')], []):
            with self.subTest(remaining=len(agents)):
                async with workflow_harness(selected=workspace(agents=agents)) as ui:
                    ui.state.selected_agent_id = 'ag_gone'
                    ui.view.focus_named('agent_actions')
                    await ui.key('Enter')
                    await self.modal(ui, 'Agent actions')
                    self.assertIsNone(ui.state.selected_agent_id)
                    self.assertEqual(ui.state.notices.lines.count('Selected agent is no longer available.'), 1)
                    self.assertIn('Add agent', ui.screen_text())
                    await ui.paste('Add agent')
                    await ui.key('Enter')
                    await self.modal(ui, 'New agent')
                    ui.api.action.assert_not_called()
                    await ui.key('Escape')

    async def test_first_escape_requests_quit(self):
        async with workflow_harness() as ui:
            task = ui.start(ui.workflows.navigate(mandatory=True))
            await self.modal(ui, 'New session')
            await ui.key('Escape')
            self.assertEqual((await task).status, 'cancelled')
            self.assertIn(('quit',), ui.calls)

    async def test_new_blank_session_creates_then_selects(self):
        async with workflow_harness(selected=workspace()) as ui:
            await ui.paste('unsent')
            task = ui.start(ui.workflows.new_session())
            await self.modal(ui, 'New session')
            await ui.key('Enter')
            await self.modal(ui, 'Orchestrator provider:')
            await ui.focus_field('cwd')
            await ui.key('Enter')
            self.assertEqual((await task).status, 'completed')
            ui.api.create.assert_called_once_with('', orchestrator={
                'provider': 'codex', 'cwd': str(Path.cwd())})
            self.assertEqual(ui.controller.workspace['id'], 'ws_created')
            self.assertEqual(ui.composer_actions.key, ('session', 'ws_created'))
            self.assertEqual(ui.state.drafts.get(('session', 'ws_one')), 'unsent')
            self.assertLess(ui.api.mock_calls.index(call.create('', orchestrator={
                'provider': 'codex', 'cwd': str(Path.cwd())})), ui.api.mock_calls.index(call.get('ws_created')))

    async def test_navigation_search_stable_ids_refresh_and_mouse(self):
        async with workflow_harness(rows=[workspace('ws_alpha', 'Same'), workspace('ws_beta', 'Same')]) as ui:
            reads = ui.api.list.call_count
            task = ui.start(ui.workflows.navigate())
            await self.modal(ui, 'Show archived')
            await ui.wait_until(lambda: ui.api.list.call_count > reads and not ui.view._sessions_loading)
            await ui.select_row('ws_beta')
            self.assertEqual((await asyncio.wait_for(task, 2)).workspace_id, 'ws_beta')
            reads = ui.api.list.call_count
            task = ui.start(ui.workflows.navigate())
            await self.modal(ui, 'Show archived')
            await ui.wait_until(lambda: ui.api.list.call_count > reads and not ui.view._sessions_loading)
            await ui._send('\x01\x0b')
            reads = ui.api.list.call_count
            await ui.activate_named('refresh')
            # The dialog remains open while Refresh runs; rows reject clicks
            # until loading finishes. Wait for that request before clicking.
            await ui.wait_until(lambda: ui.api.list.call_count > reads and not ui.view._sessions_loading)
            self.assertEqual(ui.state.selected_session_id, 'ws_beta')
            # Mouse handler belongs to actual rendered navigation rows.
            for y, row in enumerate(ui.rows):
                if 'ws_alpha' in row and 'Same' in row:
                    await ui.click(row.index('Same'), y)
                    break
            else:
                self.fail('full stable ID not visible')
            self.assertEqual((await asyncio.wait_for(task, 2)).workspace_id, 'ws_alpha')

    async def test_capacity_refuses_switch_and_create_before_mutation(self):
        async with workflow_harness(selected=workspace(), rows=[workspace('ws_other')]) as ui:
            await ui.paste('keep')
            for i in range(49):
                ui.state.drafts.set(('session', str(i)), 'saved')
            before = (ui.view.composer.text, ui.view.composer.buffer.cursor_position)
            result = await ui.workflows.run_action('select_session', target_id='ws_other')
            self.assertEqual(result.status, 'cancelled')
            self.assertEqual((ui.view.composer.text, ui.view.composer.buffer.cursor_position), before)
            self.assertEqual((await ui.workflows.new_session()).status, 'cancelled')
            ui.api.create.assert_not_called()
            ui.api.get.assert_not_called()
            self.assertEqual(ui.state.notices.lines[-1], '50 unsent drafts; send or clear one')

    async def test_rename_form_selection_aba_cancels_no_mutation(self):
        async with workflow_harness(selected=workspace(), rows=[workspace('ws_other')]) as ui:
            task = ui.start(ui.workflows.run_action('rename_session'))
            await self.modal(ui, 'Rename session')
            await ui.controller.select_session('ws_other')
            await ui.controller.select_session('ws_one')
            ui.api.reset_mock()
            await ui.paste('new')
            await ui.key('Enter')
            self.assertEqual((await task).status, 'cancelled')
            self.assertEqual(ui.api.mock_calls, [])
            self.assertIn('Selection changed', ui.state.notices.lines[-1])

    async def test_spawn_form_defaults_and_failed_retry_retains_values(self):
        with tempfile.TemporaryDirectory() as cwd:
            async with workflow_harness(selected=workspace(agents=[agent(cwd=cwd)])) as ui:
                action = ui.api.action.side_effect
                ui.api.action.side_effect = CLIError('exact launch refusal')
                task = ui.start(ui.workflows.agent_form('spawn'))
                await self.modal(ui, 'New agent')
                self.assertIn('codex', ui.screen_text())
                await ui.key('Tab')
                self.assertIn('kilo', ui.screen_text())
                await ui.key('ShiftTab')
                self.assertIn(cwd, ui.screen_text())
                await ui.key('Enter')
                await self.modal(ui, 'New agent · 2 of 2')
                await ui.key('Down')
                await ui.key('Down')
                await ui.key('Enter')
                await self.modal(ui, 'exact launch refusal')
                self.assertEqual(ui.api.action.call_count, 1)
                await ui.wait_render()
                self.assertEqual(ui.api.action.call_count, 1)
                self.assertIn(cwd, ui.screen_text())
                ui.api.action.side_effect = action
                await ui.key('Enter')
                await self.modal(ui, 'New agent · 2 of 2')
                await ui.key('Down')
                await ui.key('Down')
                await ui.key('Enter')
                self.assertEqual((await task).status, 'completed')
                self.assertEqual(ui.api.action.call_args.kwargs['body'],
                                 dict(provider='codex', cwd=cwd, history_mode='literal', name=None,
                                      role='generalist', personality='pragmatic'))

    async def test_resume_starts_ordinary_and_fresh_needs_confirmation(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            ui.state.selected_agent_id = 'ag_one'
            ui.api.action.side_effect = CLIError('native missing; use --fresh', status=409)
            task = ui.start(ui.workflows.agent_form('resume', 'ag_one'))
            await self.modal(ui, 'Resume agent')
            await ui.key('Enter')
            await self.modal(ui, 'native missing; use --fresh')
            self.assertFalse(ui.api.action.call_args.kwargs['body']['fresh'])
            await ui.focus_field('launch_mode')
            await ui.key('Tab')
            await ui.key('Enter')
            await self.modal(ui, 'Fresh')
            await ui._send('n')
            await self.modal(ui, 'Resume agent')
            self.assertEqual(ui.api.action.call_count, 1)
            await ui.key('Escape')
            self.assertEqual((await task).status, 'cancelled')

    async def test_resume_refusal_stays_visible_at_minimum_terminal_size(self):
        async with workflow_harness(selected=workspace(agents=[agent()]), size=(80, 18)) as ui:
            ui.api.action.side_effect = CLIError('Saved conversation missing; choose fresh.', status=409)
            task = ui.start(ui.workflows.agent_form('resume', 'ag_one'))
            await self.modal(ui, 'Resume agent')
            await ui.key('Enter')
            await ui.wait_until(lambda: ui.api.action.call_count == 1 and ui.dialogs.future is not None)
            self.assertIn('Saved conversation missing; choose fresh.', ui.screen_text())
            self.assertIn('< Resume agent >', ui.screen_text())
            await ui.key('Escape')
            self.assertEqual((await task).status, 'cancelled')

    async def test_candidate_resume_shows_eligible_and_missing_cwd(self):
        candidate = workspace('ws_candidate', 'Candidate', agents=[agent('ag_eligible'),
            agent('ag_skipped', cwd='/definitely/missing/task9')])
        async with workflow_harness(rows=[candidate], no_resume=False) as ui:
            task = ui.start(ui.workflows.run_action('select_session', target_id='ws_candidate'))
            await self.modal(ui, 'Resume 1 stopped agents? [Y/n]')
            screen = ui.screen_text()
            self.assertIn('ag_eligible', screen)
            self.assertIn('ag_skipped', screen)
            self.assertIn('⚠ cwd missing — /resume ag_skipped --cwd PATH', screen)
            await ui.key('Enter')
            self.assertEqual((await task).status, 'completed')
            ui.api.action.assert_called_once_with('ws_candidate', 'resume', 'ag_eligible', body={})

    async def test_history_requires_explicit_agent_and_submit(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            task = ui.start(ui.workflows.run_action('history'))
            await self.modal(ui, 'Choose agent')
            await ui.key('Enter')
            await self.modal(ui, 'History settings')
            self.assertIn('literal delivered', ui.screen_text())
            ui.api.action.assert_not_called()
            await ui.key('Tab')
            await ui.key('Enter')
            self.assertEqual((await task).status, 'completed')
            ui.api.action.assert_called_once_with('ws_one', 'history', 'ag_one', body={'mode': 'none'})

    async def test_plain_channel_switch_and_create_use_client(self):
        async with workflow_harness(plain=True) as ui:
            task = ui.start(ui.workflows.run_action('switch_channel'))
            await self.modal(ui, 'Channels')
            await ui.paste('other')
            await ui.key('Enter')
            self.assertEqual((await task).status, 'completed')
            self.assertEqual(ui.client.channel, 'other')
            self.assertEqual(ui.composer_actions.key, ('channel', 'other'))
            task = ui.start(ui.workflows.run_action('create_channel'))
            await self.modal(ui, 'Create channel')
            await ui.paste('general')
            await ui.key('Enter')
            self.assertEqual((await task).status, 'completed')
            self.assertEqual(ui.client.channel, 'general')
            self.assertEqual(ui.api.mock_calls, [])

    async def test_archive_releases_draft_and_uses_mandatory_navigation_callback(self):
        async with workflow_harness(selected=workspace()) as ui:
            await ui.paste('keep archived draft')
            for i in range(49):
                ui.state.drafts.set(('session', str(i)), 'saved')
            navigations = []
            async def navigate(mandatory=False):
                navigations.append((mandatory, ui.controller.workspace, ui.composer_actions.key))
                return await ui.workflows.navigate(mandatory)
            ui.callbacks['navigate'] = navigate
            task = ui.start(ui.workflows.run_action('archive_session'))
            await self.modal(ui, 'Archive session? [y/N]')
            self.assertIn('One', ui.screen_text())
            await ui._send('y')
            await self.modal(ui, 'Show archived')
            self.assertEqual(navigations, [(True, None, None)])
            self.assertEqual(ui.state.drafts.get(('session', 'ws_one')), 'keep archived draft')
            await ui.activate_named('new_session')
            await self.modal(ui, 'Session name:')
            await ui.key('Enter')
            await self.modal(ui, 'Orchestrator provider:')
            await ui.focus_field('cwd')
            await ui.key('Enter')
            self.assertEqual((await task).status, 'completed')
            self.assertEqual(ui.controller.workspace['id'], 'ws_created')
            self.assertEqual(ui.composer_actions.key, ('session', 'ws_created'))
            self.assertEqual(len(ui.state.drafts), 50)
            self.assertEqual(ui.api.action.call_args_list, [call('ws_one', 'archive')])

    async def test_cancel_loading_preserves_selection_draft_and_viewport(self):
        import threading
        async with workflow_harness(selected=workspace()) as ui:
            await ui.paste('keep')
            before = (ui.controller.workspace, ui.view.composer.text, ui.state.viewport.anchor_id,
                      ui.state.viewport.follow, ui.composer_actions.key)
            entered, release = threading.Event(), threading.Event()
            def blocked(*, include_archived=False):
                entered.set()
                release.wait(2)
                return {'workspaces': []}
            ui.api.list.side_effect = blocked
            task = ui.start(ui.workflows.navigate())
            try:
                await self.modal(ui, 'Loading sessions')
                await ui.key('Escape')
                self.assertEqual((await task).status, 'cancelled')
                self.assertEqual((ui.controller.workspace, ui.view.composer.text, ui.state.viewport.anchor_id,
                                  ui.state.viewport.follow, ui.composer_actions.key), before)
                ui.api.action.assert_not_called()
            finally:
                release.set()

    async def test_refresh_failure_retains_rows_warning_is_once(self):
        async with workflow_harness(rows=[workspace()]) as ui:
            ui.api.list.side_effect = CLIError('exact list error')
            result = await ui.workflows.refresh_sessions()
            self.assertEqual(result.status, 'failed')
            self.assertEqual(ui.view.session_rows()[0]['id'], 'ws_one')
            self.assertTrue(ui.view.sessions_stale)
            self.assertEqual(ui.state.notices.lines.count('exact list error'), 1)
            ui.api.list.side_effect = None
            ui.api.list.return_value = {'workspaces': [workspace()], 'warning': 'one warning'}
            await ui.workflows.refresh_sessions()
            self.assertFalse(ui.view.sessions_stale)
            self.assertEqual(ui.state.notices.lines.count('one warning'), 1)

    async def test_palette_target_agent_survives_changed_highlight(self):
        async with workflow_harness(selected=workspace(agents=[agent('ag_first'), agent('ag_second')])) as ui:
            ui.state.selected_agent_id = 'ag_first'
            task = ui.start(ui.workflows.show_palette())
            await self.modal(ui, 'Commands')
            ui.state.selected_agent_id = 'ag_second'
            await ui.paste('Resume agent')
            await ui.key('Enter')
            await self.modal(ui, 'Resume agent')
            await ui.key('Enter')
            self.assertEqual((await task).agent_id, 'ag_first')
            self.assertEqual(ui.api.action.call_args.args[2], 'ag_first')

    async def test_sidebar_mouse_selects_captured_session_id(self):
        async with workflow_harness(rows=[workspace('ws_mouse', 'Mouse target')]) as ui:
            await ui.wait_render()
            for y, row in enumerate(ui.rows):
                if 'Mouse target' in row:
                    await ui.click(row.index('Mouse target'), y)
                    break
            await ui.wait_until(lambda: ui.controller.workspace is not None)
            self.assertEqual(ui.controller.workspace['id'], 'ws_mouse')

    async def test_plain_pending_create_settings_and_unavailable_fallback(self):
        async with workflow_harness(plain=True) as ui:
            ui.client.send = AsyncMock(return_value=True)
            task = ui.start(ui.workflows.run_action('create_channel'))
            await self.modal(ui, 'Create channel')
            await ui.paste('created')
            await ui.key('Enter')
            self.assertEqual((await task).status, 'completed')
            self.assertEqual(ui.client.pending_channel, 'created')
            self.assertEqual(ui.client.channel, 'general')
            ui.client.handle_event({'type': 'settings', 'data': {'channels': ['general', 'created']}})
            self.assertEqual(ui.client.channel, 'created')
            self.assertIsNone(ui.client.pending_channel)
            # Task10 observer owns asynchronous draft reconciliation.
            ui.client.handle_event({'type': 'settings', 'data': {'channels': ['general']}})
            self.assertEqual(ui.client.channel, 'general')
            self.assertEqual(ui.api.mock_calls, [])

    async def test_spawn_invalid_directory_cannot_mutate(self):
        async with workflow_harness(selected=workspace(agents=[agent(cwd='relative/path')])) as ui:
            task = ui.start(ui.workflows.agent_form('spawn'))
            await self.modal(ui, 'New agent')
            await ui.key('Enter')
            await self.modal(ui, 'absolute existing directory')
            ui.api.action.assert_not_called()
            await ui.key('Escape')
            await task

    async def test_history_exact_refusal_survives_until_explicit_resubmit(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            ui.state.selected_agent_id = 'ag_one'
            message = 'summary history mode is not available in this version; use literal or none'
            ui.api.action.side_effect = CLIError(message)
            task = ui.start(ui.workflows.run_action('history'))
            await self.modal(ui, 'History settings')
            await ui.key('Enter')
            await self.modal(ui, 'summary history mode')
            self.assertEqual(ui.api.action.call_count, 1)
            self.assertIn(message, '\n'.join(ui.state.notices.lines))
            await ui.key('Escape')
            self.assertEqual((await task).status, 'cancelled')

    async def test_committed_selection_binds_destination_when_capacity_fills_during_read(self):
        import threading
        async with workflow_harness(selected=workspace(), rows=[workspace('ws_other')]) as ui:
            for i in range(49):
                ui.state.drafts.set(('session', str(i)), 'saved')
            entered, release = threading.Event(), threading.Event()
            original = ui.api.get.side_effect
            def get(ident):
                entered.set()
                release.wait(2)
                return original(ident)
            ui.api.get.side_effect = get
            task = ui.start(ui.workflows.run_action('select_session', target_id='ws_other'))
            try:
                await ui.wait_until(entered.is_set)
                await ui.paste('fiftieth draft')
                self.assertEqual(len(ui.state.drafts), 50)
                release.set()
                self.assertEqual((await task).status, 'completed')
                self.assertEqual(ui.composer_actions.key, ('session', 'ws_other'))
                self.assertEqual(ui.state.drafts.get(('session', 'ws_one')), 'fiftieth draft')
                self.assertEqual(ui.view.composer.text, '')
                await ui.paste('refused')
                self.assertEqual(ui.view.composer.text, '')
                self.assertEqual(len(ui.state.drafts), 50)
                self.assertEqual(ui.state.notices.lines.count('50 unsent drafts; send or clear one'), 1)
                self.assertIn('Message · INSERT · 50 unsent drafts; send or clear one',
                              ui.screen_text())
                ui.state.drafts.clear(('session', '0'))
                await ui.paste('now allowed')
                self.assertEqual(ui.view.composer.text, 'now allowed')
                self.assertNotIn('Message · INSERT · 50 unsent', ui.screen_text())
            finally:
                release.set()

    async def test_duplicate_activation_and_pending_palette_do_not_mutate_twice(self):
        async with workflow_harness(selected=workspace(agents=[agent(state='running')])) as ui:
            ui.state.selected_agent_id = 'ag_one'
            task = ui.start(ui.workflows.run_action('stop'))
            await self.modal(ui, 'Stop ag_one?')
            self.assertEqual((await ui.workflows.run_action('stop')).status, 'cancelled')
            await ui._send('y')
            self.assertEqual((await task).status, 'completed')
            ui.api.action.assert_called_once_with('ws_one', 'stop', 'ag_one')
            import threading
            entered, release = threading.Event(), threading.Event()
            ui.records['ws_next'] = workspace('ws_next')
            def get(ident):
                entered.set()
                release.wait(2)
                return copy.deepcopy(ui.records[ident])
            ui.api.get.side_effect = get
            selecting = ui.start(ui.workflows.run_action('select_session', target_id='ws_next'))
            try:
                await ui.wait_until(entered.is_set)
                choices = {c['id']: c for c in ui.view.action_choices()}
                self.assertEqual(choices['rename_session']['disabled_reason'], 'Session selection in progress')
                self.assertEqual(choices['quit']['disabled_reason'], '')
            finally:
                release.set()
                await selecting

    async def test_resume_saved_directory_and_explicit_fresh_payload(self):
        with tempfile.TemporaryDirectory() as cwd:
            async with workflow_harness(selected=workspace(agents=[agent(cwd=cwd)])) as ui:
                task = ui.start(ui.workflows.agent_form('resume', 'ag_one'))
                await self.modal(ui, 'Resume agent')
                await ui.focus_field('launch_mode')
                await ui.key('Tab')
                await ui.key('Enter')
                await self.modal(ui, 'Fresh launch')
                await ui._send('y')
                self.assertEqual((await task).status, 'completed')
                ui.api.action.assert_called_once_with('ws_one', 'resume', 'ag_one',
                    body={'fresh': True, 'cwd': None, 'name': None, 'provider_args': []})

    async def test_small_help_and_modal_help_preserve_focus_values_on_resize(self):
        async with workflow_harness(selected=workspace(), size=(70, 16)) as ui:
            await ui.key('F1')
            await ui.wait_until(lambda: 'INSERT: Enter completes/adds a line' in ui.screen_text())
            await ui.key('F2')
            self.assertIsNone(ui.dialogs.future)
            await ui.key('Escape')
            await ui.resize(120, 35)
            task = ui.start(ui.workflows.run_action('rename_session'))
            await self.modal(ui, 'Rename session')
            await ui.paste('edited')
            focused, future = ui.application.layout.current_control, ui.dialogs.future
            await ui.key('F1')
            await ui.wait_until(lambda: 'INSERT: Enter completes/adds a line' in ui.screen_text())
            self.assertIs(ui.dialogs.future, future)
            await ui.resize(70, 16)
            await ui.resize(120, 35)
            await ui.key('F1')
            self.assertIs(ui.application.layout.current_control, focused)
            self.assertIs(ui.dialogs.future, future)
            self.assertIn('editedOne', ui.screen_text())
            await ui.key('Escape')
            self.assertEqual((await task).status, 'cancelled')
            ui.api.rename.assert_not_called()

    async def test_palette_captured_empty_agent_does_not_use_later_highlight(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            task = ui.start(ui.workflows.show_palette())
            await self.modal(ui, 'Commands')
            ui.state.selected_agent_id = 'ag_one'
            await ui.paste('History settings')
            await ui.key('Enter')
            await self.modal(ui, 'Choose agent')
            ui.api.action.assert_not_called()
            await ui.key('Escape')
            await task

    async def test_minimum_compact_agent_form_all_providers_and_buttons_reachable(self):
        async with workflow_harness(selected=workspace(agents=[agent()]), size=(80, 18)) as ui:
            ui.controller.providers = ['claude', 'codex', 'kilo', 'gemini']
            task = ui.start(ui.workflows.agent_form('spawn'))
            await self.modal(ui, 'New agent')
            self.assertNotIn('Window too small', ui.screen_text())
            await ui.key('Tab')
            await ui.key('Tab')
            await ui.key('Tab')
            self.assertIn('gemini', ui.screen_text())
            await ui.focus_field('cwd')
            self.assertIn('/tmp', ui.screen_text())
            await ui.focus_field('name')
            await ui.paste('compact agent')
            await ui.key('Down')
            await ui.key('Down')
            self.assertIn('Next', ui.screen_text())
            await ui.focus_field('name')
            await ui.key('Down')
            await ui.key('Down')
            await ui.key('Down')
            await ui.key('Enter')
            await self.modal(ui, 'New agent · 2 of 2')
            self.assertNotIn('Window too small', ui.screen_text())
            await ui.key('Down')
            await ui.key('Down')
            self.assertIn('Start agent', ui.screen_text())
            await ui.key('Enter')
            self.assertEqual((await task).status, 'completed')
            self.assertEqual(ui.api.action.call_args.kwargs['body']['provider'], 'gemini')
            self.assertEqual(ui.api.action.call_args.kwargs['body']['name'], 'compact agent')

    async def test_same_session_selection_preserves_viewport_and_agent(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            ui.state.selected_agent_id = 'ag_one'
            ui.client.handle_event({'type': 'message', 'data': {'id': 7, 'timestamp': 1,
                'channel': 'ws_one', 'text': 'saved anchor', 'sender': 'human'}})
            ui.state.viewport.anchor(7, {7: ui.client.messages[7]})
            await ui.paste('saved draft')
            outcome = await ui.workflows.run_action('select_session', target_id='ws_one')
            self.assertEqual(outcome.status, 'completed')
            self.assertFalse(ui.state.viewport.follow)
            self.assertEqual(ui.state.viewport.anchor_id, 7)
            self.assertEqual(ui.state.selected_agent_id, 'ag_one')
            self.assertEqual(ui.view.composer.text, 'saved draft')
            ui.api.action.assert_not_called()

    async def test_late_session_list_cannot_replace_newer_refresh(self):
        import threading
        async with workflow_harness(rows=[workspace()]) as ui:
            entered, release = threading.Event(), threading.Event()
            count = 0
            def listing(*, include_archived=False):
                nonlocal count
                count += 1
                if count == 1:
                    entered.set()
                    release.wait(2)
                    return {'workspaces': [workspace(name='Obsolete')], 'warning': 'obsolete warning'}
                return {'workspaces': [workspace(name='Latest')]}
            ui.api.list.side_effect = listing
            first = ui.start(ui.workflows.refresh_sessions())
            try:
                await ui.wait_until(entered.is_set)
                await ui.workflows.refresh_sessions()
                release.set()
                await first
                self.assertEqual(ui.view.session_rows()[0]['name'], 'Latest')
                self.assertNotIn('obsolete warning', ui.state.notices.lines)
            finally:
                release.set()

    async def test_rename_success_refreshes_active_stable_id_and_keeps_draft(self):
        async with workflow_harness(selected=workspace()) as ui:
            await ui.paste('keep draft')
            task = ui.start(ui.workflows.run_action('rename_session'))
            await self.modal(ui, 'Rename session')
            await ui._send('\x01\x0b')
            await ui.paste('Renamed session')
            await ui.key('Enter')
            self.assertEqual((await task).status, 'completed')
            ui.api.rename.assert_called_once_with('ws_one', 'Renamed session')
            self.assertEqual(ui.controller.workspace['name'], 'Renamed session')
            self.assertEqual(ui.view.session_rows()[0]['name'], 'Renamed session')
            self.assertEqual(ui.view.composer.text, 'keep draft')

    async def test_archive_default_no_and_stop_escape_are_non_mutating(self):
        async with workflow_harness(selected=workspace(agents=[agent(state='running')])) as ui:
            ui.state.selected_agent_id = 'ag_one'
            for action, text, key in [('archive_session', 'Archive session?', 'Enter'),
                                      ('stop', 'Stop ag_one?', 'Escape')]:
                task = ui.start(ui.workflows.run_action(action))
                await self.modal(ui, text)
                await ui.key(key)
                self.assertEqual((await task).status, 'cancelled')
            ui.api.action.assert_not_called()
            self.assertEqual(ui.controller.workspace['id'], 'ws_one')

    async def test_delayed_candidate_confirmation_closes_help_before_taking_focus(self):
        import threading
        candidate = workspace('ws_candidate', agents=[agent()])
        async with workflow_harness(rows=[candidate], no_resume=False) as ui:
            entered, release = threading.Event(), threading.Event()
            def get(ident):
                entered.set()
                release.wait(2)
                return copy.deepcopy(candidate)
            ui.api.get.side_effect = get
            task = ui.start(ui.workflows.run_action('select_session', target_id='ws_candidate'))
            try:
                await ui.wait_until(entered.is_set)
                await ui.key('F1')
                self.assertTrue(ui.view.help_visible)
                release.set()
                await self.modal(ui, 'Resume 1 stopped agents?')
                self.assertFalse(ui.view.help_visible)
                await ui._send('n')
                self.assertEqual((await task).status, 'completed')
                ui.api.action.assert_not_called()
            finally:
                release.set()

    async def test_busy_candidate_confirmation_preserves_help_and_original_dialog(self):
        async with workflow_harness(selected=workspace()) as ui:
            task = ui.start(ui.workflows.run_action('rename_session'))
            await self.modal(ui, 'Rename session')
            future = ui.dialogs.future
            await ui.key('F1')
            self.assertTrue(ui.view.help_visible)
            self.assertFalse(await ui.workflows.confirm_selection('Unarchive it? [y/N]',
                workspace=workspace('ws_other'), escape=False))
            self.assertTrue(ui.view.help_visible)
            self.assertIs(ui.dialogs.future, future)
            await ui.key('F1')
            await ui.key('Escape')
            await task

    async def test_plain_history_returns_to_actual_transcript_from_activity(self):
        async with workflow_harness(plain=True) as ui:
            ui.client.handle_event({'type': 'message', 'data': {'id': 7, 'timestamp': 1,
                'channel': 'general', 'text': 'server history message', 'sender': 'human'}})
            ui.view.show_activity()
            outcome = await ui.workflows.run_action('history')
            self.assertEqual(outcome.status, 'completed')
            await ui.wait_render()
            self.assertIn('server history message', ui.screen_text())
            self.assertFalse(ui.view.activity_visible)
            ui.api.action.assert_not_called()

    async def test_palette_quit_and_disabled_windows_reason(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            ui.state.selected_agent_id = 'ag_one'
            with patch('cli_tui_view.sys.platform', 'win32'):
                task = ui.start(ui.workflows.show_palette())
                await self.modal(ui, 'Commands')
                await ui.paste('Resume agent')
                await ui.wait_render()
                self.assertIn('Requires tmux (Linux/macOS).', ui.screen_text())
                await ui.key('Enter')
                self.assertFalse(task.done())
                ui.api.action.assert_not_called()
                await ui.key('Escape')
                await task
            task = ui.start(ui.workflows.show_palette())
            await self.modal(ui, 'Commands')
            await ui.paste('Quit')
            await ui.key('Enter')
            await task
            self.assertIn(('quit',), ui.calls)
