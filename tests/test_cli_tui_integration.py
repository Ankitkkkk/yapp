"""Real-server TUI acceptance; no paid provider or developer tmux socket."""

import asyncio
import copy
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests._cli_server import ROOT, IsolatedCliServer, cli
from tests._tui_harness import PTY_OBSERVER, PtyTerminal, application_harness
from cli_workspace_chat import WorkspaceChatController


class TuiIntegrationTests(IsolatedCliServer, unittest.IsolatedAsyncioTestCase):
    """Catches lost/duplicate sends and missing switch/Quit checkpoints."""

    @classmethod
    def environment_additions(cls):
        shim_directory = Path(cls.temp.name) / 'bin'
        shim_directory.mkdir()
        shim = shim_directory / 'kilo'
        shim.write_text(f'#!{sys.executable}\nfor line in __import__("sys").stdin: pass\n')
        shim.chmod(0o755)
        return {'PATH': str(shim_directory) + os.pathsep + os.defpath, 'SHELL': '/bin/sh'}

    @asynccontextmanager
    async def real_tui(self):
        client = cli.ChatClient(self.url)
        controller = WorkspaceChatController(client, self.api, no_resume=True,
                                             data_dir=str(self.data_dir), providers=['kilo'])
        events = []
        original_action = self.api.action
        original_receive = client.receive_forever

        def action(ident, operation, *args, **kwargs):
            result = original_action(ident, operation, *args, **kwargs)
            if operation == 'checkpoint':
                events.append(('checkpoint', ident))
            return result

        async def receive():
            events.append(('receiver_start',))
            try:
                await original_receive()
            finally:
                events.append(('receiver_cancel',))

        with patch.object(self.api, 'action', side_effect=action), \
                patch.object(client, 'receive_forever', side_effect=receive):
            async with application_harness(client, controller, size=(120, 30)) as ui:
                ui.events = events
                await ui.wait_until(lambda: ui.dialogs.future is not None
                                    and 'Show archived' in ui.screen_text(), timeout=15)
                yield ui

    async def create_session(self, ui, name):
        # F2 schedules navigation; a render can precede the modal opening.
        await ui.wait_until(lambda: ui.dialogs.future is not None
                            and 'Show archived' in ui.screen_text(), timeout=15)
        await ui.activate_named('new_session')
        await ui.wait_until(lambda: 'Session name:' in ui.screen_text(), timeout=15)
        await ui.type_text(name)
        await ui.key('Down')
        await ui.key('Enter')
        await ui.wait_until(lambda: 'Orchestrator provider:' in ui.screen_text(), timeout=15)
        await ui.focus_field('cwd')
        await ui.key('Enter')
        await ui.wait_until(lambda: ui.controller.workspace is not None
                            and ui.controller.workspace['name'] == name, timeout=15)
        await asyncio.wait_for(ui.client.ready.wait(), 15)
        await ui.wait_until(lambda: ui.focused_control == 'composer', timeout=15)
        ws_id = ui.controller.workspace['id']
        self.addCleanup(self.api.action, ws_id, 'archive')
        return ws_id

    async def test_real_session_message_switch_and_quit(self):
        async with self.real_tui() as ui:
            first = await self.create_session(ui, 'billing')
            text = 'Please review retries'
            await ui.type_text(text)
            await ui.key('Left')
            cursor = ui.view.composer.buffer.cursor_position
            snapshot = copy.deepcopy(await asyncio.to_thread(self.api.get, first))
            # Synthetic server-shaped status only; messages use real WebSocket.
            snapshot['agents'] = [dict(
                agent_id='ag_synthetic_status', registry_name='qa-status', provider='kilo',
                cwd=self.temp.name, last_state='running', native_session_id='NATIVE-SECRET-QA',
                tmux_session='yapp-ag_synthetic_status', history_mode='none', history_state='done',
                history_note=None, unread_count=0, last_error=None,
                last_launch={'kind': 'spawn', 'nonce': 'synthetic-only',
                             'at': '2026-09-13T00:00:00Z', 'pid': None})]
            ui.client.handle_event({'type': 'workspace', 'data': snapshot})
            await ui.wait_until(lambda: 'qa-status' in ui.screen_text())
            self.assertEqual(ui.view.composer.text, text)
            self.assertEqual(ui.view.composer.buffer.cursor_position, cursor)
            self.assertNotIn('NATIVE-SECRET-QA', ui.screen_text())
            self.assertEqual(ui.client.messages, {})
            await ui.send_message()
            await ui.wait_until(lambda: any(m.get('text') == text
                                           for m in ui.client.messages.values()), timeout=15)
            await ui.wait_until(lambda: text in ui.screen_text(), timeout=15)
            self.assertEqual(ui.screen_text().count(text), 1)
            self.assertEqual(ui.view.composer.text, '')
            await ui.key('F2')
            await ui.wait_until(lambda: 'Show archived' in ui.screen_text(), timeout=15)
            await ui.key('Escape')
            self.assertFalse(any(event[0] == 'checkpoint' for event in ui.events))
            await ui.key('F2')
            second = await self.create_session(ui, 'frontend')
            self.assertEqual([e[1] for e in ui.events if e[0] == 'checkpoint'], [first])
            receiver, poller = ui.tui.receiver_task, ui.tui.poller_task
            await ui.key('CtrlQ')
            self.assertIsNone(await asyncio.wait_for(ui.task, 15))
            self.assertEqual([e[1] for e in ui.events if e[0] == 'checkpoint'], [first, second])
            self.assertLess(ui.events.index(('checkpoint', second)),
                            ui.events.index(('receiver_cancel',)))
            self.assertEqual(ui.events.count(('receiver_start',)), 1)
            self.assertTrue(receiver.done() and poller.done())
            self.assertIsNone(ui.client.websocket)
        messages = await asyncio.to_thread(self.json_command, 'read', '--session', first)
        self.assertEqual(sum(m.get('text') == text for m in messages), 1)
        names = [await asyncio.to_thread(self.api.get, ident) for ident in (first, second)]
        self.assertEqual([w['name'] for w in names], ['billing', 'frontend'])


@unittest.skipUnless(os.name != 'nt' and shutil.which('tmux'), 'Requires POSIX tmux')
class _PtyCase(IsolatedCliServer):
    """Catches script-entry, VT input/resize, and real terminal ownership regressions."""

    provider_commands = ('claude', 'codex', 'gemini', 'antigravity', 'agy', 'kimi',
                         'qwen', 'kilo', 'codebuddy', 'copilot', 'minimax')

    @classmethod
    def environment_additions(cls):
        # Applied by IsolatedCliServer BEFORE server startup/provider lookup.
        cls.shim_directory = Path(cls.temp.name) / 'bin'
        cls.shim_directory.mkdir()
        cls.shim_log = Path(cls.temp.name) / 'inert-launches.txt'
        script = (f'#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n'
                  'with open(os.environ["TUI_INERT_LAUNCH_LOG"], "a") as log:\n'
                  '    log.write(Path(sys.argv[0]).name + "\\n")\n'
                  'print("INERT TUI AGENT READY", flush=True)\n'
                  'for line in sys.stdin:\n    print("INERT INPUT", flush=True)\n')
        for name in cls.provider_commands:
            shim = cls.shim_directory / name
            shim.write_text(script)
            shim.chmod(0o755)
        return {'PATH': str(cls.shim_directory) + os.pathsep + os.defpath,
                'SHELL': '/bin/sh', 'TUI_INERT_LAUNCH_LOG': str(cls.shim_log)}

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='yapp-tui-pty-')
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)
        self.capture_path = self.directory / 'screen.json'
        self.terminal_env = dict(self.env, TERM='xterm-256color',
                                 YAPP_PORT=str(self.ports[0]),
                                 YAPP_MCP_HTTP_PORT=str(self.ports[1]),
                                 YAPP_MCP_SSE_PORT=str(self.ports[2]),
                                 YAPP_DATA_DIR=str(self.data_dir),
                                 YAPP_UPLOAD_DIR=str(self.upload_dir))
        self.artifacts = Path(os.environ.get('TUI_QA_ARTIFACT_DIR', self.directory))
        self.artifacts.mkdir(parents=True, exist_ok=True)
        resolved = {name: shutil.which(name, path=self.terminal_env['PATH'])
                    for name in self.provider_commands}
        for name, path in resolved.items():
            self.assertEqual(path, str(self.shim_directory / name))
        self.addCleanup(self.cleanup_tmux)
        # Multiple shell-command arguments invoke this inert executable directly;
        # the very first pane cannot enter the user's default shell startup files.
        self.tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'qa-keeper',
                  '/bin/sleep', '120')
        self.tmux('set-option', '-g', 'default-shell', '/bin/sh')
        self.assertEqual(self.tmux('show-option', '-gv', 'default-shell').stdout.strip(), '/bin/sh')
        self.write_artifact(self._testMethodName + '.isolation.json', json.dumps({
            'PATH': self.terminal_env['PATH'], 'provider_commands': resolved,
            'default_shell': '/bin/sh', 'first_pane_argv': ['/bin/sleep', '120'],
            'server_environment_has_same_PATH': self.env['PATH'] == self.terminal_env['PATH']}))

    def tmux(self, *args, check=True):
        result = subprocess.run(['tmux', *args], env=self.terminal_env,
                                capture_output=True, text=True, timeout=5)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def cleanup_tmux(self):
        self.tmux('kill-server', check=False)
        self.poll(lambda: self.tmux('list-sessions', check=False).returncode != 0)
        socket = Path(self.terminal_env['TMUX_TMPDIR']) / ('tmux-' + str(os.getuid())) / 'default'
        # Only this fixture-owned path; TemporaryDirectory also owns it.
        socket.unlink(missing_ok=True)
        self.assertFalse(socket.exists())
        self.write_artifact(self._testMethodName + '.tmux-cleanup.json', json.dumps({
            'isolated_server_stopped': True, 'isolated_socket_removed': True}))

    def archive_session(self, ident):
        count = len(self.api.get(ident)['agents'])
        self.api.action(ident, 'archive')
        self.write_artifact(self._testMethodName + '.archive.json', json.dumps({
            'agents_before_archive': count,
            'inert_launches': self.shim_log.read_text().splitlines() if self.shim_log.exists() else [],
            'archived': self.api.get(ident)['archived']}))

    def write_artifact(self, name, text):
        text = re.sub(r'([?&]token=)[^\s&\"\'<>]+', r'\1[REDACTED]', text,
                      flags=re.IGNORECASE)
        text = re.sub(r'(Session token:\s*)\S+', r'\1[REDACTED]', text,
                      flags=re.IGNORECASE)
        (self.artifacts / name).write_text(text.replace(self.token, '[REDACTED]'))

    def cli_command(self, *args):
        return [sys.executable, '-c', PTY_OBSERVER, str(self.capture_path), str(ROOT),
                '--url', self.url, '--no-resume', *args]

    def screen(self, terminal, predicate=lambda value: True):
        return terminal.screen(self.capture_path, predicate)

    def press(self, terminal, key, predicate=lambda value: True):
        before = self.screen(terminal)['count']
        terminal.key(key)
        return self.screen(terminal, lambda value: value['count'] > before and predicate(value))

    def paste(self, terminal, text):
        terminal.paste(text)
        return self.screen(terminal, lambda value: value['buffer'] == text)

    def button(self, terminal, caption):
        for _ in range(30):
            if self.screen(terminal)['focus_caption'] == caption:
                return self.press(terminal, 'Enter')
            self.press(terminal, 'Tab')
        self.fail('Button not keyboard reachable: ' + caption)

    def palette(self, terminal, action):
        self.press(terminal, 'F4', lambda s: 'Commands' in s['text'] and 'Search:' in s['text'])
        self.paste(terminal, action)
        return self.press(terminal, 'Enter')

    def focus_agent_cwd(self, terminal):
        for _ in range(len(self.provider_commands) + 1):
            if '‹ kilo ›' in self.screen(terminal)['text']:
                break
            self.press(terminal, 'Tab')
        else:
            self.fail('Inert kilo provider cannot be selected')
        self.assertIn('‹ kilo ›', self.screen(terminal)['text'])
        return self.press(terminal, 'Down')

    def save(self, terminal, label):
        snapshot = self.screen(terminal)
        self.write_artifact(label + '.vt.txt', terminal.output())
        if terminal.process.poll() is not None:
            # Last renderer frame belongs to before exit; never label it as
            # post-restoration screen evidence.
            self.write_artifact(label + '.exit.json', json.dumps({
                'returncode': terminal.process.returncode, 'source': 'pty-process',
                'restoration_assertions_passed': True}))
            return snapshot
        self.write_artifact(label + '.json', json.dumps(snapshot, ensure_ascii=False))
        self.write_artifact(label + '.txt', snapshot['text'])
        return snapshot

    @staticmethod
    def activity_body(snapshot):
        lines = snapshot['text'].splitlines()
        for index, row in enumerate(lines):
            if 'Activity · ' not in row or 'omitted · Esc Back' not in row:
                continue
            title = row.index('Activity · ')
            left, right = row.rfind('┌', 0, title), row.find('┐', title)
            if left < 0 or right < 0:
                return ''
            body = []
            for line in lines[index + 1:]:
                if len(line) > left and line[left] == '└':
                    return '\n'.join(body)
                body.append(line[left + 1:right])
        return ''

    def assert_restored(self, terminal):
        import termios
        self.assertEqual(terminal.process.wait(timeout=15), 0)
        terminal.wait(lambda: 'QA_TERMINAL_RETURNED' in terminal.output())
        self.assertEqual(termios.tcgetattr(terminal.slave), terminal.before_termios)
        self.assertIn('\x1b[?1049h', terminal.output())
        self.assertIn('\x1b[?1049l', terminal.output())
        self.assertGreater(terminal.output().rfind('\x1b[?1049l'),
                           terminal.output().rfind('\x1b[?1049h'))
        self.assertIn('\x1b[?25h', terminal.output())
        self.assertNotIn('Full-screen unavailable', terminal.output())
        self.assertNotIn('unexpected local error', terminal.output())

    def assert_terminal_closed(self, terminal):
        self.assertIsNotNone(terminal.process.poll())
        self.assertFalse(terminal.reader.is_alive())
        for fd in (terminal.master, terminal.slave):
            with self.assertRaises(OSError):
                os.fstat(fd)
        (self.artifacts / (self._testMethodName + '.cleanup.json')).write_text(json.dumps({
            'child_reaped': True, 'reader_joined': True, 'pty_fds_closed': True}))


class TuiPtyIntegrationTests(_PtyCase):
    def test_script_resize_paste_navigation_cancel_mouse_and_quit(self):
        with PtyTerminal(self.cli_command(), env=self.terminal_env, cwd=ROOT) as terminal:
            self.addCleanup(self.assert_terminal_closed, terminal)
            self.screen(terminal, lambda s: 'Show archived' in s['text'])
            self.button(terminal, 'New session')
            self.screen(terminal, lambda s: 'Session name:' in s['text'])
            self.paste(terminal, 'pty-controls')
            self.press(terminal, 'Enter', lambda s: 'Orchestrator provider:' in s['text'])
            self.press(terminal, 'Down', lambda s: 'Working directory:' in s['text'])
            self.press(terminal, 'Enter', lambda s: 'Connected' in s['text'] and 'Session name:' not in s['text'])
            session = next(w for w in self.api.list()['workspaces'] if w['name'] == 'pty-controls')
            self.addCleanup(self.archive_session, session['id'])
            draft = 'first line\nsecond line'
            self.paste(terminal, draft)
            self.press(terminal, 'Left', lambda s: s['buffer_cursor'] == len(draft) - 1)
            # A transcript click must not strand Vim edit-entry keys or mouse refocus.
            self.press(terminal, 'Escape', lambda s: 'Message · NORMAL' in s['text'])
            current = self.screen(terminal)
            y, row = next((y, row) for y, row in enumerate(current['text'].splitlines())
                          if 'No messages yet' in row)
            x = row.index('No messages yet')
            click_chat = f'\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m'
            terminal.send(click_chat)
            self.screen(terminal, lambda s: s['buffer'] == '')
            terminal.send('I')
            self.screen(terminal, lambda s: s['buffer'] == draft
                        and s['buffer_cursor'] == draft.index('\n') + 1
                        and 'Message · INSERT' in s['text'])
            terminal.send('Z')
            self.screen(terminal, lambda s: s['buffer'] == draft.replace('\n', '\nZ'))
            terminal.send('\x7f')
            self.screen(terminal, lambda s: s['buffer'] == draft)
            self.press(terminal, 'End')
            self.press(terminal, 'Left', lambda s: s['buffer_cursor'] == len(draft) - 1)
            terminal.send(click_chat)
            current = self.screen(terminal, lambda s: s['buffer'] == '')
            y, row = next((y, row) for y, row in enumerate(current['text'].splitlines())
                          if 'second line' in row)
            x = row.index('second line') + 2
            terminal.send(f'\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m')
            self.screen(terminal, lambda s: s['buffer'] == draft and 'Message · INSERT' in s['text'])
            self.save(terminal, 'pty-mouse-vim-recovered')
            initial = self.save(terminal, 'pty-wide-120x30')
            self.assertEqual(initial['buffer_cursor'], len(draft) - 1)
            self.assertEqual(self.json_command('read', '--session', session['id']), [])
            for columns, rows, label in ((80, 24, 'pty-compact-80x24'), (70, 16, 'pty-small-70x16')):
                before = self.screen(terminal)['count']
                terminal.resize(columns, rows)
                self.screen(terminal, lambda s: (s['columns'], s['rows']) == (columns, rows)
                            and s['count'] > before
                            and ('Resize terminal' in s['text'] if columns == 70
                                 else 'Message' in s['text'] and 'New session' not in s['text']))
                self.save(terminal, label)
            self.press(terminal, 'F1', lambda s: 'F1/Esc Back' in s['text']
                       and 'Message starts NORMAL' in s['text'])
            self.save(terminal, 'pty-small-help')
            self.press(terminal, 'Escape')
            before = self.screen(terminal)['count']
            terminal.resize(120, 30)
            restored = self.screen(terminal, lambda s: (s['columns'], s['rows']) == (120, 30)
                                   and s['count'] > before and s['buffer'] == draft
                                   and 'New session' in s['text'] and 'Clear draft' in s['text'])
            self.assertEqual(restored['buffer_cursor'], initial['buffer_cursor'])
            self.save(terminal, 'pty-wide-restored')
            self.press(terminal, 'F2', lambda s: 'Show archived' in s['text'])
            self.paste(terminal, 'pty-controls')
            self.save(terminal, 'pty-navigation-search')
            self.press(terminal, 'Escape', lambda s: s['buffer'] == draft)
            self.press(terminal, 'F5', lambda s: 'Connected to ' in self.activity_body(s))
            self.save(terminal, 'pty-activity')
            self.press(terminal, 'Escape', lambda s: s['buffer'] == draft)
            self.palette(terminal, 'Add agent')
            self.screen(terminal, lambda s: 'Working directory:' in s['text'])
            focused = self.focus_agent_cwd(terminal)
            self.write_artifact('pty-provider-focus.json', json.dumps({
                'selected_kilo': '‹ kilo ›' in focused['text'],
                'focused_buffer': focused['buffer'], 'expected_cwd': str(ROOT)}))
            self.assertEqual(focused['buffer'], str(ROOT),
                             'Working-directory focus required before editing or submitting')
            terminal.send('\x01\x0b')
            self.screen(terminal, lambda s: s['buffer'] == '')
            self.paste(terminal, 'relative-invalid-cwd')
            self.press(terminal, 'Enter', lambda s: 'absolute existing directory' in s['text'])
            self.save(terminal, 'pty-validation-error')
            self.assertIn('relative-invalid-cwd', self.screen(terminal)['text'])
            self.assertFalse(any(agent.get('kind') != 'orchestrator'
                                 for agent in self.api.get(session['id'])['agents']))
            self.press(terminal, 'Escape', lambda s: s['buffer'] == draft)
            # Actual SGR press/release on visible sidebar button.
            current = self.screen(terminal)
            y, row = next((y, row) for y, row in enumerate(current['text'].splitlines())
                          if 'New session' in row)
            x = row.index('New session') + 2
            terminal.send(f'\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m')
            self.screen(terminal, lambda s: 'Session name:' in s['text'])
            self.save(terminal, 'pty-mouse-dialog')
            self.press(terminal, 'Escape', lambda s: s['buffer'] == draft)
            self.press(terminal, 'CtrlC', lambda s: s['buffer'] == draft)
            self.press(terminal, 'CtrlQ', lambda s: 'Quit with unsent' in s['text'])
            self.save(terminal, 'pty-quit-confirmation')
            terminal.send('n')
            self.screen(terminal, lambda s: s['buffer'] == draft)
            self.press(terminal, 'CtrlQ', lambda s: 'Quit with unsent' in s['text'])
            terminal.send('y')
            self.assert_restored(terminal)
            self.save(terminal, 'pty-exited')


@unittest.skipUnless(os.name != 'nt' and shutil.which('tmux'), 'Requires POSIX tmux')
class TuiTmuxIntegrationTests(_PtyCase):
    def setUp(self):
        super().setUp()
        self.session = self.api.create(self._testMethodName)
        self.addCleanup(self.archive_session, self.session['id'])
        self.agent = self.api.action(self.session['id'], 'spawn', body={
            'provider': 'kilo', 'cwd': self.temp.name,
            'name': 'qa-inert-' + self._testMethodName.split('_', 2)[1],
            'history_mode': 'none'})
        def running():
            row = self.api.get(self.session['id'])['agents'][0]
            return row if row['last_state'] == 'running' else None
        self.agent = self.poll(running)
        self.target = self.agent['tmux_session']
        self.tmux('has-session', '-t', '=' + self.target)
        self.agent_pane = self.tmux('list-panes', '-t', self.target + ':',
                                    '-F', '#{pane_id}').stdout.strip()
        self.poll(lambda: 'INERT TUI AGENT READY' in self.tmux(
            'capture-pane', '-p', '-t', self.agent_pane).stdout)

    def clients(self):
        result = self.tmux('list-clients', '-F', '#{client_name}|#{session_name}', check=False)
        return dict(line.split('|', 1) for line in result.stdout.splitlines())

    def open_attach(self, terminal):
        self.press(terminal, 'F3', lambda s: 'Choose agent' in s['text'])
        self.press(terminal, 'Enter', lambda s: 'Agent actions' in s['text'])
        self.paste(terminal, 'Attach')
        # Outside tmux this suspends rendering until detach; await real client
        # ownership below rather than demanding a new TUI frame here.
        terminal.key('Enter')

    def prepare_draft(self, terminal):
        self.screen(terminal, lambda s: 'Connected' in s['text'] and 'qa-inert' in s['text'])
        self.paste(terminal, 'survives attachment')
        self.press(terminal, 'Left', lambda s: s['buffer_cursor'] == len('survives attachment') - 1)
        return self.screen(terminal)['buffer_cursor']

    def finish_with_draft(self, terminal):
        self.press(terminal, 'CtrlQ', lambda s: 'Quit with unsent' in s['text'])
        terminal.send('y')

    def test_stop_all_releases_two_sessions_and_keeps_saved_chat(self):
        other = self.api.create('other-stop-all-session')
        self.addCleanup(self.archive_session, other['id'])
        self.api.action(other['id'], 'spawn', body={
            'provider': 'kilo', 'cwd': self.temp.name, 'name': 'qa-inert-other-stop',
            'history_mode': 'none'})
        second = self.poll(lambda: next((a for a in self.api.get(other['id'])['agents']
                                        if a['last_state'] == 'running'), None))
        agents = (self.agent, second)
        wrappers = [(a['last_launch'].get('wrapper_pid') or a['last_launch']['pid']) for a in agents]
        panes = [int(self.tmux('list-panes', '-t', a['tmux_session'] + ':',
                              '-F', '#{pane_pid}').stdout.strip()) for a in agents]
        self.json_command('send', '--session', self.session['id'], 'Saved stop-all note')
        with PtyTerminal(self.cli_command('--session', self.session['id']),
                         env=self.terminal_env, cwd=ROOT) as terminal:
            self.addCleanup(self.assert_terminal_closed, terminal)
            cursor = self.prepare_draft(terminal)
            self.palette(terminal, 'Stop all agents')
            self.screen(terminal, lambda s: 'Stop all agents?' in s['text'])
            self.press(terminal, 'Enter', lambda s: 'Stop all agents?' not in s['text'])
            for a in agents:
                self.tmux('has-session', '-t', '=' + a['tmux_session'])
            self.palette(terminal, 'Stop all agents')
            self.screen(terminal, lambda s: 'Stop all agents?' in s['text'])
            terminal.send('y')
            result = self.screen(terminal, lambda s: 'Confirmed stopped:' in s['text']
                                 and s['buffer'] == 'survives attachment')
            self.assertEqual(result['buffer_cursor'], cursor)
            self.assertIn('Message · INSERT', result['text'])
            self.save(terminal, 'stop-all-completed')
            for a in agents:
                self.assertNotEqual(self.tmux('has-session', '-t', '=' + a['tmux_session'],
                                             check=False).returncode, 0)
            def gone(pid):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                # An orphan zombie has already released its execution resources.
                stat = Path(f'/proc/{pid}/stat')
                return stat.exists() and stat.read_text().split(') ', 1)[1].startswith('Z ')
            for pid in wrappers + panes:
                self.poll(lambda pid=pid: gone(pid))
            self.tmux('has-session', '-t', '=qa-keeper')
            for ident, original in ((self.session['id'], self.agent), (other['id'], second)):
                saved = self.api.get(ident)
                self.assertFalse(saved['archived'])
                self.assertEqual(len(saved['agents']), 1)
                self.assertEqual(saved['agents'][0]['agent_id'], original['agent_id'])
                self.assertEqual(saved['agents'][0]['last_state'], 'exited')
            messages = self.json_command('read', '--session', self.session['id'])
            self.assertTrue(any(m['text'] == 'Saved stop-all note' for m in messages))
            self.finish_with_draft(terminal)
            self.assert_restored(terminal)

    def test_resume_refusal_then_explicit_fresh_launch(self):
        self.api.action(self.session['id'], 'stop', self.agent['agent_id'])
        self.assertEqual(self.api.get(self.session['id'])['agents'][0]['last_state'], 'exited')
        launches_before = self.shim_log.read_text().splitlines()
        with PtyTerminal(self.cli_command('--session', self.session['id']),
                         env=self.terminal_env, cwd=ROOT) as terminal:
            self.addCleanup(self.assert_terminal_closed, terminal)
            self.screen(terminal, lambda s: 'Connected' in s['text'] and 'qa-inert' in s['text'])
            before = self.screen(terminal)['count']
            terminal.resize(80, 18)
            self.screen(terminal, lambda s: (s['columns'], s['rows']) == (80, 18)
                        and s['count'] > before)
            self.press(terminal, 'F3', lambda s: 'Choose agent' in s['text'])
            self.press(terminal, 'Enter', lambda s: 'Agent actions' in s['text'])
            self.paste(terminal, 'Resume agent')
            self.press(terminal, 'Enter', lambda s: 'Launch mode:' in s['text'])
            self.press(terminal, 'Enter', lambda s: 'resume not supported for kilo' in s['text'])
            refused = self.save(terminal, 'resume-refusal-80x18')
            self.assertIn('< Resume agent >', refused['text'])
            self.assertEqual(self.api.get(self.session['id'])['agents'][0]['last_state'], 'exited')
            self.assertEqual(self.shim_log.read_text().splitlines(), launches_before)
            self.press(terminal, 'Down')
            self.press(terminal, 'Tab', lambda s: '‹ fresh ›' in s['text'])
            self.press(terminal, 'Enter', lambda s: 'Fresh launch for' in s['text'])
            self.assertEqual(self.shim_log.read_text().splitlines(), launches_before)
            terminal.send('y')
            self.screen(terminal, lambda s: 'Launch mode:' not in s['text']
                        and 'Fresh launch for' not in s['text'] and s['buffer'] == '')
            def running():
                row = self.api.get(self.session['id'])['agents'][0]
                return row if row['last_state'] == 'running' else None
            resumed = self.poll(running)
            self.assertEqual(resumed['last_launch']['kind'], 'fresh')
            self.assertEqual(resumed['agent_id'], self.agent['agent_id'])
            self.assertEqual(len(self.shim_log.read_text().splitlines()), len(launches_before) + 1)
            terminal.key('CtrlQ')
            self.assert_restored(terminal)

    def test_outside_attach_detach_preserves_draft_and_repaints_delivery(self):
        with PtyTerminal(self.cli_command('--session', self.session['id']),
                         env=self.terminal_env, cwd=ROOT) as terminal:
            self.addCleanup(self.assert_terminal_closed, terminal)
            cursor = self.prepare_draft(terminal)
            self.save(terminal, 'tmux-outside-before')
            self.open_attach(terminal)
            terminal.wait(lambda: self.target in self.clients().values())
            frozen = self.screen(terminal)['count']
            raw_start = len(terminal.output_bytes())
            self.json_command('send', '--session', self.session['id'], 'received during outside attach')
            # Authenticated persisted send completes while foreground owns tty.
            self.assertEqual(self.screen(terminal)['count'], frozen)
            attached_bytes = terminal.output_bytes()[raw_start:]
            self.assertNotIn(b'received during outside attach', attached_bytes)
            self.write_artifact('tmux-outside-attached.vt.txt', attached_bytes.decode('utf-8', errors='replace'))
            terminal.send('\x02d')
            restored = self.screen(terminal, lambda s: s['count'] > frozen
                                   and s['buffer'] == 'survives attachment'
                                   and 'received during outside attach' in s['text'])
            self.assertEqual(restored['buffer_cursor'], cursor)
            self.save(terminal, 'tmux-outside-returned')
            self.assertFalse(self.clients())
            self.finish_with_draft(terminal)
            self.assert_restored(terminal)
            self.save(terminal, 'tmux-outside-exited')

    def test_nested_switch_return_and_tmux_selection_copy(self):
        import shlex
        tui_session = 'qa-tui-nested'
        self.addCleanup(self.tmux, 'kill-session', '-t', '=' + tui_session, check=False)
        command = shlex.join(self.cli_command('--session', self.session['id']))
        self.tmux('new-session', '-d', '-s', tui_session, '-x', '120', '-y', '30', command)
        self.tmux('set-option', '-t', tui_session, 'status', 'off')
        attach_command = [sys.executable, '-c',
                          'import fcntl,os,sys,termios; fcntl.ioctl(0,termios.TIOCSCTTY,0); '
                          'os.execvp(sys.argv[1],sys.argv[1:])',
                          'tmux', 'attach', '-t', '=' + tui_session]
        with PtyTerminal(attach_command, env=self.terminal_env, cwd=ROOT) as terminal:
            self.addCleanup(self.assert_terminal_closed, terminal)
            client = terminal.wait(lambda: next((c for c, s in self.clients().items()
                                                if s == tui_session), None))
            cursor = self.prepare_draft(terminal)
            self.save(terminal, 'tmux-nested-before')
            self.open_attach(terminal)
            terminal.wait(lambda: self.clients().get(client) == self.target)
            message = 'copied terminal message\n    indented copy line\n│ actual message separator'
            self.json_command('send', '--session', self.session['id'], message)
            self.tmux('switch-client', '-c', client, '-l')
            terminal.wait(lambda: self.clients().get(client) == tui_session)
            self.screen(terminal, lambda s: s['buffer'] == 'survives attachment'
                        and 'copied terminal message' in s['text'])
            self.assertEqual(self.screen(terminal)['buffer_cursor'], cursor)
            self.save(terminal, 'tmux-nested-returned')
            self.press(terminal, 'F5', lambda s: 'Switch back: tmux switch-client -l' in self.activity_body(s))
            self.save(terminal, 'tmux-nested-guidance')
            self.press(terminal, 'Escape', lambda s: s['buffer'] == 'survives attachment')
            before_selection = self.screen(terminal)['count']
            terminal.send('\x1b[18~')
            self.screen(terminal, lambda s: s['count'] > before_selection and 'F7 return' in s['text'])
            pane = self.tmux('list-panes', '-t', tui_session + ':', '-F', '#{pane_id}').stdout.strip()
            captured = self.tmux('capture-pane', '-p', '-t', pane).stdout
            self.assertIn(message, captured)
            self.write_artifact('tmux-nested-capture-pane.txt', captured)
            self.tmux('copy-mode', '-t', pane)
            self.tmux('send-keys', '-t', pane, '-X', 'history-top')
            for _ in range(captured.splitlines().index('copied terminal message')):
                self.tmux('send-keys', '-t', pane, '-X', 'cursor-down')
            self.tmux('send-keys', '-t', pane, '-X', 'start-of-line')
            self.tmux('send-keys', '-t', pane, '-X', 'begin-selection')
            for _ in range(2):
                self.tmux('send-keys', '-t', pane, '-X', 'cursor-down')
            self.tmux('send-keys', '-t', pane, '-X', 'end-of-line')
            self.tmux('send-keys', '-t', pane, '-X', 'copy-selection-and-cancel')
            copied = self.tmux('show-buffer').stdout
            self.assertEqual(copied.rstrip('\n'), message)
            self.write_artifact('tmux-native-selection-copy.txt', copied)
            before_restore = self.screen(terminal)['count']
            terminal.send('\x1b[18~')
            self.screen(terminal, lambda s: s['count'] > before_restore and 'F7 return' not in s['text'])
            self.assertEqual(self.screen(terminal)['buffer'], 'survives attachment')
            self.assertEqual(self.screen(terminal)['buffer_cursor'], cursor)
            terminal.send('Z')
            resumed = self.screen(terminal, lambda s: s['buffer'] == 'survives attachmenZt')
            self.assertEqual(resumed['buffer_cursor'], cursor + 1)
            self.finish_with_draft(terminal)
            terminal.wait(lambda: tui_session not in self.clients().values())
            self.assertEqual(terminal.process.wait(timeout=15), 0)
            self.assertIn('\x1b[?1049l', terminal.output())
            self.assertGreater(terminal.output().rfind('\x1b[?1049l'),
                               terminal.output().rfind('\x1b[?1049h'))
