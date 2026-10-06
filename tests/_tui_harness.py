"""Pipe-input Application harness; capture actual renderer cells, never ANSI parsing."""

import asyncio
from contextlib import asynccontextmanager
import io

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.filters import Condition
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.layout import walk
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.widgets import Button
from prompt_toolkit.output.vt100 import Vt100_Output

from cli import ChatClient
from cli_tui_dialogs import DialogHost
from cli_tui_state import TuiState
from cli_tui_view import ComposerActions, TuiView
from cli_view_contracts import ActionOutcome, SubmitOutcome
from cli_workspace_chat import WorkspaceChatController


class _HarnessAPI:
    def list(self, *, include_archived=False):
        return {'workspaces': []}


class TuiHarness:
    """Bounded render waits and controlled callbacks; no network or provider startup."""

    sequences = {'F1': '\x1bOP', 'F2': '\x1bOQ', 'F3': '\x1bOR', 'F4': '\x1bOS', 'F5': '\x1b[15~',
                 'F7': '\x1b[18~', 'Enter': '\r', 'Escape': '\x1b', 'CtrlQ': '\x11', 'CtrlC': '\x03',
                 'CtrlD': '\x04', 'AltEnter': '\x1b\r', 'Left': '\x1b[D', 'Right': '\x1b[C', 'CtrlSpace': '\x00', 'Tab': '\t', 'ShiftTab': '\x1b[Z', 'Home': '\x1b[H', 'End': '\x1b[F',
                 'PageUp': '\x1b[5~', 'PageDown': '\x1b[6~', 'Up': '\x1b[A', 'Down': '\x1b[B'}

    def __init__(self, size, client, controller, pipe):
        self.size = Size(rows=size[1], columns=size[0])
        self.stream = io.StringIO()
        self.pipe = pipe
        self.calls = []
        self.rendered = asyncio.Event()
        self.render_count = 0
        self.rows = []
        self.cells = {}
        self.client = client or (controller.client if controller else
                                 ChatClient('http://127.0.0.1:18300'))
        if controller is not None and controller.client is not self.client:
            raise ValueError('controller must own the supplied client')
        self.controller = controller or WorkspaceChatController(self.client, _HarnessAPI())
        self.api = self.controller.api
        self.state = TuiState()
        self.dialogs = DialogHost(lambda: self.application, self.invalidate,
            owner_is_short_lived=lambda owner: owner in getattr(self, 'tasks', ())
            or owner in self.application._background_tasks)

        async def submit(text):
            self.calls.append(('submit', text))
            return SubmitOutcome('cancelled')

        async def quit():
            self.calls.append(('quit',))

        async def navigate(mandatory=False):
            self.calls.append(('navigate', mandatory))
            return ActionOutcome('cancelled')

        async def run_action(action_id, *, target_id=None):
            self.calls.append(('run_action', action_id, target_id))
            return ActionOutcome('cancelled')

        async def attach(agent_id):
            self.calls.append(('attach', agent_id))
            return ActionOutcome('cancelled', agent_id=agent_id)

        self.callbacks = dict(submit=submit, quit=quit, navigate=navigate,
                              run_action=run_action, attach=attach)
        self.view = TuiView(self.client, self.controller, self.state,
                            self.dialogs, self.callbacks)
        self.application = Application(
            layout=Layout(self.view.root, focused_element=self.view.composer),
            input=pipe, output=Vt100_Output(self.stream, get_size=lambda: self.size,
                                           enable_cpr=False),
            full_screen=True, mouse_support=Condition(lambda: not self.view.selecting_text),
            key_bindings=self.view.global_key_bindings,
            style=self.view.style, after_render=self._capture)
        self.key_count = 0
        self.input_processed = asyncio.Event()
        self.application.key_processor.after_key_press += self._key_processed
        self.application.ttimeoutlen = 0.05
        self.application.timeoutlen = 1.0
        self._old = (self.client.on_view_change, self.client.output,
                     self.client.on_workspace, self.client.on_settings,
                     self.controller.presentation, self.controller.on_view_change)
        harness = self

        class Presenter:
            confirm = self.dialogs.confirm

            async def attach(self, agent):
                return await attach(agent['agent_id'])

            def notice(self, text):
                harness.notice(text)

        self.client.on_view_change = self.view.refresh
        self.client.output = self.notice
        self.controller.bind_view(Presenter(), self.view.refresh)

    def _key_processed(self, sender):
        self.key_count += 1
        self.input_processed.set()

    def invalidate(self):
        if hasattr(self, 'application'):
            self.application.invalidate()

    def notice(self, text):
        self.state.notices.add(text)
        self.invalidate()

    def _capture(self, application):
        screen = application.renderer.last_rendered_screen
        if screen is None:
            return
        self.cells = {(x, y): screen.data_buffer[y][x].char
                      for y in range(self.size.rows) for x in range(self.size.columns)}
        self.rows = [''.join(self.cells[x, y] for x in range(self.size.columns)).rstrip()
                     for y in range(self.size.rows)]
        self.render_count += 1
        self.rendered.set()

    def cell(self, x, y):
        return self.cells[x, y]

    def screen_text(self):
        return '\n'.join(self.rows)

    @property
    def focused_control(self):
        current = self.application.layout.current_control
        for name in ('composer', 'conversation', 'navigation', 'agents', 'new_session', 'activity', 'agent_actions', 'clear_draft', 'restart_server'):
            target = getattr(self.view, name)
            if current is getattr(target, 'control', target):
                return name
        return 'dialog'

    async def wait_until(self, predicate, timeout=2):
        async def wait():
            while not predicate():
                self.rendered.clear()
                self.application.invalidate()
                await self.rendered.wait()
        await asyncio.wait_for(wait(), timeout)

    async def wait_render(self, timeout=2):
        count = self.render_count
        await self.wait_until(lambda: self.render_count > count, timeout)

    async def key(self, name):
        """Escape includes the configured 50ms terminal escape decoding timeout."""
        await self._send(self.sequences[name])

    async def send_message(self):
        """Perform the user's explicit Normal-mode send gesture."""
        await self.key('Escape')
        await self.key('Enter')

    def bind_submit(self, submit):
        self.submit_mock = submit
        self.callbacks['submit'] = submit
        if not hasattr(self, 'composer_actions'):
            self.composer_actions = ComposerActions(self.view, self.state, submit, self.notice)
        else:
            self.composer_actions.submit = submit

    async def paste(self, text):
        await self.type_text(text)

    async def type_text(self, text):
        await self._send('\x1b[200~' + text + '\x1b[201~')

    async def click(self, x, y):
        """Send a real SGR mouse press/release at zero-based screen cells."""
        await self._send(f'\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m')

    async def _send(self, text):
        count = self.key_count
        self.input_processed.clear()
        await asyncio.to_thread(self.pipe.send_text, text)
        async def processed():
            while self.key_count == count:
                await self.input_processed.wait()
        await asyncio.wait_for(processed(), 2)
        await self.wait_render()

    async def activate_named(self, name):
        """Reach an actual visible button with Tab, then activate with Enter."""
        caption = {'show_archived': 'Show archived', 'new_session': 'New session',
                   'refresh': 'Refresh', 'more_actions': 'More actions', 'agent_actions': 'Actions', 'clear_draft': 'Clear draft'}[name]
        target = None
        for container in walk(self.application.layout.container, skip_hidden=True):
            if isinstance(container, Window):
                owner = getattr(getattr(container.content, 'text', None), '__self__', None)
                if isinstance(owner, Button) and (owner.text == caption or
                        caption == 'Show archived' and owner.text.startswith(caption + ' [')):
                    target = container.content
        if target is None:
            raise AssertionError('Visible button missing: ' + caption)
        for _ in range(30):
            if self.application.layout.current_control is target:
                await self.key('Enter')
                return
            await self.key('Tab')
        raise AssertionError('Button cannot be reached by Tab: ' + caption)

    async def focus_field(self, name):
        captions = {'launch_mode': 'Launch mode:', 'cwd': 'Working directory',
                    'name': 'Agent name', 'mode': 'History mode:', 'provider': 'Provider:'}
        found, target = False, None
        for container in walk(self.dialogs.body, skip_hidden=True):
            if not isinstance(container, Window):
                continue
            control = container.content
            if found and control.is_focusable():
                target = control
                break
            text = getattr(control, 'text', '')
            rendered = ''.join(fragment[1] for fragment in to_formatted_text(text))
            if rendered.startswith(captions[name]):
                found = True
        if target is None:
            raise AssertionError('Visible field missing: ' + name)
        for _ in range(30):
            if self.application.layout.current_control is target:
                return
            await self.key('Down')
        raise AssertionError('Field cannot be reached by Down: ' + name)

    async def select_row(self, ident):
        """Search and activate only after asserting the full highlighted ID."""
        from prompt_toolkit.layout.controls import BufferControl
        for _ in range(30):
            if isinstance(self.application.layout.current_control, BufferControl):
                break
            await self.key('Tab')
        else:
            raise AssertionError('Search is not keyboard reachable')
        await self._send('\x01\x0b')
        await self.paste(ident.swapcase())
        await self.wait_until(lambda: self.state.selected_session_id == ident)
        if self.state.selected_session_id != ident:
            raise AssertionError('Focused stable ID differs from intended target')
        await self.key('Enter')

    async def resize(self, columns, rows):
        before = self.render_count
        self.size = Size(rows=rows, columns=columns)
        self.application._on_resize()
        if self.render_count != before + 1:
            raise AssertionError('resize must capture exactly one synchronous redraw')

    async def close(self):
        self.dialogs.cancel()
        try:
            if not self.task.done() and not self.application.is_done:
                self.application.exit()
            await asyncio.wait_for(self.task, 2)
        finally:
            if not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            (self.client.on_view_change, self.client.output, self.client.on_workspace,
             self.client.on_settings, self.controller.presentation,
             self.controller.on_view_change) = self._old


@asynccontextmanager
async def tui_harness(size=(120, 30), *, client=None, controller=None):
    with create_pipe_input() as pipe:
        ui = TuiHarness(size, client, controller, pipe)
        ui.task = asyncio.create_task(ui.application.run_async())
        try:
            await ui.wait_render()
            if not ui.controller.plain_channel and ui.api is not None:
                ui.view.set_sessions_loading(True)
                response = await asyncio.wait_for(ui.controller.list_sessions(include_archived=True), 2)
                ui.view.set_sessions(response['workspaces'])
                if response.get('warning'):
                    ui.notice(response['warning'])
                ui.view.set_sessions_loading(False)
                await ui.wait_render()
            yield ui
        finally:
            await ui.close()


class ApplicationHarness(TuiHarness):
    """Capture production TuiApplication using the same real pipe/render helpers."""

    def __init__(self, size, client, controller, pipe, **kwargs):
        from cli_tui import TuiApplication
        self.size = Size(rows=size[1], columns=size[0])
        self.stream, self.pipe = io.StringIO(), pipe
        self.rendered, self.input_processed = asyncio.Event(), asyncio.Event()
        self.render_count = self.key_count = 0
        self.rows, self.cells = [], {}
        self.client, self.controller, self.api = client, controller, controller.api
        output = Vt100_Output(self.stream, get_size=lambda: self.size, enable_cpr=False)
        self.tui = TuiApplication(client, controller, input=pipe, output=output, **kwargs)
        self.application = self.tui.application
        self.application.after_render += self._capture
        self.application.key_processor.after_key_press += self._key_processed
        self.application.ttimeoutlen = .05
        self.application.timeoutlen = .2
        for name in ('view', 'state', 'dialogs', 'composer_actions', 'workflows', 'callbacks'):
            setattr(self, name, getattr(self.tui, name))

    async def close(self):
        if not self.task.done():
            await asyncio.wait_for(self.tui.request_quit(signal=True), 3)
        # Tests explicitly inspect startup exceptions; cleanup only retrieves them.
        await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 3)

    async def _send(self, text):
        before, count = self.render_count, self.key_count
        self.input_processed.clear()
        await asyncio.to_thread(self.pipe.send_text, text)
        if self.key_count == count:
            await asyncio.wait_for(self.input_processed.wait(), 2)
        if self.render_count == before and not self.application.is_done:
            await self.wait_until(lambda: self.render_count > before or self.task.done())


@asynccontextmanager
async def application_harness(client, controller, *, size=(120, 35), **kwargs):
    with create_pipe_input() as pipe:
        ui = ApplicationHarness(size, client, controller, pipe, **kwargs)
        ui.task = asyncio.create_task(ui.tui.run())
        ui.task.add_done_callback(lambda _: ui.rendered.set())
        try:
            await ui.wait_render()
            yield ui
        finally:
            await ui.close()


# Executed by `python -c`, without importing this module (which imports cli).
# This preserves real cli.py __main__ dispatch and lazy-import behavior.
PTY_OBSERVER = r'''
import fcntl, json, os, runpy, sys, termios, time
from pathlib import Path
from prompt_toolkit.application import Application

snapshot_path, root, *arguments = sys.argv[1:]
if os.getsid(0) == os.getpid():
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
original_run = Application.run_async
count = 0

def capture(app):
    global count
    screen = app.renderer.last_rendered_screen
    if screen is None:
        return
    # Renderer commits this size together with last_rendered_screen. A fresh
    # ioctl query here can already describe the next resize, mixing two frames.
    size = app.renderer._last_size
    if size is None:
        return
    count += 1
    cells = [[screen.data_buffer[y][x].char for x in range(size.columns)]
             for y in range(size.rows)]
    current = app.layout.current_control
    owner = getattr(getattr(current, 'text', None), '__self__', None)
    caption = getattr(owner, 'text', None)
    # Button captions are strings; other controls can expose a text method.
    # Focusing those controls must not break the observer's JSON snapshot.
    if not isinstance(caption, str):
        caption = None
    cursor = screen.get_cursor_position(app.layout.current_window)
    record = dict(count=count, time=time.monotonic(), columns=size.columns, rows=size.rows,
                  cells=cells, text='\n'.join(''.join(row).rstrip() for row in cells),
                  cursor=[cursor.x, cursor.y], buffer=app.current_buffer.text,
                  buffer_cursor=app.current_buffer.cursor_position,
                  focus_caption=caption, source='pty-renderer')
    temp = Path(snapshot_path + '.new')
    temp.write_text(json.dumps(record, ensure_ascii=False))
    temp.replace(snapshot_path)

async def observed_run(self, *args, **kwargs):
    self.after_render += capture
    try:
        return await original_run(self, *args, **kwargs)
    finally:
        self.after_render -= capture

Application.run_async = observed_run
sys.path.insert(0, root)
sys.argv = [str(Path(root) / 'cli.py'), *arguments]
try:
    runpy.run_path(sys.argv[0], run_name='__main__')
finally:
    print('QA_TERMINAL_RETURNED', flush=True)
'''


class PtyTerminal:
    """Owned controlling tty with real VT bytes; no fake input/output objects."""

    def __init__(self, command, *, env, cwd, size=(120, 30)):
        import os
        import pty
        import subprocess
        import termios
        import threading
        from contextlib import ExitStack
        from tests._cli_server import stop_process

        self.cleanup = ExitStack()
        self.process = None
        self.raw = bytearray()
        self.stopped = threading.Event()
        self.changed = threading.Event()
        self.lock = threading.Lock()
        try:
            self.master, self.slave = pty.openpty()
            self.cleanup.callback(os.close, self.slave)
            self.cleanup.callback(os.close, self.master)
            self.before_termios = termios.tcgetattr(self.slave)
            self.resize(*size, notify=False)
            self.reader = threading.Thread(target=self._drain, daemon=True)
            self.cleanup.callback(self._stop_reader)
            # Register closure before launch; child is assigned only after Popen.
            self.cleanup.callback(lambda: stop_process(self.process)
                                  if self.process is not None else None)
            self.process = subprocess.Popen(command, stdin=self.slave, stdout=self.slave,
                                            stderr=self.slave, env=env, cwd=cwd,
                                            start_new_session=True)
            self.reader.start()
        except BaseException:
            self.cleanup.close()
            raise

    def _stop_reader(self):
        self.stopped.set()
        if self.reader.ident is not None:
            self.reader.join(timeout=2)

    def _drain(self):
        import os
        import select
        pending = b''
        while not self.stopped.is_set():
            if not select.select([self.master], [], [], .05)[0]:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            with self.lock:
                self.raw.extend(chunk)
                if len(self.raw) > 2 * 1024 * 1024:
                    del self.raw[:-2 * 1024 * 1024]
            # Answer terminal CPR requests without bypassing input decoding.
            pending += chunk
            while b'\x1b[6n' in pending:
                _, pending = pending.split(b'\x1b[6n', 1)
                os.write(self.master, b'\x1b[1;1R')
            pending = pending[-4:]
            self.changed.set()

    def send(self, text):
        import os
        data = text.encode() if isinstance(text, str) else text
        while data:
            written = os.write(self.master, data)
            data = data[written:]

    def key(self, name):
        self.send(TuiHarness.sequences[name])

    def paste(self, text):
        self.send('\x1b[200~' + text + '\x1b[201~')

    def resize(self, columns, rows, *, notify=True):
        import fcntl
        import os
        import signal
        import struct
        import termios
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
        if notify and self.process is not None:
            os.killpg(self.process.pid, signal.SIGWINCH)

    def output(self):
        return self.output_bytes().decode('utf-8', errors='replace')

    def output_bytes(self):
        with self.lock:
            return bytes(self.raw)

    def wait(self, predicate, *, timeout=15):
        import re
        from time import monotonic
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            value = predicate()
            if value:
                return value
            self.changed.wait(min(.05, max(0, deadline - monotonic())))
            self.changed.clear()
        output = re.sub(r'([?&]token=)[^\s&\"\'<>]+', r'\1[REDACTED]', self.output(),
                        flags=re.IGNORECASE)
        output = re.sub(r'(Session token:\s*)\S+', r'\1[REDACTED]', output,
                        flags=re.IGNORECASE)
        raise AssertionError('PTY condition timed out; terminal tail:\n' + output[-3000:])

    def snapshot(self, path):
        import json
        from pathlib import Path
        try:
            return json.loads(Path(path).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def screen(self, path, predicate=lambda value: True, *, timeout=15):
        return self.wait(lambda: (value if (value := self.snapshot(path))
                                 and predicate(value) else None), timeout=timeout)

    def close(self):
        self.cleanup.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
