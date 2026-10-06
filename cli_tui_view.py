"""Persistent, sanitized presentation over the client and controller models."""

from collections import Counter, deque
import sys
import re
from bisect import bisect_right

from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (ConditionalContainer, DynamicContainer, Float,
                                   FloatContainer, HSplit, ScrollOffsets, VSplit, Window)
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.containers import WindowAlign
from prompt_toolkit.layout.layout import walk
from prompt_toolkit.keys import Keys
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Box, Button, Frame, TextArea

from cli_tui_state import MAX_DRAFT_BYTES, body_text, clip_cells, label_text, layout_mode
from cli_tui_theme import TUI_STYLE
from cli_tui_markdown import join_lines, markdown_lines, wrap_plain as _wrap
from cli_view_contracts import channel_transcript
from cli_workspace_chat import SESSION_COMMANDS, SESSION_HELP, _timestamp
from cli_workspaces import WINDOWS_TMUX_ERROR


class _PaneButton(Button):
    """Quiet, aligned pane actions with native Button input and mouse handling."""

    def __init__(self, text, *, handler, width=20, symbol=''):
        self.symbol = symbol
        super().__init__(text, handler=handler, width=width, left_symbol=' ', right_symbol=' ')
        native_style = self.window.style
        self.window.style = lambda: 'class:pane-action ' + native_style()
        self.window.align = WindowAlign.LEFT

    def _get_text_fragments(self):
        fragments = super()._get_text_fragments()
        focused = get_app().layout.has_focus(self)
        for index, (style, text, *extra) in enumerate(fragments):
            if style == 'class:button.text':
                label = ' ' + (self.symbol + ' ' if self.symbol else '') + self.text
                fragments[index] = (style, label.ljust(self.width - 2), *extra)
        fragments[0] = (fragments[0][0], '›' if focused else ' ', *fragments[0][2:])
        return fragments


class _MentionLexer(Lexer):
    """Style mention-shaped handles without changing draft text or routing."""

    def lex_document(self, document):
        def line(index):
            text = document.lines[index]
            fragments, start = [], 0
            for match in re.finditer(r'(?<![\w@])@[\w][\w-]*', text):
                fragments.extend([('', text[start:match.start()]),
                                  ('class:composer.mention', match.group())])
                start = match.end()
            return [*fragments, ('', text[start:])]
        return line


class _SafeComposer(Processor):
    """Sanitize displayed buffer text while preserving raw edits and offsets."""

    def apply_transformation(self, ti):
        fragments, offsets = [], [0]
        for style, text, *extra in ti.fragments:
            for character in text:
                visible = body_text(character) if character == '\t' else label_text(character)
                fragments.append((style, visible, *extra))
                offsets.append(offsets[-1] + len(visible))
        return Transformation(fragments,
            source_to_display=lambda position: offsets[min(position, len(offsets) - 1)],
            display_to_source=lambda position: max(0, bisect_right(offsets, position) - 1))


class _SidebarControl(FormattedTextControl):
    """Give blank cells coordinates so Window cannot remap their clicks to row zero."""

    def create_content(self, width, height):
        content = super().create_content(width, height)

        def get_line(index):
            line = content.get_line(index) if index < content.line_count else []
            used = sum(get_cwidth(fragment[1]) for fragment in line)
            return [*line, ('', ' ' * max(0, width - used))]

        return UIContent(get_line=get_line, line_count=max(height or 0, content.line_count),
                         cursor_position=content.cursor_position,
                         menu_position=content.menu_position, show_cursor=content.show_cursor)


class _ConversationControl(UIControl):
    """Use actual render dimensions, never FormattedTextControl sizing caches."""

    def __init__(self, view):
        self.width, self.height = 1, 1
        self.view = view
        self._visible_start = (None, 0)

    def is_focusable(self):
        return True

    def mouse_handler(self, mouse_event):
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            self.view.focus_named('conversation')
            self.view.state.viewport.mark_seen()
            self.view._app().invalidate()
            return None
        if mouse_event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            self.scroll(-1 if mouse_event.event_type == MouseEventType.SCROLL_UP else 1, 3)
            self.view._app().invalidate()
            return None
        return NotImplemented

    def page(self, direction):
        self.scroll(direction, max(1, self.height))

    def scroll(self, direction, lines):
        ids = self.view._transcript_ids()
        if not ids:
            return
        ident, offset = self._visible_start
        index = ids.index(ident) if ident in ids else 0
        remaining = lines

        def count(position):
            message = self.view._message(ids[position])
            return sum(1 for _ in self._message_lines(message, self.width)) if message else 1
        if direction < 0:
            while remaining > offset and index > 0:
                remaining -= offset
                index -= 1
                offset = count(index)
            offset = max(0, offset - remaining)
        else:
            offset += remaining
            length = count(index)
            while offset >= length and index + 1 < len(ids):
                offset -= length
                index += 1
                length = count(index)
            offset = min(offset, max(0, length - 1))
        if direction > 0:
            # At most one viewport of lookahead decides whether the proposed
            # anchor reaches the tail; do not scan the full transcript per wheel.
            available = count(index) - offset
            following = index + 1
            while available < self.height and following < len(ids):
                available += count(following)
                following += 1
            if available <= self.height and following == len(ids):
                self.view.state.viewport.mark_seen()
                return
        self.view.state.viewport.anchor(ids[index], self.view.client.messages, line_offset=offset)

    def create_content(self, width, height):
        self.width, self.height = width, height
        lines = self._lines(width, height)
        return UIContent(get_line=lambda index: lines[index],
                         line_count=len(lines), show_cursor=False)

    def text(self):
        """Sanitized styled fragments, also useful to renderer-level tests."""
        fragments = []
        for index, line in enumerate(self._lines(self.width, self.height)):
            if index:
                fragments.append(('', '\n'))
            fragments.extend(line)
        return fragments

    def _message_lines(self, message, width):
        width = max(1, width)
        if not self.view.selecting_text:
            width = min(width, 112)
        raw_sender = message.get('sender', '?')
        sender = label_text(raw_sender)
        system = raw_sender == 'system' or message.get('type') in ('system', 'join', 'leave', 'status')
        own = raw_sender == (self.view.client.username or 'user') and not system
        style = 'class:muted' if system else 'class:chat.you' if own else 'class:chat.agent'
        marker = '·' if system else '●' if own else '◆'
        name = 'You' if own else sender
        clock = clip_cells(label_text(message.get('time', '')), max(0, min(12, width - 6)))
        clock = '  ' + clock if clock else ''
        header = clip_cells(marker + ' ' + name, max(1, width - get_cwidth(clock)))
        yield [(style, header), ('class:chat.time', clock)]
        gutter = '' if self.view.selecting_text else clip_cells('│ ', max(0, width - 1))
        content_width = max(1, width - get_cwidth(gutter))

        def body_lines(text, text_style=''):
            for line in _wrap(text, content_width):
                yield [('class:chat.gutter', gutter), (text_style, line)]

        for line in markdown_lines(message.get('text', ''), content_width,
                                   raw=self.view.selecting_text):
            yield [('class:chat.gutter', gutter),
                   *((('class:muted ' if system else '') + style, text) for style, text in line)]
        for attachment in message.get('attachments', []):
            name = label_text(attachment.get('name', ''))
            url = label_text(attachment.get('url', ''))
            yield from body_lines('Attachment: ' + ' '.join(value for value in (name, url) if value),
                                  'class:chat.attachment')
        choices = message.get('metadata', {}).get('choices', [])
        if choices:
            yield from body_lines('Choices: ' + ' | '.join(label_text(choice) for choice in choices),
                                  'class:chat.choices')
        yield [('', '')]

    def _lines(self, width, height):
        height = max(1, height)
        ids = self.view._transcript_ids()
        viewport = self.view.state.viewport
        lines = deque()
        if viewport.follow:
            for ident in reversed(ids):
                message = self.view._message(ident)
                if message is None:
                    continue
                tail = deque(enumerate(self._message_lines(message, width)), maxlen=height - len(lines))
                if tail:
                    self._visible_start = (ident, tail[0][0])
                lines.extendleft(line for _, line in reversed(tail))
                if len(lines) >= height:
                    break
        else:
            start = ids.index(viewport.anchor_id) if viewport.anchor_id in ids else 0
            for ident in ids[start:]:
                message = self.view._message(ident)
                if message is None:
                    continue
                offset = viewport.line_offset if ident == viewport.anchor_id else 0
                message_lines = self._message_lines(message, width)
                # Clamp a saved offset if a message edit shortened its body.
                tail = deque(maxlen=1)
                visible = False
                for line_index, line in enumerate(message_lines):
                    tail.append((line_index, line))
                    if line_index < offset:
                        continue
                    if not lines:
                        self._visible_start = (ident, line_index)
                    visible = True
                    lines.append(line)
                    if len(lines) >= height:
                        return list(lines)
                if not visible and tail:
                    line_index, line = tail[0]
                    self._visible_start = (ident, line_index)
                    if not self.view.selecting_text:
                        viewport.line_offset = line_index
                    lines.append(line)
        return list(lines) or [[('class:muted', 'No messages yet. Write in Message below.')]]


class _ActivityControl(UIControl):
    def __init__(self, view):
        self.view = view
        self.height = 1

    def is_focusable(self):
        return True

    def mouse_handler(self, mouse_event):
        if mouse_event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            offset = -3 if mouse_event.event_type == MouseEventType.SCROLL_UP else 3
            self.view._activity_line = max(0, min(self.view._activity_max_line,
                                                 self.view._activity_line + offset))
            self.view._app().invalidate()
            return None
        return NotImplemented

    def create_content(self, width, height):
        self.height = max(1, height)
        text = '\n'.join(self.view.state.notices.lines) or 'No activity yet. Esc returns to conversation.'
        lines = markdown_lines(text, width, raw=self.view.selecting_text)
        self.view._activity_max_line = max(0, len(lines) - self.height)
        offset = min(self.view._activity_line, self.view._activity_max_line)
        if not self.view.selecting_text:
            self.view._activity_line = offset
        visible = lines[offset:offset + self.height]
        return UIContent(get_line=lambda index: visible[index],
                         line_count=len(visible), show_cursor=False)


class _HelpControl(UIControl):
    def __init__(self, view):
        self.view = view
        self.height = 1
        self.last_line = 0

    def is_focusable(self):
        return True

    def create_content(self, width, height):
        self.height = max(1, height)
        lines = markdown_lines(self.view._help_text, width, raw=self.view.selecting_text)
        self.last_line = max(0, len(lines) - self.height)
        offset = min(self.view._help_line, self.last_line)
        if not self.view.selecting_text:
            self.view._help_line = offset
        visible = lines[offset:offset + self.height]
        return UIContent(get_line=lambda index: visible[index],
                         line_count=len(visible), show_cursor=False)


class ContextualCompleter(Completer):
    """Resolve suggestions from current authoritative models on every request."""

    @staticmethod
    def _help_descriptions(help_text):
        descriptions = {}
        for line in help_text.splitlines():
            if line.startswith('/'):
                command = line.split()[0]
                columns = re.split(r'\s{2,}', line, maxsplit=1)
                descriptions[command] = (columns[1] if len(columns) == 2 else
                                         line.partition(' ')[2]) or 'Chat command'
        return descriptions

    def _commands(self):
        # Lazy import keeps cli's future lazy TUI entry path free of an import cycle.
        from cli import HELP
        commands = {command: 'Server chat command' for command in
                    re.findall(r'/[a-z][a-z_-]*', HELP)}
        commands.update(self._help_descriptions(HELP))
        if not self.view.controller.plain_channel:
            commands.pop('/join', None)
            commands.pop('/create', None)
            descriptions = self._help_descriptions(SESSION_HELP)
            applicable = SESSION_COMMANDS if self.view.controller.workspace is not None else {'/sessions'}
            commands.update({command: descriptions.get(command, 'Session command')
                             for command in sorted(applicable)})
        return commands

    def __init__(self, view):
        self.view = view
        self._context = self._context_key()

    def _session_handles(self):
        agents = (self.view.controller.workspace or {}).get('agents', [])
        handles = []
        for agent in agents:
            if not isinstance(agent, dict) or not isinstance(agent.get('agent_id'), str) or not agent['agent_id']:
                continue
            handle = agent.get('registry_name') or agent['agent_id']
            if isinstance(handle, str):
                handles.append(handle)
        return tuple(dict.fromkeys(handles))

    def _context_key(self):
        controller, client = self.view.controller, self.view.client
        if controller.plain_channel:
            names = tuple(dict.fromkeys(name for name in client.agent_names
                                        if isinstance(name, str) and name))
            return ('channel', client.channel, names)
        return ('session', (controller.workspace or {}).get('id'), self._session_handles())

    def refresh_context(self):
        context = self._context_key()
        if context != self._context:
            self._context = context
            # A pending result also observes this cleared state. Preserve text
            # and cursor, including any mention the user already inserted.
            self.view.composer.buffer.complete_state = None

    def get_completions(self, document, complete_event):
        client, controller = self.view.client, self.view.controller
        before = document.text_before_cursor
        word = re.search(r'\S*$', before).group()
        prior = before[:-len(word)] if word else before
        parts = prior.split()
        commands = self._commands()
        handles = self._session_handles()
        candidates = {}
        if not parts and word.startswith('/'):
            candidates = commands
        elif word.startswith('@'):
            candidates = {'@' + name: 'Mention agent' for name in self._context_key()[2]}
        elif parts and parts[0] in commands:
            command = parts[0]
            if controller.plain_channel and command == '/join' and len(parts) == 1:
                candidates = {(('#' if word.startswith('#') else '') + name): 'Channel'
                              for name in client.channels}
            elif not controller.plain_channel and controller.workspace is not None:
                if command == '/spawn' and len(parts) == 1:
                    candidates = {name: 'Configured provider' for name in controller.providers}
                elif command in ('/resume', '/stop', '/attach', '/retry', '/unread', '/history') and len(parts) == 1:
                    candidates = {name: 'Session agent' for name in handles}
                elif (command == '/history' and len(parts) == 2 or
                      command == '/spawn' and parts[-1] == '--history-mode'):
                    candidates = {'literal': 'Literal history', 'none': 'No history'}
        elif word.startswith('#'):
            candidates = {'#' + name: 'Channel' for name in client.channels}
        for value, description in candidates.items():
            if value.startswith(word):
                yield Completion(value, start_position=-len(word),
                                 display=[('', label_text(value))],
                                 display_meta=[('', label_text(description))])


class ComposerActions:
    """Bind one persistent buffer to bounded destination drafts and submission."""

    def __init__(self, view, state, submit, notice):
        self.view, self.state, self.submit, self.notice = view, state, submit, notice
        self.key = None
        self.edit_version = 0
        self.sending = False
        self._restoring = False
        self._last_rejection = None
        self.buffer = view.composer.buffer
        self._accepted = Document()
        self.buffer.on_text_changed += self._edited
        self.buffer.on_cursor_position_changed += self._cursor_changed
        previous_read_only = self.buffer.read_only
        self.buffer.read_only = Condition(lambda: self.key is None or previous_read_only())
        self.buffer.completer = ContextualCompleter(view)
        self.buffer.complete_while_typing = Condition(lambda:
            self.key is not None and self.view.composer_mode == 'INSERT'
            and not self.view.selecting_text)
        self._bindings()
        self.switch_draft(self.destination_key(), mandatory=True)

    def destination_key(self):
        """Return authoritative selection identity; None disables the composer."""
        if self.view.controller.plain_channel:
            return ('channel', self.view.client.channel)
        workspace = self.view.controller.workspace
        return ('session', workspace['id']) if workspace is not None else None

    def rename_channel(self, old_name, new_name):
        old_key, new_key = ('channel', old_name), ('channel', new_name)
        if not self.state.drafts.rename(old_key, new_key):
            self.notice('Channel draft collision; both drafts were kept')
            return False
        if self.key == old_key:
            self.key = new_key
            self.edit_version += 1
            self._last_rejection = None
        elif self.key == new_key:
            # Settings may have already selected the renamed destination while empty.
            self._restore(Document(self.state.drafts.get(new_key), self.state.drafts.get_cursor(new_key)))
            self.edit_version += 1
            self._last_rejection = None
        self.view._app().invalidate()
        return True

    def _restore(self, document):
        self._restoring = True
        try:
            self.buffer.set_document(document, bypass_readonly=True)
            self._accepted = document
        finally:
            self._restoring = False

    def _reject_edit(self, message):
        if message != self._last_rejection:
            self._last_rejection = message
            self.notice(message)

    def _edited(self, buffer):
        if self._restoring:
            return
        document = buffer.document
        if self.key is None or not self.state.drafts.set(
                self.key, document.text, cursor=document.cursor_position):
            too_large = len(document.text.encode('utf-8', 'surrogatepass')) > MAX_DRAFT_BYTES
            self._restore(self._accepted)
            self._reject_edit('Select a destination before editing' if self.key is None else
                              'Draft exceeds 64 KiB UTF-8; edit rejected' if too_large else
                              self.state.drafts.capacity_notice)
            return
        self._last_rejection = None
        self.edit_version += 1
        self._accepted = document

    def _cursor_changed(self, buffer):
        if not self._restoring:
            self._accepted = buffer.document
            if self.key is not None:
                self.state.drafts.set_cursor(self.key, buffer.cursor_position)

    def switch_draft(self, key, *, mandatory=False):
        """Bind admitted destination text/cursor; caller owns selection commits."""
        if key == self.key:
            return True
        full = key is not None and not self.state.drafts.can_open(key)
        if full and not mandatory:
            self.notice(self.state.drafts.capacity_notice)
            return False
        if full:
            self.notice(self.state.drafts.capacity_notice)
        self.key = key
        self._last_rejection = self.state.drafts.capacity_notice if full else None
        self.edit_version += 1
        self._restore(Document(self.state.drafts.get(key), self.state.drafts.get_cursor(key))
                      if key is not None else Document())
        self.view._app().invalidate()
        return True

    def _claim_send(self):
        """Capture the Enter-time snapshot before queued typeahead can edit it."""
        if (self.view.composer_mode != 'NORMAL' or self.view.selecting_text
                or self.sending or self.key is None or not self.buffer.text.strip()):
            return
        if self.key != self.destination_key():
            self.notice('Draft destination changed; select its destination before sending')
            return
        self.sending = True
        return self.key, self.buffer.text, self.state.drafts.revision(self.key)

    async def send(self):
        snapshot = self._claim_send()
        if snapshot is not None:
            await self._send_snapshot(snapshot)

    async def _send_snapshot(self, snapshot):
        key, text, revision = snapshot
        try:
            # Selection may have changed after the synchronous key handler returned.
            if key != self.destination_key():
                self.notice('Draft destination changed; select its destination before sending')
                return
            outcome = await self.submit(text)
            if outcome.status == 'completed':
                if revision == self.state.drafts.revision(key):
                    self.state.drafts.clear(key)
                    if key == self.key:
                        self.edit_version += 1
                        self._restore(Document())
                elif outcome.sent:
                    self.notice('The earlier message was sent; your current draft was kept')
            if not outcome.keep_running:
                await self.view.callbacks['quit']()
        finally:
            self.sending = False
            self.view._app().invalidate()

    async def clear_draft(self):
        key, version = self.key, self.edit_version
        if key is None or not self.buffer.text:
            return False
        if not await self.view.dialogs.confirm('Clear draft? [y/N]', default=False, escape=False):
            return False
        if key != self.key or version != self.edit_version:
            return False
        self.state.drafts.clear(key)
        self.edit_version += 1
        self._restore(Document())
        return True

    def _bindings(self):
        bindings = self.view.key_bindings
        focused = has_focus(self.view.composer) & Condition(lambda:
            self.view.screen_mode != 'small' and self.view.dialogs.future is None)
        editable = focused & Condition(lambda: self.key is not None)

        @bindings.add('enter', filter=focused)
        def send(event):
            if self.view.selecting_text:
                return
            completion = self.buffer.complete_state
            if self.view.composer_mode == 'INSERT':
                if completion is not None and completion.complete_index is not None:
                    self.buffer.apply_completion(completion.current_completion)
                elif self.key is not None:
                    self.buffer.insert_text('\n')
                return
            self.buffer.complete_state = None
            snapshot = self._claim_send()
            if snapshot is not None:
                started = False
                async def submit_snapshot():
                    nonlocal started
                    started = True
                    await self._send_snapshot(snapshot)
                task = event.app.create_background_task(submit_snapshot())
                def release_unstarted(done):
                    if not started:
                        self.sending = False
                        event.app.invalidate()
                task.add_done_callback(release_unstarted)

        @bindings.add('escape', 'enter', filter=focused)
        def normal_then_send(event):
            # A quick Esc, Enter can arrive together as a terminal key sequence.
            if self.view.selecting_text:
                return
            self.view.normal_composer()
            send(event)

        @bindings.add('tab', filter=editable & Condition(lambda: self.buffer.complete_state is not None))
        def complete(event):
            self.buffer.complete_next()

        @bindings.add('c-c', filter=focused)
        def cancel(event):
            if self.buffer.complete_state is not None:
                # Keep the visible candidate, rather than restoring pre-completion text.
                self.buffer.complete_state = None
            else:
                self.view._cancel_overlay()

        @bindings.add('escape', filter=focused,
                      eager=Condition(lambda: (self.buffer.complete_state is not None or self.view.activity_visible) and
                                      not self.view._app().key_processor.input_queue))
        def escape(event):
            if self.view.selecting_text:
                return
            self.view.normal_composer()
            if self.view.activity_visible:
                self.view.hide_activity()

        @bindings.add('c-d', filter=focused)
        def delete_or_quit(event):
            if self.view.selecting_text:
                return
            if not self.buffer.text:
                event.app.create_background_task(self.view.callbacks['quit']())
            elif self.key is not None and self.view.composer_mode == 'INSERT':
                self.buffer.delete()


class TuiView:
    """Build controls once. Callbacks own application actions and all I/O."""

    style = TUI_STYLE

    def __init__(self, client, controller, state, dialogs, callbacks):
        self.client, self.controller = client, controller
        self.state, self.dialogs, self.callbacks = state, dialogs, callbacks
        self._sessions = []
        self._sessions_loading = False
        self._sessions_stale = False
        self._transcript_key = None
        self._ordered_ids = ()
        self.inspecting = False
        self.selecting_text = False
        self.composer_mode = 'NORMAL'
        self._inspector_line = 0
        self.activity_visible = False
        self.help_visible = False
        self._help_text = ''
        self._help_line = 0
        self._help_focus = None
        self.help = _HelpControl(self)
        self._activity_line = 0
        self._activity_max_line = 0
        self._activity_focus = None
        self.composer = TextArea(multiline=True, height=self._composer_height, wrap_lines=True,
                                 focus_on_click=True,
                                 read_only=Condition(lambda: self.screen_mode == 'small'
                                                     or self.selecting_text
                                                     or self.composer_mode == 'NORMAL'),
                                 lexer=_MentionLexer(),
                                 input_processors=[_SafeComposer()])
        self.conversation = _ConversationControl(self)
        self.activity = _ActivityControl(self)
        self.navigation = _SidebarControl(self._navigation_fragments,
                                               focusable=True, show_cursor=False)
        self.pending_inputs = FormattedTextControl(self._pending_fragments, focusable=True, show_cursor=False)
        self.agents = FormattedTextControl(self._agent_fragments,
                                           focusable=True, show_cursor=False)
        self.conversation_window = Window(self.conversation, wrap_lines=False)
        self.navigation_window = Window(self.navigation, wrap_lines=False)
        self.agents_window = Window(self.agents, height=lambda: None if self.screen_mode == 'wide' and not self.inspecting else
                                    7 if self.inspecting else
                                    1 if self.screen_mode == 'compact' else 3, wrap_lines=False,
                                    scroll_offsets=ScrollOffsets(bottom=lambda: int(
                                        self.screen_mode == 'wide' and not self.inspecting)))
        framed_conversation = self._frame(Box(self.conversation_window, padding=0,
            padding_left=2, padding_right=2), self._conversation_title, 'conversation')
        conversation = DynamicContainer(lambda: self.conversation_window if self.selecting_text
                                         else framed_conversation)
        self.agent_actions = _PaneButton('Actions', symbol='≡', handler=lambda:
            self._app().create_background_task(self.callbacks['run_action'](
                'agents', target_id=self.state.selected_agent_id)))
        self.agent_review = _PaneButton('Review input', symbol='!', handler=self._review_input)
        review = ConditionalContainer(HSplit([self.agent_review], style='class:agent.review'),
                                      filter=Condition(lambda: self._input_agent() is not None))
        self.agent_attach = _PaneButton('Attach', symbol='↗', handler=self._direct_attach)
        attach = ConditionalContainer(self.agent_attach, filter=Condition(self._show_attach))
        self.agent_add = _PaneButton('Add agent', symbol='+', handler=self._add_agent)
        add = ConditionalContainer(self.agent_add, filter=Condition(self._show_add_agent))
        agent_contents = HSplit([self.agents_window,
            VSplit([review, attach, self.agent_actions, add], padding=1)])
        framed_agents = self._frame(agent_contents, lambda: 'Agent details · Esc Message' if self.inspecting
                                    else 'Agents · Enter Actions · F3 Choose · Esc Message', 'agents')
        agent_area = ConditionalContainer(DynamicContainer(lambda: agent_contents
            if self.screen_mode == 'compact' and not self.inspecting else framed_agents),
            filter=Condition(lambda: not self.controller.plain_channel
                             and (self.screen_mode != 'wide' or self.inspecting)))
        rail_agent_controls = HSplit([Window(height=1), review, attach, self.agent_actions, add],
            height=lambda: 2 + int(self._show_add_agent()) + int(
                self._input_agent() is not None or self._show_attach()))
        rail_agents = self._frame(HSplit([self.agents_window, rail_agent_controls],
                                        width=Dimension.exact(20)),
            lambda: 'Agent details' if self.inspecting else 'Agents', 'agents')
        self.clear_draft = _PaneButton('Clear draft', width=15, handler=lambda:
            self._app().create_background_task(self.callbacks['run_action']('clear_draft')))
        self.restart_server = _PaneButton('Restart server', symbol='↻', width=18, handler=lambda:
            self._app().create_background_task(self.callbacks['run_action']('restart_server')))
        composer_area = FloatContainer(
            content=self._frame(Box(self.composer, padding=0, padding_left=1,
                                   padding_right=1), self._composer_title, 'composer'),
            floats=[Float(top=0, right=1, content=self.clear_draft),
                    Float(bottom=0, right=1, content=self.restart_server)])
        activity_area = self._frame(Window(self.activity), lambda:
            f'Activity · {self.state.notices.omitted} omitted · Esc Back', 'activity')
        main = HSplit([DynamicContainer(lambda: activity_area if self.activity_visible else conversation),
                       agent_area, composer_area])
        self.new_session = _PaneButton('New session', symbol='+', handler=lambda:
            self._app().create_background_task(self.callbacks['run_action']('new_session'))
            if self.screen_mode == 'wide' and not self.controller.plain_channel else None)
        navigation_area = HSplit([self.navigation_window, ConditionalContainer(
            self.new_session, filter=Condition(lambda: not self.controller.plain_channel))])
        sidebar = self._frame(navigation_area, lambda: 'Channels' if
                              self.controller.plain_channel else 'Sessions · Stale' if self._sessions_stale
                              else 'Sessions', 'navigation',
                              width=Dimension.exact(22))
        pending_window = Window(self.pending_inputs, wrap_lines=False,
                                height=lambda: max(1, min(3, len(self.pending_input_rows()))),
                                dont_extend_height=True)
        pending_hint = Window(FormattedTextControl(lambda: [('class:muted',
            'Enter: review input' if self.pending_input_rows() else '')]), height=1)
        pending_area = self._frame(HSplit([pending_window, ConditionalContainer(
            pending_hint, filter=Condition(lambda: bool(self.pending_input_rows())))]),
            lambda: f'Input pending {min(99, len(self.pending_input_rows()))}'
                    f'{"+" if len(self.pending_input_rows()) > 99 else ""}', 'pending_inputs',
            width=Dimension.exact(22))
        sidebar = HSplit([sidebar, ConditionalContainer(rail_agents,
            filter=Condition(lambda: not self.controller.plain_channel and not self.inspecting)), ConditionalContainer(pending_area,
            filter=Condition(lambda: not self.controller.plain_channel))], width=Dimension.exact(22))
        wide = VSplit([sidebar, main])
        resize_notice = HSplit([Window(FormattedTextControl(
            'Resize terminal to at least 80 × 18.\nDraft and focus are preserved.'))])
        appbar = ConditionalContainer(Window(FormattedTextControl(self._appbar), height=1,
            style='class:appbar'), filter=Condition(self._show_appbar))
        footer = Window(FormattedTextControl(self._footer), height=1, style='class:footer')
        notice_strip = ConditionalContainer(Window(FormattedTextControl(self._notice_text), height=1),
            filter=Condition(lambda: self.screen_mode != 'small' and bool(self.state.notices.lines)))
        self.global_key_bindings = KeyBindings()
        self.key_bindings = self._bindings()
        self.root = FloatContainer(content=HSplit([appbar,
            DynamicContainer(lambda: resize_notice if self.screen_mode == 'small'
                             else wide if self.screen_mode == 'wide' else main),
            notice_strip, footer]),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=6,
                        extra_filter=has_focus(self.composer) & Condition(lambda:
                            self.screen_mode != 'small' and self.dialogs.future is None))),
                    Float(content=DynamicContainer(lambda: self.dialogs.body)),
                    Float(content=ConditionalContainer(self._help_container(),
                        filter=Condition(lambda: self.help_visible)))],
            key_bindings=self.key_bindings, style='class:app')
        self.refresh()

    def _help_container(self):
        bindings = KeyBindings()

        @bindings.add('escape', eager=Condition(lambda: not self._app().key_processor.input_queue))
        @bindings.add('c-c', eager=True)
        def close(event):
            self.hide_help()

        @bindings.add('escape', Keys.Any)
        @bindings.add('tab')
        @bindings.add('s-tab')
        def preserve(event):
            pass

        @bindings.add('up')
        @bindings.add('down')
        @bindings.add('pageup')
        @bindings.add('pagedown')
        @bindings.add('home')
        @bindings.add('end')
        def scroll(event):
            key = event.key_sequence[-1].key
            if key == 'home':
                self._help_line = 0
            elif key == 'end':
                self._help_line = self.help.last_line
            else:
                amount = self.help.height if key in ('pageup', 'pagedown') else 1
                self._help_line = max(0, self._help_line + (-amount if key in ('up', 'pageup') else amount))
        content = Frame(Window(self.help,
            height=lambda: max(3, min(20, self._app().output.get_size().rows - 4)),
            width=lambda: max(10, min(90, self._app().output.get_size().columns - 4))),
            title='yapp Help · F1/Esc Back · PgUp/PgDn Scroll')
        return FloatContainer(content=content, floats=[], modal=True, key_bindings=bindings,
                              style='class:dialog.body')

    def show_help(self, text):
        if self.help_visible:
            return False
        self._help_focus = self._app().layout.current_window
        self._help_text = body_text(text)
        self._help_line = 0
        self.help_visible = True
        self._app().layout.update_parents_relations()
        self._app().layout.focus(self.help)
        self._app().invalidate()
        return True

    def hide_help(self):
        if not self.help_visible:
            return False
        saved, self._help_focus = self._help_focus, None
        if (saved is not None and (saved.content is self.agent_review.control
                and self._input_agent() is None or saved.content is self.agent_attach.control
                and not self._show_attach() or saved.content is self.agent_add.control
                and not self._show_add_agent())):
            saved = self.composer.window
            self.state.focus_name = 'composer'
        self.help_visible = False
        visible = [node.content for node in walk(self.root, skip_hidden=True) if isinstance(node, Window)]
        if self.dialogs.future is not None:
            modal = [node for node in walk(self.dialogs.body, skip_hidden=True)
                     if isinstance(node, Window) and node.content.is_focusable()]
            current = self._app().layout.current_window
            if saved in modal:
                self._app().layout.current_window = saved
            elif current not in modal and modal:
                self._app().layout.current_window = modal[0]
        elif saved is not None and (saved.content in visible or self.screen_mode == 'small'):
            # A resize can hide the saved pane; retain its actual focus window.
            self._app().layout.current_window = saved
        else:
            self._app().layout.current_window = self.composer.window
        self._app().invalidate()
        return True

    def _composer_title(self):
        if self.selecting_text:
            return 'Message · Copying · F7 Resume ' + self.composer_mode
        key = (('channel', self.client.channel) if self.controller.plain_channel else
               ('session', self.controller.workspace['id']) if self.controller.workspace else None)
        title = 'Message · ' + self.composer_mode
        if key is not None and not self.state.drafts.can_open(key):
            return title + ' · ' + self.state.drafts.capacity_notice
        return title + (' · i Edit · Enter Send' if self.composer_mode == 'NORMAL'
                        else ' · Enter Newline · Esc Normal')

    def normal_composer(self):
        self.composer_mode = 'NORMAL'
        self.composer.buffer.complete_state = None
        self.composer.buffer.exit_selection()
        self._app().invalidate()

    def _composer_height(self):
        width = max(1, self._app().output.get_size().columns -
                    (22 if self.screen_mode == 'wide' else 0) - 4)
        lines = sum(max(1, (get_cwidth(line) + width - 1) // width)
                    for line in body_text(self.composer.text).split('\n'))
        return min(6, max(3, lines))

    def _app(self):
        try:
            return self.dialogs.app_getter()
        except AttributeError:  # Initial construction precedes Application composition.
            return get_app()

    def _frame(self, body, title, name, width=None):
        focus = self._focus_style(name)
        return HSplit([Frame(body, title=title)], width=width,
                      style=lambda: 'class:pane.' + name + ' ' + focus())

    @property
    def screen_mode(self):
        size = self._app().output.get_size()
        mode = layout_mode(size.columns, size.rows)
        # Native selection copies whole terminal rows, including adjacent panes.
        return 'compact' if self.selecting_text and mode == 'wide' else mode

    def _focus_style(self, name):
        def style():
            groups = {'agents': ('agents', 'agent_actions', 'agent_add', 'agent_review', 'agent_attach'),
                      'navigation': ('navigation', 'new_session'),
                      'composer': ('composer', 'clear_draft')}
            focused = self._app().layout.current_control
            for member in groups.get(name, (name,)):
                target = getattr(self, member)
                if focused is getattr(target, 'control', target):
                    return 'class:focused'
            return ''
        return style

    def focus_named(self, name):
        if name not in ('composer', 'conversation', 'navigation', 'agents', 'new_session', 'activity', 'agent_actions', 'agent_review', 'agent_attach', 'agent_add', 'pending_inputs', 'clear_draft', 'restart_server'):
            raise ValueError('Unknown focus target: ' + name)
        target = getattr(self, name)
        control = getattr(target, 'control', target)
        visible = [node.content for node in walk(self.root, skip_hidden=True) if isinstance(node, Window)]
        if control not in visible:
            return False
        self._app().layout.focus(target)
        self.state.focus_name = name
        self._app().invalidate()
        return True

    def _transcript_ids(self):
        key = (self.client.view_revision, self.client.channel)
        if key != self._transcript_key:
            self._ordered_ids = tuple(message['id'] for message in
                channel_transcript(self.client.messages, self.client.channel))
            self._transcript_key = key
        return self._ordered_ids

    def _message(self, ident):
        message = self.client.messages.get(ident)
        if message is None or message.get('channel', 'general') != self.client.channel:
            self._transcript_key = None
            return None
        return message

    def _transcript(self):
        for ident in self._transcript_ids():
            message = self._message(ident)
            if message is not None:
                yield message

    def refresh(self, event=None):
        completer = self.composer.buffer.completer
        if isinstance(completer, ContextualCompleter):
            completer.refresh_context()
        if (self._app().layout.current_control is self.agent_review.control
                and self._input_agent() is None or
                self._app().layout.current_control is self.agent_attach.control and not self._show_attach() or
                self._app().layout.current_control is self.agent_add.control and not self._show_add_agent()):
            if not self.focus_named('composer'):
                # Preserve a valid editing target even while the small-screen
                # overlay hides the composer; resizing back must restore typing.
                self._app().layout.current_window = self.composer.window
                self.state.focus_name = 'composer'
        rows = {message['id']: message for message in self._transcript()}
        changed = event.message_ids if event is not None and event.source == 'client' else ()
        self.state.viewport.sync(rows, changed_ids=changed,
                                 deleted_ids=tuple(ident for ident in changed if ident not in rows),
                                 reconnect=event is not None and event.kind in ('history', 'selection', 'channel'))
        self._app().invalidate()

    def session_rows(self):
        return tuple(self._sessions)

    @property
    def sessions_stale(self):
        return self._sessions_stale

    def set_sessions(self, rows):
        """Project navigation metadata only; caller owns fetching and warnings."""
        self._sessions_stale = False
        self._sessions = [{key: row.get(key) for key in ('id', 'name', 'archived', 'updated_at')}
                          for row in rows]
        self._sessions.sort(key=lambda row: _timestamp(row.get('updated_at')), reverse=True)
        ids = [row['id'] for row in self._sessions]
        if self.state.selected_session_id not in ids:
            current = (self.controller.workspace or {}).get('id')
            self.state.selected_session_id = current if current in ids else next(iter(ids), None)
        self._app().invalidate()

    def set_sessions_loading(self, loading):
        self._sessions_loading = bool(loading)
        self._app().invalidate()

    def set_sessions_error(self, text):
        """Retain old rows; own the stale marker and one diagnostic notice."""
        self._sessions_loading = False
        self._sessions_stale = True
        self.state.notices.add(text)
        self._app().invalidate()

    def _notice_text(self):
        latest = self.state.notices.lines[-1] if self.state.notices.lines else ''
        width = max(1, self._app().output.get_size().columns - len('Notice: '))
        lines = markdown_lines(latest, width, raw=self.selecting_text)
        line = next((line for line in lines if any(text.strip() for _, text in line)), lines[0])
        return [('class:notice', 'Notice: '),
                *(('class:notice ' + style, text) for style, text in line)]

    def show_activity(self):
        """Open the read-only NoticeStore surface, retaining prior chat focus."""
        if self.activity_visible or self.screen_mode == 'small' or self.dialogs.future is not None:
            return False
        current = self._app().layout.current_control
        name = next((name for name in ('composer', 'conversation', 'navigation', 'agents', 'new_session',
                                       'restart_server')
                     if current is getattr(getattr(self, name), 'control', getattr(self, name))),
                    self.state.focus_name)
        self._activity_focus = (current, name)
        self._activity_line = 0
        self.activity_visible = True
        return self.focus_named('activity')

    def hide_activity(self):
        """Keep current visible focus; otherwise restore prior focus or composer."""
        if not self.activity_visible or self.screen_mode == 'small':
            return False
        current = self._app().layout.current_control
        self.activity_visible = False
        saved, name = self._activity_focus
        self._activity_focus = None
        visible = [node.content for node in walk(self.root, skip_hidden=True) if isinstance(node, Window)]
        if current is not self.activity and current in visible:
            self._app().invalidate()
        elif saved in visible:
            self._app().layout.focus(saved)
            self.state.focus_name = name
            self._app().invalidate()
        else:
            self.focus_named('composer')
        return True

    def _navigation_fragments(self):
        if self.controller.plain_channel:
            return [('', clip_cells(('> ' if channel == self.client.channel else '  ') +
                                    label_text(channel), 20) + '\n') for channel in self.client.channels]
        workspace = self.controller.workspace or {}
        current = workspace.get('id')
        rows = [dict(row, name=workspace.get('name'), archived=workspace.get('archived'))
                if row['id'] == current else row for row in self._sessions]
        counts = Counter(label_text(row.get('name') or row['id']) for row in rows)
        fragments = [('class:muted', ' Loading sessions…\n')] if self._sessions_loading else [('', '\n')] if rows else []
        for row in rows:
            ident = row['id']
            name = label_text(row.get('name') or ident)
            suffix = (' [' + label_text(ident)[-6:] + ']') if counts[name] > 1 else ''
            selected = ident == self.state.selected_session_id
            if selected:
                fragments.append(('[SetCursorPosition]', ''))
            prefix = ' ● ' if ident == current else ' › ' if selected else '   '
            # Leave one cell at either edge, including when suffixes disambiguate names.
            label = prefix + clip_cells(name, max(0, 16 - get_cwidth(suffix))) + suffix
            label += ' ' * max(0, 20 - get_cwidth(label))
            def activate(event, ident=ident):
                if (event.event_type == MouseEventType.MOUSE_UP and self.dialogs.future is None
                        and self.screen_mode == 'wide' and not self.controller.plain_channel
                        and any(row['id'] == ident for row in self._sessions)):
                    self.state.selected_session_id = ident
                    self.focus_named('navigation')
                    self._app().create_background_task(self.callbacks['run_action'](
                        'select_session', target_id=ident))
                    return None
                return NotImplemented
            style = 'class:session.selected' if selected else 'class:session.current' if ident == current else ''
            fragments.append((style, clip_cells(label, 20), activate))
            fragments.append(('', '\n'))
            if row.get('archived'):
                fragments.append(('class:muted', '   archived\n'))
            fragments.append(('', '\n'))
        if not fragments:
            fragments.append(('', 'No sessions yet\n'))
        return fragments

    def agent_rows(self):
        """Transient validated references into the authoritative workspace."""
        return [agent for agent in (self.controller.workspace or {}).get('agents', [])
                if isinstance(agent, dict) and isinstance(agent.get('agent_id'), str)
                and agent['agent_id']]

    def _selected_agent(self):
        return next((agent for agent in self.agent_rows()
                     if agent['agent_id'] == self.state.selected_agent_id), None)

    def inspector_text(self):
        agent = self._selected_agent()
        if agent is None:
            return 'Select an agent to inspect.'
        lines = ['Agent: ' + label_text(agent['agent_id']),
                 label_text(self.controller._agent_status(agent)),
                 'Native session: ' + ('id present' if agent.get('native_session_id') else 'id unknown')]
        for key, caption in [('cwd', 'Working directory'), ('history_note', 'History'),
                             ('last_error', 'Error')]:
            if agent.get(key):
                separator = ': ' if key == 'cwd' else ':\n'
                lines.append(caption + separator + body_text(agent[key]))
        return '\n'.join(lines)

    def show_inspector(self):
        self.inspecting = True
        self._inspector_line = 0
        self.focus_named('agents')

    def action_choices(self):
        """Stable UI action IDs with current, readable disabled reasons."""
        workspace = self.controller.workspace
        agent = self._selected_agent()
        choices = [('help', 'Help', 'Keyboard and command help'),
                   ('restart_server', 'Restart server', 'Restart connected local server; keep agents and drafts'),
                   ('update', 'Update yapp', 'Install the latest release and reopen yapp; keeps agents and drafts'),
                   ('loop_guard', 'Loop guard', 'Set the agent-to-agent hop limit for all sessions'),
                   ('stop_all', 'Stop all agents', 'Stop agents across every session; keep history for Resume'),
                   ('activity', 'Activity', 'Read diagnostics; F5 opens Activity, Esc returns'),
                   ('clear_draft', 'Clear draft', 'Clear the current unsent message')]
        if self.controller.plain_channel:
            choices += [('create_channel', 'Create channel', 'Create a chat channel'),
                        ('switch_channel', 'Switch channel', 'Choose a chat channel'),
                        ('history', 'History', 'Show channel history')]
        else:
            orchestrator = workspace.get('orchestrator', {}) if workspace else {}
            orchestrator_label = 'Resume orchestrator' if orchestrator.get('agent_id') else 'Enable orchestrator'
            choices += [('new_session', 'New session', 'Create a session'),
                        ('switch_session', 'Switch session', 'Choose another session'),
                        ('rename_session', 'Rename session', 'Rename the active session'),
                        ('archive_session', 'Archive session', 'Archive the active session'),
                        ('refresh', 'Refresh', 'Fetch the latest session list'),
                        ('enable_orchestrator', orchestrator_label,
                         'Configure or resume this session orchestrator'),
                        ('new_agent', 'Add agent', 'Start an agent in this session'),
                        ('attach', 'Attach', 'Open the selected agent terminal'),
                        ('resume', 'Resume agent', 'Resume the selected stopped agent'),
                        ('stop', 'Stop agent', 'Stop the selected agent'),
                        ('remove', 'Remove agent', 'Stop its terminal and remove the saved agent entry'),
                        ('unread', 'Unread', 'Inspect unread messages'),
                        ('retry', 'Retry unread', 'Retry delivery of unread messages'),
                        ('history', 'History settings', 'Inspect and change selected agent history mode'),
                        ('inspect_agent', 'Inspect agent', 'Show full status and recovery details')]
        choices.append(('quit', 'Quit', 'Disconnect' if self.controller.plain_channel else 'Checkpoint and disconnect'))
        agent_actions = {'attach', 'resume', 'stop', 'remove', 'unread', 'retry', 'inspect_agent'}
        session_actions = agent_actions | {'rename_session', 'archive_session', 'new_agent', 'history',
                                           'enable_orchestrator'}
        result = []
        for ident, label, description in choices:
            reason = ''
            if ident == 'stop_all' and sys.platform == 'win32':
                reason = WINDOWS_TMUX_ERROR
            if not self.controller.plain_channel:
                if ident in session_actions and workspace is None:
                    reason = 'Select a session first'
                elif ident in agent_actions and agent is None:
                    reason = 'Select an agent first'
                elif ident == 'attach' and not agent.get('tmux_session'):
                    reason = 'Agent has no terminal; resume it first'
                elif ident == 'stop' and agent.get('last_state') not in ('running', 'starting'):
                    reason = 'Agent is already stopped'
                elif ident == 'resume' and agent.get('last_state') in ('running', 'starting'):
                    reason = 'Agent is already running'
                if ident == 'new_agent' and workspace is not None and not self.controller.providers:
                    reason = 'No providers configured'
                if ident == 'enable_orchestrator' and orchestrator.get('enabled'):
                    reason = 'Orchestrator is already enabled'
                if ident in {'new_agent', 'attach', 'resume', 'stop', 'remove'} and sys.platform == 'win32':
                    reason = WINDOWS_TMUX_ERROR
            if self.controller.selection_pending and ident not in {'help', 'activity', 'quit'}:
                reason = 'Session selection in progress'
            result.append(dict(id=ident, label=label, description=description, disabled_reason=reason))
        return result

    def _agent_live(self, agent):
        live = self.client.status.get(agent.get('registry_name'), {})
        return live if self.client.websocket is not None and isinstance(live, dict) else {}

    def _agent_waiting(self, agent):
        return (agent.get('last_state') in ('running', 'starting') and
                self._agent_live(agent).get('waiting_for_input', agent.get('waiting_for_input')) is True)

    def pending_input_rows(self):
        """Waiting agents from the open session, never other sidebar sessions."""
        if self.controller.plain_channel:
            return []
        return [agent for agent in self.agent_rows() if self._agent_waiting(agent)]

    def _pending_fragments(self):
        rows = self.pending_input_rows()
        if not rows:
            return [('class:muted', 'No pending input' if self.controller.workspace else 'Select a session')]
        fragments = []
        for agent in rows:
            ident = agent['agent_id']
            selected = ident == self.state.selected_agent_id
            if selected:
                fragments.append(('[SetCursorPosition]', ''))
            def click(event, ident=ident):
                if event.event_type == MouseEventType.MOUSE_UP:
                    self._review_pending(ident)
            text = ('› ' if selected else '  ') + '! ' + label_text(agent.get('registry_name') or ident)
            fragments.append(('class:agent.input' + (' underline' if selected else ''),
                              clip_cells(text, 20) + '\n', click))
        return fragments

    def _review_pending(self, ident=None):
        if (self.screen_mode != 'wide' or self.controller.plain_channel
                or self.dialogs.future is not None or self.help_visible):
            return
        ids = [agent['agent_id'] for agent in self.pending_input_rows()]
        if ident is None:
            ident = self.state.selected_agent_id if self.state.selected_agent_id in ids else next(iter(ids), None)
        if ident not in ids:
            return
        self.state.selected_agent_id = ident
        self.focus_named('pending_inputs')
        self._app().create_background_task(self.callbacks['run_action']('attach', target_id=ident))

    def _show_add_agent(self):
        return not self.controller.plain_channel and self.controller.workspace is not None

    def _add_agent(self):
        if (not self._show_add_agent() or self.screen_mode == 'small'
                or self.dialogs.future is not None or self.help_visible
                or self.controller.selection_pending):
            return
        self._app().create_background_task(self.callbacks['run_action']('new_agent'))

    def _show_attach(self):
        selected = self._selected_agent()
        candidates = [selected] if selected is not None else self.agent_rows()
        return (not self.controller.plain_channel and self._input_agent() is None
                and any(agent.get('tmux_session') for agent in candidates))

    def _direct_attach(self, *, choose=False):
        if (self.screen_mode == 'small' or self.controller.plain_channel
                or self.dialogs.future is not None or self.help_visible
                or self.controller.selection_pending or not self.agent_rows()):
            return
        self._app().create_background_task(self.callbacks['run_action'](
            'choose_attach' if choose else 'attach',
            target_id=None if choose else self.state.selected_agent_id))

    def _input_agent(self):
        selected = self._selected_agent()
        if selected is not None:
            return selected if self._agent_waiting(selected) else None
        return next((agent for agent in self.agent_rows() if self._agent_waiting(agent)), None)

    def _review_input(self):
        agent = self._input_agent()
        if agent is not None and self.dialogs.future is None:
            self.state.selected_agent_id = agent['agent_id']
            self._app().create_background_task(self.callbacks['run_action'](
                'attach', target_id=agent['agent_id']))

    def _agent_badge(self, agent):
        state = agent.get('last_state')
        if self._agent_waiting(agent):
            return 'input', '! Input · Attach'
        if state == 'starting':
            return 'starting', '◌ Starting'
        if state == 'running':
            return ('working', '◆ Working') if self._agent_live(agent).get('busy') else ('ready', '● Ready')
        if state == 'failed' or agent.get('last_error') or agent['agent_id'] in self.controller._failed_launches:
            return 'error', '× Error'
        return 'stopped', '○ Stopped'

    def _agent_content_width(self):
        if self.screen_mode == 'wide' and not self.inspecting:
            return 20
        return max(1, self._app().output.get_size().columns -
                   (22 if self.screen_mode == 'wide' else 0) - (2 if self.inspecting else 0))

    def _agent_fragments(self):
        if self.inspecting:
            width = self._agent_content_width()
            lines = markdown_lines(self.inspector_text(), width, raw=self.selecting_text)
            offset = min(self._inspector_line, max(0, len(lines) - 1))
            if not self.selecting_text:
                self._inspector_line = offset
            return join_lines(lines[offset:])
        agents = self.agent_rows()
        if not agents:
            return [('class:muted', 'No agents.\nClick Add agent\nto start.' if self.screen_mode == 'wide'
                     else 'No agents. Click Add agent to start.')]
        fragments = []
        width = self._agent_content_width()
        name_width = min(20, max(8, width - 30), max(get_cwidth(label_text(
            a.get('registry_name') or a['agent_id'])) for a in agents))
        for agent in agents:
            selected = agent['agent_id'] == self.state.selected_agent_id
            if selected:
                fragments.append(('[SetCursorPosition]', ''))
            def select(event, ident=agent['agent_id']):
                if event.event_type == MouseEventType.MOUSE_UP and self.dialogs.future is None:
                    self.state.selected_agent_id = ident
                    self.focus_named('agents')
            kind, badge = self._agent_badge(agent)
            profile = agent.get('profile') if isinstance(agent.get('profile'), dict) else {}
            label = ('orchestrator' if agent.get('kind') == 'orchestrator'
                     else profile.get('role'))
            if label:
                badge += ' · ' + label_text(label)
            count = agent.get('unread_count')
            unread = '  ✉ ' + ('99+' if count > 99 else str(count)) if isinstance(count, int) and count > 0 else ''
            if self.screen_mode == 'wide':
                name = clip_cells(label_text(agent.get('registry_name') or agent['agent_id']),
                                  max(1, width - 2 - get_cwidth(unread)))
                fragments.extend([
                    ('class:selected' if selected else 'bold',
                     ('› ' if selected else '  ') + name, select),
                    ('class:agent.unread', unread, select), ('', '\n', select),
                    ('class:agent.' + kind, '  ' + clip_cells(badge, width - 2), select),
                    ('', '\n\n', select)])
                continue
            name = clip_cells(label_text(agent.get('registry_name') or agent['agent_id']), name_width)
            parts = [('class:selected' if selected else '', ('› ' if selected else '  ') +
                      name + ' ' * max(0, name_width - get_cwidth(name)) + '  '),
                     ('class:agent.' + kind, badge), ('class:agent.unread', unread)]
            cwd = agent.get('cwd')
            if isinstance(cwd, str) and cwd.strip():
                parts.append(('class:muted', '  ' + label_text(cwd)))
            remaining = width
            for style, text in parts:
                visible = clip_cells(text, remaining) if remaining > 0 else ''
                fragments.append((style, visible, select))
                remaining -= get_cwidth(visible)
            fragments.append(('', '\n', select))
        return fragments

    def _conversation_title(self):
        workspace = self.controller.workspace
        name = (workspace.get('name') or workspace['id']) if workspace else '#' + self.client.channel
        title = ('Conversation' if self._show_appbar() else
                 label_text(name) + ' · ' + label_text(self.client.connection_state).capitalize())
        if self.state.viewport.new_ids:
            title += f' · {len(self.state.viewport.new_ids)} new'
        return clip_cells(title, max(1, self._app().output.get_size().columns - 30))

    def _show_appbar(self):
        return self._app().output.get_size().rows >= 24 and not self.selecting_text

    def _appbar(self):
        workspace = self.controller.workspace
        name = (workspace.get('name') or workspace['id']) if workspace else '#' + self.client.channel
        connected = self.client.connection_state == 'connected'
        status = ('● ' if connected else '○ ') + label_text(self.client.connection_state).capitalize() + '  '
        brand = '  yapp  '
        width = self._app().output.get_size().columns
        session = clip_cells('  ' + label_text(name), max(0, width - len(brand) - get_cwidth(status) - 2))
        gap = ' ' * max(1, width - len(brand) - get_cwidth(session) - get_cwidth(status))
        return [('class:appbar.brand', brand), ('class:appbar.session', session), ('', gap),
                ('class:appbar.connected' if connected else 'class:appbar.connecting', status)]

    def _footer(self):
        if self.selecting_text:
            return [('class:mode.copy', ' COPY '),
                    ('class:status.starting', '  Select text: drag · Ctrl+Shift+C copy · F7 return')]
        if self.screen_mode == 'small':
            return [('class:muted', 'F1 Help · Ctrl+Q Quit')]
        mode = [('class:mode.' + self.composer_mode.lower(), ' ' + self.composer_mode + ' '), ('', '  ')]
        compact = self.screen_mode == 'compact'
        shortcuts = [('F2', 'Channels' if self.controller.plain_channel else 'Sessions'),
                     ('F3', 'Agents'), ('F4', 'Menu' if compact else 'Commands'),
                     ('F5', ('Chat' if compact else 'Back to chat') if self.activity_visible else
                            ('Log' if compact else 'Activity')),
                     ('F7', 'Copy'), ('F1', 'Help'), ('Ctrl+Q', 'Quit')]
        for key, label in shortcuts:
            mode.extend([('class:footer.key', key), ('', ' ' + label + '  ')])
        return mode

    def _cancel_overlay(self):
        if self.screen_mode == 'small':
            return
        if self.activity_visible:
            self.hide_activity()
        elif self.inspecting:
            self.inspecting = False
            self._app().invalidate()

    def _bindings(self):
        bindings = KeyBindings()
        copying = Condition(lambda: self.selecting_text)

        @bindings.add(Keys.Any, eager=True, filter=copying & Condition(lambda:
            self.dialogs.future is None and not self.help_visible))
        def preserve_copy_input(event):
            # Read-only buffers still accept selection and cursor shortcuts.
            # Native copying owns these keys until F7 restores editor input.
            pass

        editing_allowed = Condition(lambda:
            self.screen_mode != 'small' and self.dialogs.future is None
            and not self.help_visible and not self.selecting_text)
        composer_focused = has_focus(self.composer) & editing_allowed
        normal = composer_focused & Condition(lambda: self.composer_mode == 'NORMAL')
        insert_entry = editing_allowed & (~has_focus(self.composer) |
                                         Condition(lambda: self.composer_mode == 'NORMAL'))

        @bindings.add('i', filter=insert_entry)
        @bindings.add('I', filter=insert_entry)
        @bindings.add('a', filter=insert_entry)
        @bindings.add('A', filter=insert_entry)
        def insert(event):
            if not self.focus_named('composer'):
                return
            buffer = self.composer.buffer
            key = event.data
            if key == 'I':
                buffer.cursor_position += buffer.document.get_start_of_line_position(after_whitespace=True)
            elif key == 'A':
                buffer.cursor_position += buffer.document.get_end_of_line_position()
            elif key == 'a':
                buffer.cursor_right()
            self.composer_mode = 'INSERT'

        @bindings.add(Keys.Any, filter=normal)
        def normal_key(event):
            buffer = self.composer.buffer
            motions = {'h': buffer.cursor_left, 'l': buffer.cursor_right,
                       'j': buffer.cursor_down, 'k': buffer.cursor_up}
            if event.data in motions:
                motions[event.data]()
            elif event.data == 'w':
                buffer.cursor_position += buffer.document.find_next_word_beginning() or 0
            elif event.data == 'b':
                buffer.cursor_position += buffer.document.find_start_of_previous_word() or 0
            elif event.data == '0':
                buffer.cursor_position += buffer.document.get_start_of_line_position()
            elif event.data == '$':
                buffer.cursor_position += buffer.document.get_end_of_line_position()

        @bindings.add(Keys.BracketedPaste, filter=composer_focused)
        def paste(event):
            self.composer_mode = 'INSERT'
            self.composer.buffer.insert_text(event.data.replace('\r\n', '\n').replace('\r', '\n'))

        @bindings.add('escape', filter=composer_focused,
                      eager=Condition(lambda: not self._app().key_processor.input_queue))
        def normal_mode(event):
            self.normal_composer()

        def dispatch(key, callback, *args):
            @self.global_key_bindings.add(key, eager=copying, filter=Condition(lambda:
                (key in ('f1', 'c-q') or self.screen_mode != 'small') and
                (key in ('f1', 'c-q') or self.dialogs.future is None and not self.help_visible)))
            def invoke(event):
                event.app.create_background_task(self.callbacks[callback](*args))

        dispatch('f1', 'run_action', 'help')
        dispatch('f2', 'navigate', False)
        @self.global_key_bindings.add('f3', eager=copying, filter=Condition(lambda:
            self.screen_mode != 'small' and self.dialogs.future is None and not self.help_visible))
        def agent_actions(event):
            # Always start at the list, including a single previously selected agent.
            event.app.create_background_task(self.callbacks['run_action'](
                'agents', target_id=None))
        @self.global_key_bindings.add('f6', eager=copying)
        def attach_agent(event):
            self._direct_attach(choose=True)

        dispatch('f4', 'run_action', 'commands')
        dispatch('c-q', 'quit')

        @self.global_key_bindings.add('f5', eager=copying, filter=Condition(lambda:
            self.screen_mode != 'small' and self.dialogs.future is None and not self.help_visible))
        def activity(event):
            self.hide_activity() if self.activity_visible else self.show_activity()

        @self.global_key_bindings.add('f7', eager=copying, filter=Condition(lambda:
            self.selecting_text or self.screen_mode != 'small'
            and self.dialogs.future is None))
        def select_text(event):
            # Let the terminal own drag selection and its native clipboard shortcut.
            self.selecting_text = not self.selecting_text
            if (not self.selecting_text and self.dialogs.future is None
                    and not self.help_visible):
                # Copying often starts after clicking the transcript. Return to
                # the editor instead of leaving its shortcuts on another control.
                if not self.focus_named('composer'):
                    event.app.layout.current_window = self.composer.window
                    self.state.focus_name = 'composer'
            event.app.invalidate()

        @bindings.add('c-c')
        def preserve(event):
            self._cancel_overlay()

        @bindings.add('tab')
        @bindings.add('s-tab')
        def focus(event):
            if self.screen_mode == 'small':
                return
            names = ['navigation', 'new_session', 'conversation', 'agents', 'agent_actions', 'composer',
                     'clear_draft', 'restart_server'] if self.screen_mode == 'wide' else [
                'conversation', 'agents', 'agent_actions', 'composer', 'clear_draft', 'restart_server']
            if self.screen_mode == 'wide' and not self.controller.plain_channel:
                names.insert(names.index('conversation'), 'pending_inputs')
            if self._input_agent() is not None:
                names.insert(names.index('agent_actions'), 'agent_review')
            elif self._show_attach():
                names.insert(names.index('agent_actions'), 'agent_attach')
            if self._show_add_agent():
                names.insert(names.index('agent_actions') + 1, 'agent_add')
            if self.activity_visible:
                names[names.index('conversation')] = 'activity'
            if self.controller.plain_channel:
                names.remove('agents')
                names.remove('agent_actions')
                if 'new_session' in names:
                    names.remove('new_session')
            current = next((name for name in names if event.app.layout.current_control is
                            getattr(getattr(self, name), 'control', getattr(self, name))),
                           self.state.focus_name)
            offset = -1 if event.key_sequence[-1].key == 's-tab' else 1
            self.focus_named(names[(names.index(current) + offset) % len(names)] if current in names else 'composer')

        @bindings.add('up', filter=has_focus(self.pending_inputs))
        @bindings.add('down', filter=has_focus(self.pending_inputs))
        def move_pending(event):
            if self.screen_mode != 'wide' or self.controller.plain_channel:
                return
            ids = [agent['agent_id'] for agent in self.pending_input_rows()]
            if not ids:
                return
            offset = -1 if event.key_sequence[-1].key == 'up' else 1
            selected = self.state.selected_agent_id
            index = ids.index(selected) if selected in ids else (-1 if offset > 0 else len(ids))
            self.state.selected_agent_id = ids[max(0, min(len(ids) - 1, index + offset))]

        @bindings.add('enter', filter=has_focus(self.pending_inputs))
        def review_pending(event):
            self._review_pending()

        @bindings.add('up', filter=has_focus(self.navigation) | has_focus(self.agents))
        @bindings.add('down', filter=has_focus(self.navigation) | has_focus(self.agents))
        def move(event):
            if self.screen_mode == 'small':
                return
            offset = -1 if event.key_sequence[-1].key == 'up' else 1
            if event.app.layout.current_control is self.agents and self.inspecting:
                self._inspector_line = max(0, self._inspector_line + offset)
                return
            navigation = event.app.layout.current_control is self.navigation
            if navigation and self.screen_mode != 'wide':
                return
            if navigation and self.controller.plain_channel:
                return  # Channel selection belongs to the channel navigation workflow.
            ids = ([row['id'] for row in self._sessions] if navigation else
                   [agent['agent_id'] for agent in self.agent_rows()])
            attribute = 'selected_session_id' if navigation else 'selected_agent_id'
            selected = getattr(self.state, attribute)
            if ids:
                index = ids.index(selected) if selected in ids else (-1 if offset > 0 else len(ids))
                setattr(self.state, attribute, ids[max(0, min(len(ids) - 1, index + offset))])

        @bindings.add('enter', filter=has_focus(self.navigation) | has_focus(self.agents))
        def activate(event):
            if self.screen_mode == 'small':
                return
            if event.app.layout.current_control is self.navigation:
                if self.screen_mode != 'wide':
                    return
                if self.controller.plain_channel:
                    event.app.create_background_task(self.callbacks['run_action']('switch_channel'))
                elif self.state.selected_session_id is not None:
                    event.app.create_background_task(self.callbacks['run_action'](
                        'select_session', target_id=self.state.selected_session_id))
                else:
                    event.app.create_background_task(self.callbacks['run_action']('new_session'))
            else:
                event.app.create_background_task(self.callbacks['run_action'](
                    'agents', target_id=self.state.selected_agent_id))

        @bindings.add('escape', filter=has_focus(self.agents) | has_focus(self.agent_actions) | has_focus(self.agent_review) | has_focus(self.agent_attach) | has_focus(self.agent_add) | has_focus(self.pending_inputs),
                      eager=Condition(lambda: not self._app().key_processor.input_queue))
        def close_inspector(event):
            self.inspecting = False
            self.hide_activity()
            self.focus_named('composer')

        @bindings.add('pageup', filter=has_focus(self.conversation))
        @bindings.add('pagedown', filter=has_focus(self.conversation))
        @bindings.add('end', filter=has_focus(self.conversation))
        def scroll_conversation(event):
            if self.screen_mode == 'small':
                return
            key = event.key_sequence[-1].key
            if key == 'end':
                self.state.viewport.mark_seen()
            else:
                self.conversation.page(-1 if key == 'pageup' else 1)

        @bindings.add('up', filter=has_focus(self.activity))
        @bindings.add('down', filter=has_focus(self.activity))
        @bindings.add('pageup', filter=has_focus(self.activity))
        @bindings.add('pagedown', filter=has_focus(self.activity))
        @bindings.add('home', filter=has_focus(self.activity))
        @bindings.add('end', filter=has_focus(self.activity))
        def scroll_activity(event):
            key = event.key_sequence[-1].key
            if key == 'home':
                self._activity_line = 0
            elif key == 'end':
                self._activity_line = self._activity_max_line
            else:
                offset = self.activity.height if key in ('pageup', 'pagedown') else 1
                if key in ('up', 'pageup'):
                    offset = -offset
                self._activity_line = max(0, min(self._activity_max_line, self._activity_line + offset))

        @bindings.add('escape', filter=Condition(lambda: self.activity_visible),
                      eager=Condition(lambda: not self._app().key_processor.input_queue))
        def close_activity(event):
            self.hide_activity()

        @bindings.add('escape', Keys.Any, filter=has_focus(self.agents) | has_focus(self.agent_actions) | has_focus(self.agent_review) | has_focus(self.agent_attach) | has_focus(self.agent_add) | has_focus(self.pending_inputs) | has_focus(self.activity))
        def consume_alt(event):
            pass

        return bindings
