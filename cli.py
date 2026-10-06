"""Interactive chat and shell commands for a running local yapp server."""

import argparse
import asyncio
from collections import OrderedDict, deque
from contextlib import redirect_stdout
import json
import os
import re
import sys
import uuid
from urllib.error import URLError
from urllib.parse import urlencode

from cli_api import (CLIError, SessionTokenParser, fetch_session_token, get_api,
                     local_url)
from cli_workspaces import (WorkspaceAPI, attach_agent, format_workspace_result,
                            require_tmux_platform, resolve_agent, run_workspace_command)
from cli_workspace_chat import WorkspaceChatController, ensure_server
from cli_view_contracts import (SubmitOutcome, ViewEvent, channel_transcript,
                                terminal_text)
from config_loader import load_config


HELP = """/channels           List channels
/join NAME          Switch to an existing channel
/create NAME        Create a channel
/agents             Show agent availability and roles
/history            Show recent messages in this channel
/jobs               List jobs
/rules              List rules
/help               Show commands
/quit               Disconnect
Type a message to send it; @mentions wake agents. Tab completes names.
Server commands such as /continue and /summary @agent are sent to chat."""


class ChatClient:
    def __init__(self, url, channel="general", username=None, history_limit=30,
                 output=print):
        self.url = local_url(url)
        self.channel = channel.removeprefix("#")
        self.username = username
        self.history_limit = history_limit
        self.output = output
        self.channels = []
        self.status = {}
        self.agent_names = []
        self.jobs = []
        self.rules = []
        self.messages = OrderedDict()
        self.websocket = None
        self.ready = asyncio.Event()
        self.pending_channel = None
        self.on_workspace = None
        self.on_settings = None
        self.on_view_change = None
        self.view_revision = 0
        self.connection_state = "connecting"
        self._output_paused = False
        self._output_buffer = deque(maxlen=10000)
        self._output_omitted = 0

    def show(self, text, *, immediate=False):
        text = terminal_text(text)
        if self._output_paused and not immediate:
            for line in text.split('\n'):
                if len(self._output_buffer) == self._output_buffer.maxlen:
                    self._output_omitted += 1
                self._output_buffer.append(line)
        else:
            self.output(text)

    def pause_output(self):
        self._output_paused = True

    def resume_output(self):
        self._output_paused = False
        if self._output_omitted:
            omitted, self._output_omitted = self._output_omitted, 0
            self.output(f'[{omitted} buffered lines omitted]')
        while self._output_buffer:
            self.output(self._output_buffer.popleft())

    def remember(self, message):
        key = message["id"]
        fresh = key not in self.messages
        self.messages[key] = message
        while len(self.messages) > 10000:
            self.messages.popitem(last=False)
        return fresh

    def _notify_view(self, kind, *, message_ids=(), text=None, old_channel=None, new_channel=None):
        self.view_revision += 1
        if self.on_view_change is not None:
            self.on_view_change(ViewEvent('client', kind, self.view_revision,
                                          message_ids=tuple(message_ids), text=text,
                                          old_channel=old_channel, new_channel=new_channel))

    def _set_connection_state(self, state, *, force=False):
        if not force and self.connection_state == state:
            return
        self.connection_state = state
        self._notify_view("connection")

    def show_message(self, message):
        if self.on_view_change is not None:
            message_id = message.get("id")
            self._notify_view("messages", message_ids=(() if message_id is None else (message_id,)))
            return
        self.show(f"[{message.get('time', '')}] {message.get('sender', '?')}: "
                  f"{message.get('text', '')}")
        for attachment in message.get("attachments", []):
            self.show(f"  Attachment: {attachment.get('url') or attachment.get('name', 'image')}")
        choices = message.get("metadata", {}).get("choices", [])
        if choices:
            self.show("  Choices: " + " | ".join(map(str, choices)))

    def history(self):
        messages = channel_transcript(self.messages, self.channel)[-self.history_limit:]
        if self.on_view_change is not None:
            self._notify_view("history", message_ids=(m["id"] for m in messages))
            return
        self.show(f"# {self.channel}")
        for message in messages:
            self.show_message(message)

    def handle_event(self, event):
        kind = event.get("type")
        data = event.get("data", {})
        if kind == "settings":
            show_history = False
            self.channels = data.get("channels", ["general"])
            if self.username is None:
                self.username = data.get("username", "user")
            if self.on_settings is not None:
                self.on_settings(data)
            elif self.pending_channel in self.channels:
                self.channel = self.pending_channel
                self.pending_channel = None
                show_history = True
            elif self.channel not in self.channels:
                self.show(f"Channel {self.channel!r} is unavailable; using #general.")
                self.channel = "general"
            self._notify_view("settings")
            if show_history and self.on_view_change is None:
                self.history()
        elif kind == "agents":
            self.agent_names = list(data)
            self._notify_view("status")
        elif kind == "status":
            self.status = data
            self._notify_view("status")
        elif kind == "workspace" and self.on_workspace is not None:
            self.on_workspace(data)
        elif kind == "jobs":
            self.jobs = data
        elif kind == "rules":
            self.rules = data
        elif kind in ("job", "rule"):
            records = self.jobs if kind == "job" else self.rules
            records[:] = [r for r in records if r["id"] != data.get("id")]
            if event.get("action") != "delete" and "id" in data:
                records.append(data)
        elif kind == "history":
            messages = event.get("messages", [])
            for message in messages:
                self.remember(message)
            if messages:
                # Invalidate presentation ordering without publishing partial history.
                self.view_revision += 1
        elif kind == "history_complete":
            self.ready.set()
            self.history()
        elif kind == "message":
            fresh = self.remember(data)
            if fresh and self.ready.is_set() and data.get("channel", "general") == self.channel:
                self.show_message(data)
            else:
                self._notify_view("messages", message_ids=(data["id"],))
        elif kind == "message_update":
            updated = event["message"]
            self.remember(updated)
            self._notify_view("messages", message_ids=(updated["id"],))
        elif kind == "delete":
            ids = event.get("ids", [])
            for key in ids:
                self.messages.pop(key, None)
            self._notify_view("messages", message_ids=ids)
        elif kind == "clear":
            channel = event.get("channel")
            removed = tuple(k for k, m in self.messages.items()
                            if not channel or m.get("channel", "general") == channel)
            self.messages = OrderedDict((k, m) for k, m in self.messages.items()
                                        if channel and m.get("channel", "general") != channel)
            self._notify_view("messages", message_ids=removed)
            self.show(f"History cleared: #{channel or 'all channels'}")
        elif kind == "agent_renamed":
            changed = []
            for key, message in self.messages.items():
                if message.get("sender") == event.get("old_name"):
                    message["sender"] = event["new_name"]
                    changed.append(key)
            self._notify_view("messages", message_ids=changed)
        elif kind == "channel_renamed":
            changed = []
            for key, message in self.messages.items():
                if message.get("channel") == event["old_name"]:
                    message["channel"] = event["new_name"]
                    changed.append(key)
            self._notify_view("channel", message_ids=changed,
                              old_channel=event['old_name'], new_channel=event['new_name'])
        elif kind.endswith("_error"):
            self.show(event.get("error", "Request failed"))

    async def receive_forever(self):
        from websockets.asyncio.client import connect
        from websockets.exceptions import WebSocketException

        self._set_connection_state("connecting", force=True)
        while True:
            try:
                token = await asyncio.to_thread(fetch_session_token, self.url)
                uri = "ws" + self.url[4:] + "/ws?" + urlencode({"token": token})
                async with connect(uri, proxy=None, open_timeout=5,
                                   close_timeout=2, max_size=16 * 1024 * 1024) as socket:
                    self.websocket = socket
                    self.messages.clear()
                    self._set_connection_state("connected")
                    self.show(f"Connected to {self.url}")
                    async for raw in socket:
                        self.handle_event(json.loads(raw))
            except (OSError, ValueError, TimeoutError, WebSocketException):
                # Exception URLs may contain the token; never print them.
                self.show("Connection unavailable. Start run.py; retrying in 3 seconds.")
            finally:
                self.websocket = None
                self.ready.clear()
                self._set_connection_state("reconnecting")
            await asyncio.sleep(3)

    async def send(self, event):
        from websockets.exceptions import WebSocketException

        if not self.ready.is_set() or self.websocket is None:
            self.show("Disconnected. Message was not sent.")
            return False
        try:
            await self.websocket.send(json.dumps(event))
            return True
        except (OSError, WebSocketException):
            self.show("Connection lost. Delivery is uncertain; check history before retrying.")
            return False

    async def submit(self, text):
        return (await self.submit_outcome(text)).keep_running

    async def submit_outcome(self, text):
        text = text.strip()
        if not text:
            return SubmitOutcome("completed")
        command, _, argument = text.partition(" ")
        argument = argument.strip().removeprefix("#")
        if command in ("/quit", "/exit"):
            return SubmitOutcome("completed", keep_running=False)
        if command == "/help":
            self.show(HELP)
        elif command == "/channels":
            self.show("  ".join("#" + ch for ch in self.channels) or "Connecting...")
        elif command == "/join":
            if argument not in self.channels:
                message = "Unknown channel. Use /channels or /create NAME."
                self.show(message)
                return SubmitOutcome("failed", message=message)
            else:
                self.channel = argument
                self.history()
        elif command == "/create":
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,19}", argument):
                message = "Use 1-20 lowercase letters, numbers, or hyphens; start with a letter or number."
                self.show(message)
                return SubmitOutcome("failed", message=message)
            elif argument in self.channels:
                self.channel = argument
                self.history()
            else:
                self.pending_channel = argument
                accepted = await self.send({"type": "channel_create", "name": argument})
                if not accepted:
                    self.pending_channel = None
                    return SubmitOutcome("failed")
                return SubmitOutcome("completed", sent=True)
        elif command == "/history":
            self.history()
        elif command == "/agents":
            for name, info in self.status.items():
                if isinstance(info, dict):
                    state = "busy" if info.get("busy") else "online" if info.get("available") else "offline"
                    self.show(f"@{name}: {state}" + (f" ({info['role']})" if info.get("role") else ""))
            if self.status.get("paused"):
                self.show("An agent conversation is paused by the loop guard.")
        elif command in ("/jobs", "/rules"):
            records = self.jobs if command == "/jobs" else self.rules
            for record in records:
                self.show(f"{record['id']} [{record.get('status', '')}] "
                          f"{record.get('title', record.get('text', ''))}")
            if not records:
                self.show("No records.")
        else:
            accepted = await self.send({"type": "message", "text": text,
                                        "channel": self.channel, "sender": self.username})
            return SubmitOutcome("completed" if accepted else "failed", sent=accepted)
        return SubmitOutcome("completed")


async def interactive(client, controller=None):
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.patch_stdout import patch_stdout

    commands = ["/channels", "/join", "/create", "/agents", "/history",
                "/jobs", "/rules", "/help", "/quit", "/continue", "/summary"]
    session = PromptSession(completer=WordCompleter(
        lambda: commands + ["@" + n for n in client.agent_names] + client.channels
                + (controller.completion_words() if controller is not None else []),
        WORD=True))

    async def prompt(text, default=""):
        # Confirmation defaults apply to empty answers, not editable input.
        answer = await session.prompt_async(text)
        return answer if answer.strip() else default

    with patch_stdout():
        if controller is not None and not await controller.initialize(prompt):
            return
        client.show("yapp terminal | /help for commands | /quit to exit")
        receiver = asyncio.create_task(client.receive_forever())
        tasks = [receiver]
        try:
            if controller is not None and not controller.plain_channel:
                tasks.append(asyncio.create_task(controller.poll_forever()))
            while True:
                try:
                    text = await session.prompt_async(lambda: f"#{terminal_text(client.channel)} > ")
                except KeyboardInterrupt:
                    continue
                except EOFError:
                    break
                action = await controller.handle(text) if controller is not None else None
                if action == 'quit':
                    break
                if action is None and not await client.submit(text):
                    break
        finally:
            try:
                if controller is not None:
                    await controller.close()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


async def shell_command(client, args):
    from websockets.asyncio.client import connect

    try:
        token = await asyncio.to_thread(fetch_session_token, client.url)
    except (OSError, URLError):
        raise CLIError("Could not connect to the local yapp server") from None
    if args.command == "status":
        return await asyncio.to_thread(get_api, client.url, token, "/api/status")
    settings = await asyncio.to_thread(get_api, client.url, token, "/api/settings")
    client.handle_event({"type": "settings", "data": settings})
    requested_channel = args.channel.removeprefix("#")
    if args.command == "channels":
        return settings["channels"]
    if requested_channel not in settings["channels"]:
        raise ValueError(f"Unknown channel: {requested_channel}")
    if args.command == "read":
        query = urlencode({"channel": client.channel, "limit": args.history})
        return await asyncio.to_thread(get_api, client.url, token, "/api/messages?" + query)

    text = " ".join(args.message).strip()
    if not text:
        raise ValueError("Message must not be empty")
    request_id = uuid.uuid4().hex
    uri = "ws" + client.url[4:] + "/ws?" + urlencode({"token": token})
    async with connect(uri, proxy=None, open_timeout=5, close_timeout=2,
                       max_size=16 * 1024 * 1024) as socket:
        async for raw in socket:
            event = json.loads(raw)
            if event.get("type") == "history_complete":
                await socket.send(json.dumps({"type": "message", "text": text,
                    "sender": client.username, "channel": client.channel,
                    "request_id": request_id}))
            elif event.get("type") == "message_sent" and event.get("request_id") == request_id:
                return event["data"]
    raise ValueError("Connection closed before the server confirmed delivery")


def choose_interactive_mode(*, plain, stdin_tty, stdout_tty, platform, term):
    if plain or not stdin_tty or not stdout_tty:
        return "plain"
    if platform != "win32" and (not term or term.lower() == "dumb"):
        return "plain"
    return "tui"


def build_parser(*, prog=None):
    parser = argparse.ArgumentParser(prog=prog, description="yapp: terminal chat and shell commands for a local yapp server.")
    parser.set_defaults(url=None, channel=None, session=None, name=None, history=30,
                        timeout=15, json=False, command="chat", agent_name=None,
                        history_mode="literal", cwd=None, fresh=False, archived=False,
                        yes=False, agent=None, no_resume=False, provider=None,
                        session_name=None, target_session=None, plain=False,
                        role=None, personality=None,
                        orchestrator_provider=None, provider_flags=None, check=False)

    def options(target):
        target.add_argument("--url", default=argparse.SUPPRESS, help="Local server URL")
        target.add_argument("--channel", default=argparse.SUPPRESS, help="Channel (default: general)")
        target.add_argument("--session", default=argparse.SUPPRESS,
                            help="Session id, name, or unique name prefix")
        target.add_argument("--name", default=argparse.SUPPRESS, help="Human display name")
        target.add_argument("--history", "--limit", type=int, default=argparse.SUPPRESS,
                            help="Recent messages to display (default: 30)")
        target.add_argument("--timeout", type=float, default=argparse.SUPPRESS,
                            help="Shell command timeout in seconds (default: 15)")
        target.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                            help="Machine-readable output for shell commands")

    options(parser)
    parser.add_argument("--plain", action="store_true", default=argparse.SUPPRESS,
                        help="Use legacy interactive rendering")
    parser.add_argument("--no-resume", action="store_true", default=argparse.SUPPRESS,
                        help="Do not offer to resume stopped agents")
    commands = parser.add_subparsers(dest="command")
    command_help = [("chat", "Interactive chat (default)"),
                    ("send", "Send a message; use - to read stdin"),
                    ("read", "Read recent channel messages"),
                    ("channels", "List channels"),
                    ("status", "Show agent status"),
                    ("sessions", "List terminal sessions"),
                    ("new", "Create a terminal session"),
                    ("spawn", "Start a new agent in a session"),
                    ("resume", "Resume an agent in a session"),
                    ("attach", "Attach to an agent terminal"),
                    ("stop", "Stop an agent in a session"),
                    ("unread", "Show unread messages for session agents"),
                    ("retry", "Retry unread delivery for an agent"),
                    ("history", "Change an agent history mode"),
                    ("archive", "Archive a terminal session"),
                    ("update", "Check for and install yapp updates")]
    parser.json_commands = tuple(command for command, _ in command_help
                                 if command not in ("chat", "attach"))
    for command, help_text in command_help:
        subparser = commands.add_parser(command, help=help_text)
        options(subparser)
        if command == "chat":
            subparser.add_argument("--plain", action="store_true",
                                   default=argparse.SUPPRESS,
                                   help="Use legacy interactive rendering")
            subparser.add_argument("--no-resume", action="store_true",
                                   default=argparse.SUPPRESS,
                                   help="Do not offer to resume stopped agents")
        elif command == "send":
            subparser.add_argument("message", nargs="+", help="Message text or - for stdin")
        elif command == "sessions":
            subparser.add_argument("--archived", action="store_true",
                                   default=argparse.SUPPRESS,
                                   help="Include archived sessions")
        elif command == "new":
            subparser.add_argument("session_name", metavar="NAME")
            subparser.add_argument('--orchestrator-provider', metavar='PROVIDER')
            subparser.add_argument('--cwd', help='Orchestrator working directory')
            subparser.add_argument('--provider-flags', help='Quoted orchestrator provider flags')
        elif command == "spawn":
            subparser.add_argument("provider", metavar="PROVIDER")
            subparser.add_argument('--provider-flags', help='Quoted provider flags; saved for later resumes')
            subparser.add_argument("--cwd", required=True)
            subparser.add_argument("--agent-name", default=argparse.SUPPRESS)
            subparser.add_argument("--history-mode", default=argparse.SUPPRESS,
                                   metavar="MODE")
            subparser.add_argument('--role', choices=('generalist', 'implementer', 'code-reviewer',
                                                      'planner', 'tester', 'debugger'))
            subparser.add_argument('--personality', choices=('pragmatic', 'meticulous', 'concise',
                                                             'supportive'))
        elif command == "resume":
            subparser.add_argument("agent", metavar="AGENT")
            subparser.add_argument('--provider-flags', help='Replace saved provider flags; empty text clears them')
            subparser.add_argument("--fresh", action="store_true",
                                   default=argparse.SUPPRESS)
            subparser.add_argument("--agent-name", default=argparse.SUPPRESS)
            subparser.add_argument("--cwd", default=argparse.SUPPRESS)
        elif command in ("stop", "retry", "attach"):
            subparser.add_argument("agent", metavar="AGENT")
        elif command == "unread":
            subparser.add_argument("--agent", default=argparse.SUPPRESS)
        elif command == "history":
            subparser.add_argument("agent", metavar="AGENT")
            subparser.add_argument("--mode", dest="history_mode", required=True,
                                   metavar="MODE")
        elif command == "archive":
            subparser.add_argument("target_session", metavar="SESSION")
            subparser.add_argument("--yes", action="store_true",
                                   default=argparse.SUPPRESS)
        elif command == "update":
            subparser.add_argument("--check", action="store_true", default=argparse.SUPPRESS,
                                   help="Only report whether an update is available")
            subparser.add_argument("--yes", action="store_true", default=argparse.SUPPRESS,
                                   help="Install without asking")
    return parser


def main(argv=None, *, prog=None):
    parser = build_parser(prog=prog)
    args = parser.parse_args(argv)
    args.command = args.command or "chat"
    if args.plain and args.command != "chat":
        parser.error("--plain is only available for chat")
    if args.command == "update":
        from cli_update import run_update
        with redirect_stdout(sys.stderr):
            config = load_config()
        code = run_update(args, config)
        if code:
            parser.exit(code)
        return
    json_unavailable = (f"--json is not available for {args.command}; available for "
                        + ", ".join(parser.json_commands))
    if not 1 <= args.history <= 10000:
        parser.error("--history must be between 1 and 10000")
    if not 0 < args.timeout <= 300:
        parser.error("--timeout must be between 0 and 300 seconds")
    if args.channel is not None and args.session is not None:
        parser.error("--channel and --session cannot be used together")
    if args.command == "archive" and args.session is not None:
        parser.error("archive SESSION cannot be combined with --session")
    if args.command in ("spawn", "resume", "stop", "unread", "retry", "history", "attach") \
            and args.session is None:
        parser.error(f"{args.command} requires --session")
    if args.command == 'attach':
        if args.json:
            parser.error(json_unavailable)
        try:
            require_tmux_platform()
        except CLIError as error:
            parser.exit(1, str(error) + '\n')
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            parser.exit(1, 'Attach requires a terminal\n')
    if args.history_mode == "summary":
        parser.error("summary history mode is not available in this version; use literal or none")
    if args.command in ("spawn", "history") and args.history_mode not in ("literal", "none"):
        parser.error("history mode must be literal or none")
    if args.command == "archive" and not args.yes:
        if not sys.stdin.isatty():
            parser.exit(1, "archive requires --yes when input is not a terminal\n")
        if input("Archive session? [y/N] ").strip().lower() not in ("y", "yes"):
            return
    workspace_commands = {"sessions", "new", "spawn", "resume", "stop",
                          "unread", "retry", "history", "archive"}
    try:
        if args.command in ("chat", "send", "read", "status", "channels"):
            from websockets.asyncio.client import connect  # noqa: F401
        if args.command == "chat":
            import prompt_toolkit  # noqa: F401
    except ImportError:
        parser.exit(1, "Install terminal dependencies: python -m pip install -r requirements-cli.txt\n")
    if args.command == "chat":
        if args.json:
            parser.error(json_unavailable)
        if not sys.stdin.isatty():
            parser.exit(1, "Interactive chat requires a terminal. Use read or send for scripts.\n")
        mode = choose_interactive_mode(plain=args.plain, stdin_tty=True,
                                       stdout_tty=sys.stdout.isatty(),
                                       platform=sys.platform,
                                       term=os.environ.get("TERM"))
        if mode == 'tui':
            try:
                from rich.markdown import Markdown  # noqa: F401
                from markdown_it import MarkdownIt  # noqa: F401
            except ImportError:
                parser.exit(1, "Install terminal dependencies: python -m pip install -r requirements-cli.txt\n")
        if mode == "plain" and not args.plain:
            print("Full-screen unavailable; using plain mode.", file=sys.stderr)
    elif args.command == "send" and args.message == ["-"]:
        args.message = [sys.stdin.read()]
    with redirect_stdout(sys.stderr):
        config = load_config() if args.command == "chat" or not args.url else None
        url = args.url or f"http://127.0.0.1:{config['server']['port']}"
    try:
        output = print if args.command == "chat" else lambda text: None
        channel = (args.channel or "general").removeprefix("#")
        client = ChatClient(url, channel, args.name, args.history, output=output)
    except ValueError as error:
        parser.error(str(error))
    try:
        if args.command == "chat":
            import updates
            # Only a relaunch after an update names a state file; pop it before the
            # server (or anything else) is started so no child or later run reuses it.
            relaunch_state = os.environ.pop(updates.RELAUNCH_ENV, None)
            initial_notices = []

            def startup_output(text):
                text = terminal_text(text)
                initial_notices.append(text)
                client.show(text)

            startup_status = ensure_server(url, explicit_url=args.url is not None, config=config,
                                           output=startup_output)
            restored = None
            if startup_status.get('data_dir'):
                # The restored session is only a preference; the TUI falls back to the picker.
                restored = updates.load_relaunch_state(startup_status['data_dir'], path=relaunch_state)
            if restored and args.channel is not None and restored['channel']:
                client.channel = restored['channel']
            controller = WorkspaceChatController(
                client, WorkspaceAPI(url, timeout=args.timeout), selector=args.session,
                no_resume=args.no_resume, plain_channel=args.channel is not None,
                data_dir=startup_status.get('data_dir'), providers=config.get('agents', {}))
            relaunch = False
            if mode == "plain":
                asyncio.run(interactive(client, controller))
            else:
                try:
                    from cli_tui import interactive_tui
                    from cli_tui_update import AutoUpdater
                    relaunch = asyncio.run(interactive_tui(
                        client, controller, initial_notices=initial_notices, restored=restored,
                        updater_factory=lambda host: AutoUpdater(
                            host, data_dir=controller.data_dir, config=config)))
                except (KeyboardInterrupt, ValueError):
                    raise
                except Exception as error:
                    parser.exit(1, "Full-screen terminal stopped after an unexpected "
                                f"local error ({type(error).__name__}); rerun with --plain.\n")
            if relaunch is True and controller.data_dir:
                os.environ[updates.RELAUNCH_ENV] = str(updates.relaunch_path(controller.data_dir))
                try:
                    os.execv(sys.executable, [sys.executable, *sys.argv])
                except OSError as error:
                    os.environ.pop(updates.RELAUNCH_ENV, None)
                    parser.exit(1, f"yapp was updated but could not reopen ({error.strerror}); "
                                "run the same command again.\n")
        elif args.command == 'attach':
            api = WorkspaceAPI(url, timeout=args.timeout)

            async def resolve_terminal():
                async with asyncio.timeout(args.timeout):
                    workspace = await asyncio.to_thread(api.resolve, args.session, True)
                    return resolve_agent(workspace, args.agent), workspace['id']

            agent, ws_id = asyncio.run(resolve_terminal())
            code = attach_agent(agent, output=print, shell_session=ws_id)
            if code:
                parser.exit(code)
        elif args.command in workspace_commands:
            api = WorkspaceAPI(url, timeout=args.timeout)

            async def run_session_command():
                async with asyncio.timeout(args.timeout):
                    return await asyncio.to_thread(run_workspace_command, api, args)

            result = asyncio.run(run_session_command())
            if args.json:
                print(json.dumps(result.data, ensure_ascii=True))
            else:
                print(terminal_text(format_workspace_result(args.command, result)))
        else:
            async def run_command():
                async with asyncio.timeout(args.timeout):
                    if args.session:
                        api = WorkspaceAPI(url, timeout=args.timeout)
                        workspace = await asyncio.to_thread(
                            api.resolve, args.session, True)
                        args.channel = workspace["channel"]
                        client.channel = workspace["channel"]
                    else:
                        args.channel = channel
                    return await shell_command(client, args)
            result = asyncio.run(run_command())
            if args.json:
                print(json.dumps(result, ensure_ascii=True))
            elif args.command == "channels":
                print(terminal_text("\n".join("#" + ch for ch in result)))
            elif args.command == "read":
                client.output = print
                for message in result:
                    client.show_message(message)
            elif args.command == "status":
                client.output = print
                client.status = result
                asyncio.run(client.submit("/agents"))
            else:
                print(f"Sent to #{terminal_text(client.channel)}" +
                      (f" (message {result['id']})" if "id" in result else ""))
    except KeyboardInterrupt:
        parser.exit(130)
    except ValueError as error:
        message = terminal_text(str(error))
        if args.command != "chat" and ("Could not connect" in message or
                                        "timed out" in message.lower()):
            message += "\nStart it manually: python run.py"
        parser.exit(1, message + "\n")
    except TimeoutError:
        parser.exit(1, "Server request timed out.\nStart it manually: python run.py\n")
    except Exception:
        # Transport exception messages may embed the session token in a URL.
        parser.exit(1, "Server request failed or timed out. Check run.py and --url. "
                    "For send, delivery may be uncertain; check history before retrying.\n")


if __name__ == "__main__":
    main()
