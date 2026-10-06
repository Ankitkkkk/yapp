"""Awaitable, terminal-safe dialogs hosted by one running Application."""

import asyncio
import shlex
import sys
from bisect import bisect_right
from dataclasses import dataclass, replace
from pathlib import Path

from prompt_toolkit.completion import Completion, PathCompleter, ThreadedCompleter
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.application.current import get_app
from prompt_toolkit.keys import Keys
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import (ConditionalContainer, Float, FloatContainer,
                                   HSplit, ScrollablePane, VSplit, Window)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import walk
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.widgets import Button, Dialog, Label, TextArea

from cli_api import CLIError
from provider_args import parse_provider_flags
from cli_tui_state import body_text, label_text
from cli_view_contracts import ActionOutcome
from cli_workspace_chat import _agent_cwd, _agent_line, _agent_label, SESSION_HELP
from cli_workspaces import WINDOWS_TMUX_ERROR
from agent_profiles import ROLE_CHOICES, PERSONALITY_CHOICES

_NAVIGATION_BUSY = object()
_CHOICE_BACK = object()
_ADD_AGENT = object()


def _style_input(control, *, read_only=False):
    """Keep field focus visible independently of the terminal's own palette."""
    base_style = control.window.style
    control.window.style = lambda: base_style + ' ' + (
        'class:dialog.input.readonly' if read_only else
        'class:dialog.input.focused' if has_focus(control)() else 'class:dialog.input')


@dataclass(frozen=True)
class ModalResult:
    value: object = None
    cancelled: bool = False


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    default: str = ''
    choices: tuple = ()
    required: bool = False
    read_only: bool = False
    directory: bool = False


class _DirectoryCompleter(PathCompleter):
    """Suggest directories with safe labels while preserving raw path values."""

    def __init__(self):
        super().__init__(only_directories=True)

    def get_completions(self, document, complete_event):
        # Completing a prefix in the middle would duplicate the remaining path.
        if document.text_after_cursor:
            return
        for completion in super().get_completions(document, complete_event):
            yield Completion(completion.text, start_position=completion.start_position,
                             display=label_text(completion.display_text))


class _ChoiceInput:
    """One-line choice with keyboard cycling and clickable previous/next arrows."""

    def __init__(self, choices, default='', *, read_only=False):
        self.choices = tuple(choices)
        self.current_value = default if default in self.choices else self.choices[0]
        self.read_only = read_only
        bindings = KeyBindings()
        bindings.add(' ')(lambda event: self.cycle(1))
        self.control = FormattedTextControl(self._fragments, focusable=True,
                                            key_bindings=bindings)
        self.window = Window(self.control, height=1, dont_extend_height=True)
        _style_input(self, read_only=read_only)

    def cycle(self, offset):
        if not self.read_only:
            index = (self.choices.index(self.current_value) + offset) % len(self.choices)
            self.current_value = self.choices[index]

    def _mouse(self, event, offset=0):
        if event.event_type != MouseEventType.MOUSE_UP:
            return NotImplemented
        get_app().layout.focus(self)
        self.cycle(offset)

    def _fragments(self):
        index = self.choices.index(self.current_value) + 1
        return [('', '‹ ', lambda event: self._mouse(event, -1)),
                ('[SetCursorPosition]', ''),
                ('', label_text(self.current_value), self._mouse),
                ('', ' ›', lambda event: self._mouse(event, 1)),
                ('', f'  {index}/{len(self.choices)}', self._mouse)]

    def __pt_container__(self):
        return self.window


class _SafeInput(Processor):
    """Sanitize displayed input without changing its submitted buffer value."""

    def apply_transformation(self, transformation_input):
        fragments = []
        offsets = [0]
        for style, text, *extra in transformation_input.fragments:
            for character in text:
                visible = body_text(character) if character == '\t' else label_text(character)
                fragments.append((style, visible, *extra))
                offsets.append(offsets[-1] + len(visible))
        return Transformation(
            fragments,
            source_to_display=lambda position: offsets[min(position, len(offsets) - 1)],
            display_to_source=lambda position: max(0, bisect_right(offsets, position) - 1),
        )


class DialogHost:
    """Own one modal waiter and restore focus when that waiter finishes."""

    def __init__(self, app_getter, invalidate, *, owner_is_short_lived=None):
        self.app_getter = app_getter
        self.invalidate = invalidate
        self.owner_is_short_lived = owner_is_short_lived or (lambda owner: False)
        self.future = None
        self._owner = None
        self.float = None
        self._empty = Window(height=0, width=0)
        self._saved_focus = None
        self._cancel_value = None

    @property
    def owner(self):
        """Task owning the current modal; capture together with its Future."""
        return self._owner

    @property
    def body(self):
        return self.float if self.float is not None else self._empty

    def _bindings(self, cancel_value):
        bindings = KeyBindings()

        # A queued continuation belongs to Alt editing. Let native multi-key
        # bindings consume it; only a lone Escape bypasses timeoutlen.
        @bindings.add('escape', eager=Condition(lambda: not get_app().key_processor.input_queue))
        @bindings.add('c-c', eager=True)
        def cancel(event):
            self.finish(cancel_value)

        @bindings.add('escape', Keys.Any)
        def unknown_alt(event):
            # Specific native Alt bindings outrank this wildcard fallback.
            # Unknown Alt sequences must not cancel and leak into the composer.
            pass

        bindings.add('tab')(focus_next)
        bindings.add('s-tab')(focus_previous)
        return bindings

    async def _open(self, content, bindings, focus, cancel_value):
        if self.future is not None:
            return cancel_value
        app = self.app_getter()
        if app is None or not app.is_running:
            return cancel_value
        future = asyncio.get_running_loop().create_future()
        self.future = future
        self._owner = asyncio.current_task()
        self._saved_focus = app.layout.current_control
        self._cancel_value = cancel_value
        # Cursor-relative root floats draw near 10**8. The completion window
        # must paint after the modal itself, including its hint and buttons.
        self.float = FloatContainer(content=content, modal=True, key_bindings=bindings,
            floats=[Float(xcursor=True, ycursor=True,
                          content=CompletionsMenu(max_height=6, scroll_offset=1,
                                                  z_index=10**9))])
        try:
            app.layout.update_parents_relations()
            app.layout.focus(focus)
            self.invalidate()
            return await future
        finally:
            if self.future is future:
                self.finish(cancel_value)

    def restore_focus(self):
        saved, self._saved_focus = self._saved_focus, None
        app = self.app_getter()
        if app is None or not app.is_running:
            return
        layout = app.layout
        controls = [container.content for container in walk(layout.container, skip_hidden=True)
                    if isinstance(container, Window)]
        if saved in controls and saved.is_focusable():
            layout.focus(saved)
        else:
            for control in controls:
                if control.is_focusable():
                    layout.focus(control)
                    break

    def finish(self, value):
        future, self.future = self.future, None
        if future is None:
            return
        self._owner = None
        self.float = None
        self._cancel_value = None
        try:
            self.restore_focus()
            self.invalidate()
        finally:
            if not future.done():
                future.set_result(value)

    def cancel(self):
        """Cancel any open dialog, including during application shutdown."""
        self.finish(self._cancel_value)

    async def confirm(self, text, *, default=False, escape=False):
        """Return bool; escape MUST mean non-mutation (No/Chat only/stay).

        A busy second confirmation returns its escape answer immediately.
        """
        bindings = self._bindings(escape)

        @bindings.add('y')
        @bindings.add('Y')
        def yes(event):
            self.finish(True)

        @bindings.add('n')
        @bindings.add('N')
        def no(event):
            self.finish(False)

        yes_button = Button('Yes', handler=lambda: self.finish(True))
        no_button = Button('No', handler=lambda: self.finish(False))
        dialog = Dialog(title='Confirm', body=Label(body_text(text)),
                        buttons=[yes_button, no_button], modal=False)
        return await self._open(dialog, bindings,
                                yes_button if default else no_button, escape)

    async def form(self, title, fields, *, submit_label, error=None, description=None):
        """Collect raw field values; show validation errors until resubmitted."""
        cancelled = ModalResult(cancelled=True)
        if self.future is not None:
            return cancelled
        fields = tuple(fields)
        controls = {}
        # Put refusals ahead of the editable fields: optional context below the
        # fields can be clipped on short terminals, hiding a failed submission.
        error_label = Label(body_text(error) if error else '', style='class:dialog.error')
        rows = []
        choice_label_width = max((get_cwidth(label_text(field.label)) + 2
                                  for field in fields if field.choices), default=20)
        for field in fields:
            if field.choices:
                control = _ChoiceInput(field.choices, field.default, read_only=field.read_only)
                rows.append(VSplit([Label(label_text(field.label), width=choice_label_width), control]))
            else:
                control = TextArea(text=field.default, multiline=False, height=1,
                                   read_only=field.read_only,
                                   focus_on_click=True,
                                   completer=(ThreadedCompleter(_DirectoryCompleter())
                                              if field.directory and not field.read_only else None),
                                   complete_while_typing=field.directory and not field.read_only,
                                   input_processors=[_SafeInput()])
                _style_input(control, read_only=field.read_only)
                if field.directory:
                    control.buffer.cursor_position = len(control.text)
                rows.append(HSplit([Label(label_text(field.label)), control]))
            controls[field.name] = control
        if description:
            rows.append(Label(body_text(description)))

        def submit():
            values = {name: (control.current_value if isinstance(control, _ChoiceInput)
                             else control.text) for name, control in controls.items()}
            for field in fields:
                if field.required and not values[field.name].strip():
                    error_label.text = label_text(field.label).rstrip(':') + ' is required'
                    self.app_getter().layout.focus(controls[field.name])
                    self.invalidate()
                    return
            self.finish(ModalResult(values))

        submit_button = Button(label_text(submit_label),
                               width=max(12, get_cwidth(label_text(submit_label)) + 4), handler=submit)
        cancel_button = Button('Cancel', handler=self.cancel)
        bindings = self._bindings(cancelled)
        focus_order = [controls[field.name] for field in fields if not field.read_only]
        focus_order.extend([submit_button, cancel_button])

        @bindings.add('up', eager=True)
        @bindings.add('down', eager=True)
        def move_field(event):
            # Keep the selected completion when leaving; arrows always move
            # between fields, even while the directory menu is open.
            event.current_buffer.complete_state = None
            offset = -1 if event.key_sequence[-1].key == 'up' else 1
            current = next((i for i, control in enumerate(focus_order)
                            if event.app.layout.has_focus(control)), None)
            target = (current + offset) % len(focus_order) if current is not None else (
                0 if offset > 0 else len(focus_order) - 1)
            event.app.layout.focus(focus_order[target])

        @bindings.add('tab', eager=True)
        @bindings.add('s-tab', eager=True)
        def cycle_value(event):
            backwards = event.key_sequence[-1].key == 's-tab'
            field = next((field for field in fields
                          if event.app.layout.has_focus(controls[field.name])), None)
            if field is None or field.read_only:
                return
            control = controls[field.name]
            if isinstance(control, _ChoiceInput):
                control.cycle(-1 if backwards else 1)
            elif field.directory:
                buffer = control.buffer
                if buffer.complete_state:
                    if backwards:
                        buffer.complete_previous()
                    else:
                        buffer.complete_next()
                else:
                    buffer.start_completion(select_first=not backwards,
                                            select_last=backwards)

        @bindings.add('enter', filter=~has_focus(cancel_button), eager=True)
        def accept(event):
            completion = event.current_buffer.complete_state
            if completion is not None and completion.current_completion is not None:
                event.current_buffer.complete_state = None
                return
            submit()

        # Collapse spacing on short terminals. Validation and actions remain
        # outside the scrollable fields, even when an error needs extra lines.
        field_rows = HSplit(rows, padding=lambda: (
            1 if self.app_getter().output.get_size().rows >= 24 else 0))
        body = HSplit([
            ConditionalContainer(error_label, filter=Condition(lambda: bool(error_label.text))),
            ScrollablePane(field_rows, show_scrollbar=False),
            Label('↑↓ fields · Tab/Shift+Tab choices/paths · Enter submit · Esc cancel')])
        dialog = Dialog(title=label_text(title), body=body,
                        buttons=[submit_button, cancel_button], modal=False,
                        width=Dimension(preferred=76, max=88))
        first = next((controls[field.name] for field in fields if not field.read_only), submit_button)
        return await self._open(dialog, bindings, first, cancelled)

    async def choose(self, title, choices, *, searchable=True, cancel_label='Cancel',
                     back_key=None, extra_buttons=()):
        """Choose mapping ID; descriptions and disabled reasons stay visible."""
        cancelled = ModalResult(cancelled=True)
        if self.future is not None:
            return cancelled
        choices = tuple(choices)
        visible = list(choices)
        selected_id = visible[0]['id'] if visible else None
        search = TextArea(multiline=False, height=1, prompt='Search: ',
                          input_processors=[_SafeInput()])
        _style_input(search)

        def render_rows():
            fragments = []
            for choice in visible:
                selected = choice['id'] == selected_id
                if selected:
                    fragments.append(('[SetCursorPosition]', ''))
                style = 'class:dialog.choice.selected' if selected else ''
                text = ('> ' if selected else '  ') + label_text(choice['label'])
                if choice.get('description'):
                    text += ' — ' + label_text(choice['description'])
                if choice.get('disabled_reason'):
                    text += ' (' + label_text(choice['disabled_reason']) + ')'
                fragments.append((style, text + '\n'))
            return fragments or [('', 'No matching choices')]

        def refilter(buffer):
            nonlocal visible, selected_id
            query = label_text(buffer.text).casefold()
            visible = [choice for choice in choices if query in ' '.join(
                label_text(choice.get(key, '')) for key in ('id', 'label', 'description')
            ).casefold()]
            if selected_id not in [choice['id'] for choice in visible]:
                selected_id = visible[0]['id'] if visible else None
            self.invalidate()

        search.buffer.on_text_changed += refilter
        rows = Window(FormattedTextControl(render_rows, focusable=True),
                      height=Dimension(min=1, max=10), wrap_lines=True)

        def submit():
            choice = next((choice for choice in visible if choice['id'] == selected_id), None)
            if choice is not None and not choice.get('disabled_reason'):
                self.finish(ModalResult(choice['id']))

        user_cancel = ModalResult(_CHOICE_BACK, cancelled=True) if back_key else cancelled
        select_button = Button('Select', handler=submit)
        cancel_button = Button(cancel_label, handler=lambda: self.finish(user_cancel))
        action_buttons = [Button(label_text(label), handler=lambda value=value:
                                 self.finish(ModalResult(value))) for label, value in extra_buttons]
        buttons = [select_button, *action_buttons, cancel_button]
        button_focus = Condition(lambda: self.app_getter().layout.current_control in
                                 [button.control for button in buttons])
        bindings = self._bindings(user_cancel)
        if back_key is not None:
            @bindings.add(back_key, eager=True)
            def back(event):
                self.finish(user_cancel)

        @bindings.add('left', filter=button_focus | has_focus(rows), eager=True)
        @bindings.add('right', filter=button_focus | has_focus(rows), eager=True)
        def move_button(event):
            current = event.app.layout.current_control
            controls = [button.control for button in buttons]
            if current in controls:
                offset = -1 if event.key_sequence[-1].key == 'left' else 1
                index = (controls.index(current) + offset) % len(buttons)
            else:
                index = 0
            event.app.layout.focus(buttons[index])

        @bindings.add('up', eager=True)
        @bindings.add('down', eager=True)
        def move(event):
            nonlocal selected_id
            down = event.key_sequence[-1].key == 'down'
            if button_focus():
                if not down:
                    event.app.layout.focus(rows if visible or not searchable else search)
                return
            if not visible:
                event.app.layout.focus(select_button if down else search if searchable else rows)
                return
            ids = [choice['id'] for choice in visible]
            index = ids.index(selected_id)
            if down and index == len(ids) - 1:
                event.app.layout.focus(select_button)
            elif not down and index == 0 and searchable:
                event.app.layout.focus(search)
            else:
                selected_id = ids[max(0, min(len(ids) - 1, index + (1 if down else -1)))]
                event.app.layout.focus(rows)

        @bindings.add('enter', filter=has_focus(search) | has_focus(rows) | has_focus(select_button), eager=True)
        def accept(event):
            submit()

        content = HSplit([search, rows] if searchable else [rows])
        dialog = Dialog(title=label_text(title), body=content,
                        buttons=buttons, modal=False)
        return await self._open(dialog, bindings, search if searchable else rows, cancelled)


class TuiWorkflows:
    """Gather user intent, then call typed controller/client operations once."""

    def __init__(self, client, controller, view, dialogs, state, *, composer_actions):
        self.client, self.controller, self.view = client, controller, view
        self.dialogs, self.state = dialogs, state
        self.composer_actions = composer_actions
        self.include_archived = False
        self._active = set()
        self._list_request = 0

    def notice(self, text):
        self.state.notices.add(text)
        self.dialogs.invalidate()

    async def _dialog(self, method, *args, **kwargs):
        # Busy calls retain the existing modal and Help. No await separates
        # hiding Help from constructing/focusing the next actual dialog.
        if self.dialogs.future is None:
            self.view.hide_help()
        return await getattr(self.dialogs, method)(*args, **kwargs)

    def _scope(self):
        return ((self.controller.workspace or {}).get('id'), self.controller.selection_generation)

    def _unchanged(self, scope):
        return scope == self._scope()

    def _cancelled_selection(self):
        message = 'Selection changed; action cancelled'
        self.notice(message)
        return ActionOutcome('cancelled', message)

    def _admit(self, key, mandatory=False):
        if self.state.drafts.can_open(key, mandatory=mandatory):
            return True
        self.notice(self.state.drafts.capacity_notice)
        return False

    async def confirm_selection(self, text, *, workspace, default=False, escape=False):
        if self.dialogs.future is not None:
            return escape
        self.view.hide_help()
        # The controller supplies its actual candidate, including unarchive changes.
        lines = ['Session: ' + label_text(workspace.get('name') or workspace['id']),
                 label_text(workspace['id']) + (' (archived)' if workspace.get('archived') else '')]
        for agent in workspace.get('agents', []):
            if isinstance(agent, dict) and agent.get('last_state') == 'exited':
                prefix = 'Eligible: ' if _agent_cwd(agent) is not None else 'Skipped: '
                lines.append(prefix + _agent_line(agent))
        lines.extend(['', text])
        return await self._dialog('confirm', '\n'.join(lines), default=default, escape=escape)

    async def refresh_sessions(self):
        """Refresh navigation metadata; errors retain old rows and publish once."""
        self._list_request += 1
        request = self._list_request
        self.view.set_sessions_loading(True)
        try:
            response = await self.controller.list_sessions(include_archived=self.include_archived)
            if request != self._list_request:
                return ActionOutcome('cancelled')
            if (not isinstance(response, dict) or not isinstance(response.get('workspaces'), list)
                    or any(not isinstance(row, dict) or not isinstance(row.get('id'), str)
                           or not row['id'] for row in response['workspaces'])):
                raise CLIError('Invalid session list response; retry Refresh.')
            self.view.set_sessions(response['workspaces'])
            if response.get('warning'):
                self.notice(response['warning'])
            return ActionOutcome('completed')
        except (CLIError, OSError, TimeoutError) as error:
            if request != self._list_request:
                return ActionOutcome('cancelled')
            message = str(error) if isinstance(error, CLIError) else 'Session list unavailable; retry Refresh.'
            self.view.set_sessions_error(message)
            return ActionOutcome('failed', message)
        finally:
            if request == self._list_request:
                self.view.set_sessions_loading(False)

    async def _navigation(self):
        """Specialized modal, sharing DialogHost cancellation and focus semantics."""
        cancelled = ModalResult(cancelled=True)
        if self.dialogs.future is not None:
            return ModalResult(_NAVIGATION_BUSY)
        search = TextArea(text=self.state.search, multiline=False, height=1, prompt='Search: ',
                          input_processors=[_SafeInput()])
        _style_input(search)
        tasks = set()
        owner = asyncio.current_task()
        fetch_error = None
        loading = False

        def visible():
            query = search.text.casefold()
            return [row for row in self.view.session_rows() if query in
                    (str(row.get('name') or '') + ' ' + row['id']).casefold()]

        def refilter(buffer):
            self.state.search = buffer.text
            ids = [row['id'] for row in visible()]
            if self.state.selected_session_id not in ids:
                self.state.selected_session_id = next(iter(ids), None)
            self.dialogs.invalidate()
        search.buffer.on_text_changed += refilter

        def choose(ident):
            if not loading:
                self.state.selected_session_id = ident
                self.dialogs.finish(ModalResult(ident))

        def fragments():
            rows = []
            for row in visible():
                ident = row['id']
                if ident == self.state.selected_session_id:
                    rows.append(('[SetCursorPosition]', ''))
                def mouse(event, ident=ident):
                    if event.event_type == MouseEventType.MOUSE_UP:
                        choose(ident)
                text = ('> ' if ident == self.state.selected_session_id else '  ')
                text += label_text(row.get('name') or ident) + ' [' + label_text(ident) + ']'
                if row.get('archived'):
                    text += ' (archived)'
                rows.append(('class:dialog.choice.selected' if ident == self.state.selected_session_id else '',
                             text + '\n', mouse))
            return rows or [('', 'No matching sessions')]

        rows = Window(FormattedTextControl(fragments, focusable=True),
                      height=Dimension(min=1, max=10), wrap_lines=True)
        status = Label(lambda: 'Loading sessions…' if loading else
                       'Session list stale; retry Refresh.' if self.view.sessions_stale else '')

        async def fetch():
            nonlocal loading
            loading = True
            self.dialogs.invalidate()
            try:
                await self.refresh_sessions()
                refilter(search.buffer)
            finally:
                loading = False
                self.dialogs.invalidate()

        def fetched(task):
            nonlocal fetch_error
            tasks.discard(task)
            if task.cancelled():
                return
            error = task.exception()  # Retrieve before releasing this read's owner.
            if error is not None:
                if fetch_error is None:
                    fetch_error = error
                # Wake only our modal. _open still owns focus/restoration; a
                # replacement or already-finished modal keeps its own result.
                future = self.dialogs.future
                if self.dialogs.owner is owner and future is not None and not future.done():
                    future.set_exception(error)

        def refresh():
            if loading or tasks:
                self.notice('Loading sessions… Refresh is already in progress.')
                return
            task = asyncio.create_task(fetch())
            tasks.add(task)
            task.add_done_callback(fetched)

        def toggle():
            if loading or tasks:
                self.notice('Loading sessions… Wait before changing Show archived.')
                return
            self.include_archived = not self.include_archived
            archived.text = 'Show archived [' + ('on' if self.include_archived else 'off') + ']'
            refresh()

        new = Button('New session', width=15, handler=lambda: self.dialogs.finish(ModalResult('__new__')))
        archived = Button('Show archived [' + ('on' if self.include_archived else 'off') + ']',
                          width=25, handler=toggle)
        def more_actions():
            if self.controller.workspace is None:
                self.notice('Select a session first')
            else:
                self.dialogs.finish(ModalResult('__actions__'))
        more = Button('More actions', width=16, handler=more_actions)
        more_reason = Label(lambda: 'More actions: Select a session first'
                            if self.controller.workspace is None else '')
        refresh_button = Button('Refresh', handler=refresh)
        cancel = Button('Cancel', handler=self.dialogs.cancel)
        bindings = self.dialogs._bindings(cancelled)

        @bindings.add('up', eager=True)
        @bindings.add('down', eager=True)
        def move(event):
            ids = [row['id'] for row in visible()]
            if ids:
                selected = self.state.selected_session_id
                index = ids.index(selected) if selected in ids else 0
                offset = -1 if event.key_sequence[-1].key == 'up' else 1
                self.state.selected_session_id = ids[max(0, min(len(ids) - 1, index + offset))]

        @bindings.add('enter', filter=has_focus(search) | has_focus(rows), eager=True)
        def select(event):
            if self.state.selected_session_id in [row['id'] for row in visible()]:
                choose(self.state.selected_session_id)

        dialog = Dialog(title='Sessions', body=HSplit([search, status, rows, more_reason,
                        VSplit([more, refresh_button, cancel], padding=1)]),
                        buttons=[new, archived], modal=False)
        refresh()
        try:
            return await self._dialog('_open', dialog, bindings, search, cancelled)
        finally:
            pending = tuple(tasks)
            for task in pending:
                task.cancel()
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            finally:
                if fetch_error is not None:
                    raise fetch_error

    async def navigate(self, mandatory=False):
        generation = self.controller.selection_generation
        def committed_elsewhere():
            return (self.controller.workspace is not None
                    and self.controller.selection_generation != generation)

        if self.controller.plain_channel:
            return await self._menu('Channel actions', {'create_channel', 'switch_channel', 'history'},
                                    None, self._scope())
        while True:
            if committed_elsewhere():
                return ActionOutcome('completed', workspace_id=self.controller.workspace['id'])
            result = await self._navigation()
            if result.value is _NAVIGATION_BUSY:
                if not mandatory and self.controller.workspace is not None:
                    return ActionOutcome('cancelled')
                # Modal completion precedes its owner's submitted HTTP work.
                # Observe both lifetimes without inheriting their cancellation.
                future, owner = self.dialogs.future, self.dialogs.owner
                await asyncio.wait({future})
                if committed_elsewhere():
                    continue
                if (owner is not None and owner is not asyncio.current_task()
                        and self.dialogs.owner_is_short_lived(owner)):
                    await asyncio.wait({owner})
                    if committed_elsewhere():
                        continue
                await self.controller.wait_selection()
                continue
            if result.cancelled:
                if mandatory or self.controller.workspace is None:
                    await self.view.callbacks['quit']()
                return ActionOutcome('cancelled')
            if result.value == '__new__':
                outcome = await self.new_session(mandatory=mandatory)
            elif result.value == '__actions__':
                scope = self._scope()
                name = (self.controller.workspace or {}).get('name') or scope[0] or 'No active session'
                outcome = await self._menu('Session actions: ' + label_text(name),
                                           {'rename_session', 'archive_session', 'refresh'}, scope[0], scope)
                if outcome.status == 'completed' and not self._unchanged(scope):
                    return outcome
                if self.controller.workspace is None:
                    return ActionOutcome('cancelled')
                continue
            else:
                outcome = await self._select(result.value, mandatory=mandatory)
            if outcome.status == 'completed':
                return outcome
            if outcome.message in ('Archived session was not selected',
                                   'Session selection already in progress.'):
                self.notice(outcome.message)

    async def _select(self, ident, *, mandatory=False):
        if not self._admit(('session', ident), mandatory):
            return ActionOutcome('cancelled')
        scope = self._scope()
        outcome = await self.controller.select_session(ident)
        if outcome.status == 'completed':
            if (self.controller.workspace or {}).get('id') != ident:
                return self._cancelled_selection()
            self.state.selected_session_id = ident
            self.composer_actions.switch_draft(self.composer_actions.destination_key(), mandatory=True)
            if not self._unchanged(scope):
                self.state.selected_agent_id = None
                self.state.viewport.mark_seen()
            await self.refresh_sessions()
        return outcome

    async def new_session(self, *, mandatory=False):
        # A fresh server ID cannot already own a draft.
        if not self._admit(('new_session', None), mandatory):
            return ActionOutcome('cancelled')
        if not self.controller.providers:
            return self._failure('No providers configured')
        scope = self._scope()
        name_fields = [Field('name', 'Session name:')]
        last_cwd = next((a.get('cwd') for a in reversed(self.view.agent_rows()) if a.get('cwd')), None)
        orchestrator_fields = [
            Field('provider', 'Orchestrator provider:', choices=tuple(self.controller.providers), required=True),
            Field('cwd', 'Working directory:', default=last_cwd or str(Path.cwd()),
                  required=True, directory=True),
            Field('provider_flags', 'Provider flags:'),
        ]
        error = None
        while True:
            identity = await self._dialog('form', 'New session · 1 of 2', name_fields,
                                          submit_label='Next', error=error)
            if identity.cancelled:
                return ActionOutcome('cancelled')
            if not self._unchanged(scope):
                return self._cancelled_selection()
            details = await self._dialog('form', 'New session · 2 of 2', orchestrator_fields,
                                         submit_label='Create session', error=error,
                                         description='Orchestrator remains resident for this session.')
            if details.cancelled:
                return ActionOutcome('cancelled')
            if not self._unchanged(scope):
                return self._cancelled_selection()
            if not self._admit(('new_session', None), mandatory):
                return ActionOutcome('cancelled')
            try:
                provider_args = parse_provider_flags(details.value.get('provider_flags', ''))
            except ValueError as exc:
                error = str(exc)
                name_fields = [replace(f, default=identity.value[f.name]) for f in name_fields]
                orchestrator_fields = [replace(f, default=details.value[f.name]) for f in orchestrator_fields]
                continue
            cwd = details.value['cwd']
            if not Path(cwd).is_absolute() or not Path(cwd).is_dir():
                error = 'Working directory must be an absolute existing directory.'
                name_fields = [replace(f, default=identity.value[f.name]) for f in name_fields]
                orchestrator_fields = [replace(f, default=details.value[f.name]) for f in orchestrator_fields]
                continue
            orchestrator = {'provider': details.value['provider'], 'cwd': cwd}
            if provider_args:
                orchestrator['provider_args'] = provider_args
            payload = {'name': identity.value['name'], 'orchestrator': orchestrator}
            outcome = await self.controller.execute_action('create_session', payload)
            if outcome.status != 'failed':
                break
            error = outcome.message
            name_fields = [replace(f, default=identity.value[f.name]) for f in name_fields]
            orchestrator_fields = [replace(f, default=details.value[f.name]) for f in orchestrator_fields]
        if outcome.status == 'completed':
            await self.refresh_sessions()
            return await self._select(outcome.workspace_id, mandatory=mandatory)
        return outcome

    async def _choose_agent(self, agent_id, *, allow_add=False):
        rows = self.view.agent_rows()
        if agent_id is not None:
            return agent_id if any(a['agent_id'] == agent_id for a in rows) else None
        result = await self._dialog('choose', 'Choose agent', [dict(id=a['agent_id'],
            label=_agent_label(a), description=_agent_line(a)) for a in rows],
            extra_buttons=(('Add agent', _ADD_AGENT),) if allow_add else ())
        return None if result.cancelled else result.value

    async def agent_form(self, action, agent_id=None, *, _scope=None):
        scope = self._scope() if _scope is None else _scope
        if self.controller.workspace is None:
            return self._failure('Select a session first')
        if action in ('spawn', 'resume') and sys.platform == 'win32':
            return self._failure(WINDOWS_TMUX_ERROR)
        if action == 'spawn' and not self.controller.providers:
            return self._failure('No providers configured')
        if action != 'spawn':
            agent_id = await self._choose_agent(agent_id)
            if not self._unchanged(scope):
                return self._cancelled_selection()
            if agent_id is None:
                return ActionOutcome('cancelled')
        agent = next((a for a in self.view.agent_rows() if a['agent_id'] == agent_id), {})
        if action == 'spawn':
            last_cwd = next((a['cwd'] for a in reversed(self.view.agent_rows()) if a.get('cwd')), None)
            fields = [Field('provider', 'Provider:', choices=tuple(self.controller.providers), required=True),
                      Field('cwd', 'Working directory:', default=last_cwd or str(Path.cwd()),
                            required=True, directory=True),
                      Field('name', 'Agent name:'),
                      Field('history_mode', 'History mode:', default='literal',
                            choices=('none', 'literal'), required=True),
                      Field('provider_flags', 'Provider flags:')]
            title, submit = 'New agent · 1 of 2', 'Next'
            profile_fields = [
                Field('role', 'Role:', default='generalist', choices=ROLE_CHOICES, required=True),
                Field('personality', 'Personality:', default='pragmatic',
                      choices=PERSONALITY_CHOICES, required=True),
            ]
        elif action == 'resume':
            fields = [Field('cwd', 'Working directory (read-only):', default=agent.get('cwd', ''), read_only=True),
                      Field('name', 'Agent name (blank keeps stored):'),
                      Field('launch_mode', 'Launch mode:', default='ordinary', choices=('ordinary', 'fresh')),
                      Field('provider_flags', 'Provider flags:', default=shlex.join(agent.get('provider_args', [])))]
            title, submit = 'Resume agent', 'Resume agent'
        elif action == 'history':
            fields = [Field('mode', 'History mode:', default=agent.get('history_mode', 'literal'),
                            choices=('literal', 'none'), required=True)]
            title, submit = 'History settings', 'Apply history mode'
        else:
            return self._failure('Unsupported agent form')
        error = None
        while True:
            context = ''
            if action != 'spawn':
                current = next((a for a in self.view.agent_rows() if a['agent_id'] == agent_id), agent)
                context = '\n'.join([_agent_line(current),
                    'History: ' + str(current.get('history_state') or 'unknown'),
                    str(current.get('history_note') or '')])
                if action == 'resume':
                    profile = current.get('profile') or {}
                    locked = (('Locked profile: ' + str(profile['role']) + ' · '
                               + str(profile['personality'])) if profile else
                              'Legacy agent — no saved profile')
                    context += '\n' + locked + '\nResume automatically uses the saved working directory.'
            result = await self._dialog('form', title, fields, submit_label=submit,
                                       error=error, description=context or None)
            if result.cancelled:
                return ActionOutcome('cancelled')
            if not self._unchanged(scope):
                return self._cancelled_selection()
            values = result.value
            fields = [replace(f, default=values[f.name]) for f in fields]
            if action in ('spawn', 'resume'):
                try:
                    provider_args = parse_provider_flags(values.get('provider_flags', ''))
                except ValueError as exc:
                    error = str(exc)
                    continue
            cwd = values.get('cwd')
            if action == 'spawn' and cwd and (not Path(cwd).is_absolute() or not Path(cwd).is_dir()):
                error = 'Working directory must be an absolute existing directory.'
                continue
            if action == 'spawn':
                payload = {key: value for key, value in values.items() if key != 'provider_flags'}
                payload['name'] = values['name'] or None
                if provider_args:
                    payload['provider_args'] = provider_args
                profile = await self._dialog('form', 'New agent · 2 of 2', profile_fields,
                    submit_label='Start agent', error=error,
                    description='Role and personality stay locked after startup.')
                if profile.cancelled:
                    return ActionOutcome('cancelled')
                if not self._unchanged(scope):
                    return self._cancelled_selection()
                profile_fields = [replace(field, default=profile.value[field.name])
                                  for field in profile_fields]
                payload.update(profile.value)
            elif action == 'resume':
                fresh = values['launch_mode'] == 'fresh'
                if fresh:
                    accepted = await self._dialog('confirm',
                        'Fresh launch for ' + _agent_label(agent) + '? [y/N]', default=False, escape=False)
                    if not self._unchanged(scope):
                        return self._cancelled_selection()
                    if not accepted:
                        continue
                payload = dict(agent_id=agent_id, fresh=fresh, cwd=None, name=values['name'] or None)
                payload['provider_args'] = provider_args
            else:
                payload = dict(agent_id=agent_id, mode=values['mode'])
            outcome = await self.controller.execute_action(action, payload)
            if outcome.status != 'failed':
                return outcome
            error = outcome.message

    async def loop_guard_form(self):
        try:
            settings = await asyncio.to_thread(self.controller.api.settings)
        except (CLIError, OSError, TimeoutError) as error:
            return self._failure(str(error))
        if not isinstance(settings, dict) or type(settings.get('max_agent_hops')) is not int:
            return self._failure('Server did not provide the current loop guard limit.')
        field = Field('hops', 'Maximum hops (1–50):', default=str(settings['max_agent_hops']), required=True)
        context = 'Applies to all sessions. Use /continue if a conversation is already paused.'
        error = None
        while True:
            result = await self._dialog('form', 'Loop guard', [field], submit_label='Save',
                                       error=error, description=context)
            if result.cancelled:
                return ActionOutcome('cancelled')
            field = replace(field, default=result.value['hops'])
            try:
                hops = int(field.default)
                if not 1 <= hops <= 50:
                    raise ValueError
            except ValueError:
                error = 'Enter a whole number from 1 to 50'
                continue
            outcome = await self.controller.execute_action('set_loop_guard', {'max_agent_hops': hops})
            if outcome.status != 'failed':
                return outcome
            error = outcome.message

    async def orchestrator_form(self, scope):
        if self.controller.workspace is None:
            return self._failure('Select a session first')
        if not self.controller.providers:
            return self._failure('No providers configured')
        rows = self.view.agent_rows()
        current = next((agent for agent in rows if agent.get('kind') == 'orchestrator'), {})
        last_cwd = current.get('cwd') or next((a.get('cwd') for a in reversed(rows) if a.get('cwd')), None)
        fields = [
            Field('provider', 'Orchestrator provider:', default=current.get('provider', ''),
                  choices=tuple(self.controller.providers), required=True),
            Field('cwd', 'Working directory:', default=last_cwd or str(Path.cwd()),
                  required=True, directory=True),
            Field('provider_flags', 'Provider flags:',
                  default=shlex.join(current.get('provider_args', []))),
        ]
        error = None
        while True:
            result = await self._dialog('form', 'Enable orchestrator', fields,
                                        submit_label='Enable', error=error,
                                        description='One resident orchestrator routes new session requests.')
            if result.cancelled:
                return ActionOutcome('cancelled')
            if not self._unchanged(scope):
                return self._cancelled_selection()
            fields = [replace(field, default=result.value[field.name]) for field in fields]
            try:
                provider_args = parse_provider_flags(result.value.get('provider_flags', ''))
            except ValueError as exc:
                error = str(exc)
                continue
            cwd = result.value['cwd']
            if not Path(cwd).is_absolute() or not Path(cwd).is_dir():
                error = 'Working directory must be an absolute existing directory.'
                continue
            payload = {'provider': result.value['provider'], 'cwd': cwd}
            if provider_args:
                payload['provider_args'] = provider_args
            outcome = await self.controller.execute_action('configure_orchestrator', payload)
            if outcome.status != 'failed':
                return outcome
            error = outcome.message

    def _failure(self, message):
        self.notice(message)
        return ActionOutcome('failed', message)

    async def restart_server(self, scope):
        try:
            status = await asyncio.to_thread(self.controller.api.server_status)
        except (CLIError, OSError, TimeoutError) as error:
            return self._failure(str(error) if isinstance(error, CLIError)
                                 else 'Could not check server restart support.')
        if not self._unchanged(scope):
            return self._cancelled_selection()
        if not status['restart_supported']:
            return self._failure(status['reason'] or 'This server cannot restart itself.')
        if status['state'] != 'ready':
            return self._failure('Server is already ' + status['state'] + '; wait before restarting.')
        prompt = ('Restart server? [y/N]\n' + self.client.url
                  + '\nChat and MCP disconnect briefly.\n'
                    'Agent terminals and unsent drafts stay intact.')
        accepted = await self._dialog('confirm', prompt, default=False, escape=False)
        if not self._unchanged(scope):
            return self._cancelled_selection()
        if not accepted:
            return ActionOutcome('cancelled')
        self.notice('Restarting server…')
        return await self.controller.execute_action('restart_server', {
            'instance_id': status['instance_id'], 'confirmed': True})

    async def stop_all_agents(self, scope):
        if sys.platform == 'win32':
            return self._failure(WINDOWS_TMUX_ERROR)
        self.notice('Checking agents across all sessions…')
        try:
            response = await self.controller.list_sessions(include_archived=True)
            if not isinstance(response, dict) or not isinstance(response.get('workspaces'), list):
                raise CLIError('Could not read all sessions; retry Stop all agents.')
            targets = []
            for workspace in response['workspaces']:
                if (not isinstance(workspace, dict) or not isinstance(workspace.get('id'), str)
                        or not workspace['id'] or not isinstance(workspace.get('agents'), list)):
                    raise CLIError('Incomplete session list; retry Stop all agents.')
                for agent in workspace['agents']:
                    if (not isinstance(agent, dict) or not isinstance(agent.get('agent_id'), str)
                            or not agent['agent_id']):
                        raise CLIError('Incomplete agent list; retry Stop all agents.')
                    launch = agent.get('last_launch') or {}
                    if not isinstance(launch, dict) or (launch.get('nonce') is not None
                                                        and not isinstance(launch['nonce'], str)):
                        raise CLIError('Incomplete launch identity; retry Stop all agents.')
                    targets.append((agent.get('kind') != 'orchestrator', workspace['id'],
                                    agent['agent_id'], launch.get('nonce')))
            targets = tuple(item[1:] for item in sorted(dict.fromkeys(targets)))
        except (CLIError, OSError, TimeoutError) as error:
            return self._failure(str(error) if isinstance(error, CLIError)
                                 else 'Could not check agents; retry Stop all agents.')
        if not self._unchanged(scope):
            return self._cancelled_selection()
        if not targets:
            message = 'No saved agents across any session.'
            self.notice(message)
            return ActionOutcome('completed', message)
        agents, sessions = len(targets), len({target[0] for target in targets})
        prompt = (f'Stop all agents? [y/N]\n{agents} agent{"s" if agents != 1 else ""} across '
                  f'{sessions} session{"s" if sessions != 1 else ""}.\n'
                  'Active work will be interrupted.\n'
                  'Also checks stopped agents for leftover processes.\n'
                  'Saved sessions and history are kept for resuming later.')
        accepted = await self._dialog('confirm', prompt, default=False, escape=False)
        if not self._unchanged(scope):
            return self._cancelled_selection()
        if not accepted:
            return ActionOutcome('cancelled')
        outcome = await self.controller.execute_action('stop_all', {'targets': targets, 'confirmed': True})
        await self.refresh_sessions()
        return outcome

    async def _menu(self, title, actions, target_id, scope):
        choices = [choice for choice in self.view.action_choices() if choice['id'] in actions]
        result = await self._dialog('choose', title, choices)
        if result.cancelled:
            return ActionOutcome('cancelled')
        if not self._unchanged(scope):
            return self._cancelled_selection()
        return await self._dispatch(result.value, target_id, scope)

    async def _agent_menu(self, target_id, scope):
        actions = {'new_agent', 'resume', 'stop', 'remove', 'attach', 'unread',
                   'retry', 'history', 'inspect_agent'}
        while self._unchanged(scope):
            rows = self.view.agent_rows()
            stale = target_id is not None and not any(a['agent_id'] == target_id for a in rows)
            if not stale:
                target_id = await self._choose_agent(target_id, allow_add=self.controller.workspace is not None)
                if not self._unchanged(scope):
                    return self._cancelled_selection()
                if target_id is _ADD_AGENT:
                    return await self._dispatch('new_agent', None, scope)
                if target_id is None:
                    if self.dialogs.future is None and not self.view.help_visible:
                        self.view.focus_named('composer')
                    return ActionOutcome('cancelled')
                stale = not any(a['agent_id'] == target_id for a in self.view.agent_rows())
                if not stale:
                    self.state.selected_agent_id = target_id
            if stale:
                self.state.selected_agent_id = target_id = None
                self.notice('Selected agent is no longer available.')
            title = 'Agent actions' + (': ' + label_text(target_id) if target_id else '')
            choices = [choice for choice in self.view.action_choices() if choice['id'] in actions]
            result = await self._dialog('choose', title, choices,
                                       cancel_label='Back' if rows else 'Cancel', back_key='f3')
            if not self._unchanged(scope):
                return self._cancelled_selection()
            if result.cancelled:
                if result.value is not _CHOICE_BACK or not self.view.agent_rows():
                    return ActionOutcome('cancelled')
                target_id = None
                continue
            return await self._dispatch(result.value, target_id, scope)
        return self._cancelled_selection()

    async def show_palette(self):
        scope = self._scope()
        agent_id = self.state.selected_agent_id
        result = await self._dialog('choose', 'Commands', self.view.action_choices())
        if result.cancelled:
            return ActionOutcome('cancelled')
        if not self._unchanged(scope):
            return self._cancelled_selection()
        return await self._dispatch(result.value, agent_id, scope)

    async def run_action(self, action_id, *, target_id=None):
        scope = self._scope()
        agent_actions = {'resume', 'stop', 'remove', 'attach', 'unread', 'retry', 'history', 'inspect_agent'}
        if action_id == 'choose_attach':
            action_id, target_id = 'attach', None
        elif target_id is None and action_id in agent_actions:
            target_id = self.state.selected_agent_id
        return await self._dispatch(action_id, target_id, scope)

    async def _dispatch(self, action_id, target_id, scope):
        key = (scope, action_id, target_id)
        if key in self._active:
            return ActionOutcome('cancelled')
        self._active.add(key)
        try:
            return await self._run(action_id, target_id, scope)
        finally:
            self._active.discard(key)

    async def _run(self, action, target_id, scope):
        if action == 'update':
            callback = self.view.callbacks.get('update')
            if callback is None:
                return ActionOutcome('cancelled')
            return await callback()
        if action == 'restart_server':
            return await self.restart_server(scope)
        elif action == 'stop_all':
            return await self.stop_all_agents(scope)
        elif action == 'loop_guard':
            return await self.loop_guard_form()
        elif action == 'commands':
            return await self.show_palette()
        if action == 'quit':
            await self.view.callbacks['quit']()
        elif action == 'new_session':
            return await self.new_session()
        elif action == 'enable_orchestrator':
            return await self.orchestrator_form(scope)
        elif action == 'switch_session':
            return await self.view.callbacks['navigate']()
        elif action == 'select_session':
            return await self._select(target_id) if target_id else ActionOutcome('cancelled')
        elif action == 'refresh':
            return await self.refresh_sessions()
        elif action == 'activity':
            self.view.show_activity()
        elif action == 'clear_draft':
            return ActionOutcome('completed' if await self.composer_actions.clear_draft() else 'cancelled')
        elif action == 'help':
            if self.view.help_visible:
                self.view.hide_help()
                return ActionOutcome('completed')
            from cli import HELP
            text = ('F2 Sessions/Channels · F3 Agents · F4 Commands · F5 Activity · F6 Attach\n'
                    'F7 Select text: drag, terminal Copy (Ctrl+Shift+C), F7 return\n'
                    'Editing/sending pause during selection; F7 restores message focus and mode.\n'
                    'Shift-drag also bypasses mouse capture in supporting terminals.\n'
                    'Message starts NORMAL: i/I/a/A edit · Enter sends\n'
                    'INSERT: Enter completes/adds a line · Escape returns NORMAL\n'
                    'NORMAL movement: h/j/k/l, w/b, 0/$ · Paste enters INSERT\n'
                    'Mentions such as @agent-1 appear bright cyan and bold\n'
                    'Outside forms: Tab changes focus · Enter selects dialog choices\n'
                    'Forms: Up/Down changes fields · Tab/Shift+Tab cycles choices/paths\n'
                    'Ctrl+Q Quit · Escape cancels · Ctrl+C preserves draft\n'
                    'Sessions opens navigation. Committed switches and Quit checkpoint.\n\n' +
                    HELP.replace('/history            Show recent messages in this channel',
                                 '/history            Jump to conversation') + '\n' +
                    SESSION_HELP.replace('/sessions            Checkpoint, then choose a session',
                                         '/sessions            Open session navigation').replace(
                        '/history             Show recent channel messages',
                        '/history            Jump to conversation'))
            self.view.show_help(text)
        elif action in ('switch_channel', 'create_channel'):
            return await self._channel(action)
        elif action == 'history' and self.controller.plain_channel:
            self.view.hide_activity()
            self.state.viewport.mark_seen()
            self.view.focus_named('conversation')
            return ActionOutcome('completed')
        elif action in ('new_agent', 'resume', 'history'):
            return await self.agent_form('spawn' if action == 'new_agent' else action, target_id, _scope=scope)
        elif action in ('rename_session', 'archive_session'):
            if scope[0] is None:
                return self._failure('Select a session first')
            name = self.controller.workspace.get('name') or scope[0]
            if action == 'rename_session':
                fields, error = [Field('name', 'Session name:', default=name)], None
                while True:
                    result = await self._dialog('form', 'Rename session', fields, submit_label='Rename', error=error)
                    if result.cancelled:
                        return ActionOutcome('cancelled')
                    if not self._unchanged(scope):
                        return self._cancelled_selection()
                    outcome = await self.controller.execute_action(action, result.value)
                    if outcome.status != 'failed':
                        break
                    error = outcome.message
                    fields = [replace(f, default=result.value[f.name]) for f in fields]
            else:
                running = sum(a.get('last_state') in ('running', 'starting') for a in self.view.agent_rows())
                accepted = await self._dialog('confirm', label_text(name) +
                    f'\n{running} running agent(s) will be checkpointed and stopped.' +
                    '\nArchive session? [y/N]',
                                                       default=False, escape=False)
                if not self._unchanged(scope):
                    return self._cancelled_selection()
                if not accepted:
                    return ActionOutcome('cancelled')
                outcome = await self.controller.execute_action(action, {'confirmed': True})
                if outcome.status == 'completed':
                    generation = self.controller.selection_generation
                    self.composer_actions.switch_draft(None, mandatory=True)
                    await self.refresh_sessions()
                    if (self.controller.workspace is not None
                            and self.controller.selection_generation != generation):
                        return ActionOutcome('completed', workspace_id=self.controller.workspace['id'])
                    return await self.view.callbacks['navigate'](mandatory=True)
            if outcome.status == 'completed':
                await self.refresh_sessions()
            return outcome
        elif action in ('agents', 'inspect_agent', 'stop', 'remove', 'attach', 'unread', 'retry'):
            if self.controller.plain_channel and action == 'agents':
                result = await self.client.submit_outcome('/agents')
                self.view.show_activity()
                return ActionOutcome(result.status, result.message)
            if action == 'agents':
                return await self._agent_menu(target_id, scope)
            ident = await self._choose_agent(target_id)
            if not self._unchanged(scope):
                return self._cancelled_selection()
            if ident is None:
                return ActionOutcome('cancelled')
            if action in ('agents', 'inspect_agent'):
                self.state.selected_agent_id = ident
                self.view.show_inspector()
            else:
                if action in ('attach', 'stop', 'remove') and sys.platform == 'win32':
                    return self._failure(WINDOWS_TMUX_ERROR)
                if action in ('stop', 'remove'):
                    agent = next((a for a in self.view.agent_rows() if a['agent_id'] == ident), None)
                    if agent is None:
                        return self._cancelled_selection()
                    prompt = ('Remove ' + _agent_label(agent) + '? [y/N]\n'
                              'Stops its wrapper and tmux session, then removes the saved entry.\n'
                              'Session chat and provider conversation files are kept.'
                              if action == 'remove' else 'Stop ' + _agent_label(agent) + '? [y/N]')
                    accepted = await self._dialog('confirm', prompt,
                                                          default=False, escape=False)
                    if not self._unchanged(scope):
                        return self._cancelled_selection()
                    if not accepted:
                        return ActionOutcome('cancelled')
                outcome = await self.controller.execute_action(action, {'agent_id': ident})
                if action == 'remove' and outcome.status == 'completed' and self._unchanged(scope):
                    if self.state.selected_agent_id == ident:
                        self.state.selected_agent_id = None
                    self.view.inspecting = False
                    self.view.focus_named('composer')
                if action == 'unread':
                    self.view.show_activity()
                return outcome
        else:
            return self._failure('Unknown action: ' + str(action))
        return ActionOutcome('completed')

    async def _channel(self, action):
        if not self.controller.plain_channel:
            return self._failure('Channel navigation requires plain-channel mode')
        original = self.client.channel
        fields, error = [Field('name', 'Channel name:', required=True)], None
        while True:
            if action == 'switch_channel':
                result = await self._dialog('choose', 'Channels', [dict(id=name, label=name)
                                                               for name in self.client.channels])
                name = result.value
            else:
                result = await self._dialog('form', 'Create channel', fields, submit_label='Create channel', error=error)
                name = result.value['name'] if not result.cancelled else None
            if result.cancelled:
                return ActionOutcome('cancelled')
            if original != self.client.channel:
                return self._cancelled_selection()
            normalized = name.strip().removeprefix('#')
            if not self._admit(('channel', normalized)):
                return ActionOutcome('cancelled')
            outcome = await self.client.submit_outcome(('/join ' if action == 'switch_channel' else '/create ') + name)
            if outcome.status != 'failed' or action == 'switch_channel':
                if outcome.status == 'completed' and self.client.channel != original:
                    self.composer_actions.switch_draft(self.composer_actions.destination_key(), mandatory=True)
                    self.state.viewport.mark_seen()
                return ActionOutcome(outcome.status, outcome.message)
            error = outcome.message
            fields = [replace(f, default=name) for f in fields]
