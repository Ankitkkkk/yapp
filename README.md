# yapp: multi-agent chat and terminal UI for AI coding agents

> Run Claude Code, Codex, Gemini CLI, and other AI coding agents side by side,
> let them talk to each other over MCP, and manage them all from one full-screen
> terminal app. Open source (MIT), local-first, installs with one command.

![Linux](https://img.shields.io/badge/platform-Linux-orange) ![macOS](https://img.shields.io/badge/platform-macOS-lightgrey) ![WSL2](https://img.shields.io/badge/Windows-WSL2-blue) ![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-green)

**yapp is a full-screen terminal workspace (TUI) for your AI coding agents.** Chat
with multiple agents in one shared room, @mention them to hand off work, organize
them into sessions, see which agent is waiting for approval, and attach to their
terminals without losing your message draft. Agents coordinate through a local
[MCP](https://modelcontextprotocol.io/) chat server, so Claude Code can ask Codex
for a review, and Gemini can pick up where they left off, with you in the loop.

Use cases:

- **Multi-agent coding:** a planner, an implementer, and a reviewer working in the same repo.
- **Agent-to-agent code review:** have one model review another model's changes.
- **One place for many CLIs:** a single terminal UI instead of juggling tmux panes and tabs.
- **Orchestration:** a resident orchestrator routes your requests to the right worker agent.
- **Human in the loop:** see pending approval prompts and jump straight to that agent's terminal.

yapp builds on [Agentchattr](https://github.com/bcurts/agentchattr), retaining
its local server, MCP communication, and optional browser interface, and adds
the terminal workflow described below.

[Website](https://yapp.riggedcode.com/) · [Setup guide](https://yapp.riggedcode.com/docs/) · [Install yapp](INSTALLATION.md) · [For AI agents](#for-ai-agents) · [FAQ](#faq) · [TUI guide](#tui-guide) · [Keyboard controls](#keyboard-controls) · [Browser and server features](#browser-and-server-features)

## What the TUI provides

| Feature | What you can do |
| --- | --- |
| Searchable sessions | Create, switch, rename, and archive sessions with their own chat and agents. |
| Chat and drafts | Read styled messages, scroll history, mention agents, and keep drafts while navigating or attaching. |
| Agent status | See colored status indicators, unread counts, saved roles, and orchestrators. |
| Pending input | See waiting agents from the selected session and open their terminals to resolve prompts. |
| Direct attachment | Press F6 to choose an agent, or use Attach / Review input for the selected agent. |
| Agent lifecycle | Add named agents, stop or resume them, and remove entries with their tmux sessions. |
| Saved profiles | Choose a role and personality once; startup instructions and badges remain locked across resumes. |
| Session orchestration | Give each new session a resident orchestrator which routes unmentioned human requests to suitable workers. |
| Loop guard controls | Set the hop limit through F4 → Loop guard and use `/continue` to resume a paused conversation. |
| Text copying | Press F7 to release mouse capture, select text, and copy with your terminal. |
| Shell commands | Script session management and chat with JSON output, alongside the interactive TUI. |

Terminal providers include **Claude Code**, **Codex**, **Gemini CLI**,
**Antigravity**, **GitHub Copilot CLI**, **Kimi**, **Qwen**, **Kilo CLI**, and
**CodeBuddy**. Native conversation resume has built-in adapters for Claude and
Codex; other providers need a compatible adapter or a fresh launch. **MiniMax**
and local OpenAI-compatible models use the separate [API wrapper](#api-agents-local-models).
Install and authenticate providers separately. Waiting indicators are advisory;
approvals are resolved in the provider's own terminal.

## Quick start

Use Linux, macOS, or WSL2 with curl and tmux. Install with one command:

```sh
curl -fsSL https://yapp.riggedcode.com/install.sh | sh
```

Then run `yapp`. The installer keeps yapp in its own environment under
`~/.local/share/yapp`, so it works on Ubuntu/Debian without touching system
Python. It reuses Python 3.11+ when available; with only Python 3.10 (or no
Python), it downloads a private Python 3.13 through uv under the yapp install
directory. Your system Python and shell profiles are unchanged. Run it again
to update.

### Staying up to date

yapp updates itself. While the TUI is open it checks for a new
[release](https://github.com/Ankitkkkk/yapp/releases) every 6 hours; when one
appears it installs it, restarts the server, and reopens, keeping your agents
running and your unsent drafts. Update by hand with `yapp update` (or
F4 → Update yapp); `yapp update --check` only reports. Turn automatic installs
off with `[updates] auto = false` in `config.local.toml` (or
`YAPP_NO_AUTO_UPDATE=1`), or stop checking with `[updates] check = false`
(`YAPP_NO_UPDATE_CHECK=1`). Source checkouts are never updated automatically;
use `git pull`.

Prefer [pipx](https://pipx.pypa.io/)? Install pipx first (`sudo apt install pipx`
or `brew install pipx`, then `pipx ensurepath`), then run
`pipx install git+https://github.com/Ankitkkkk/yapp.git`. If your shell offers to
correct `pipx` to `pip`, answer **n**: pipx is not installed yet, and plain `pip`
fails with `externally-managed-environment`.

The package installs two equivalent commands, `yapp` and `goon`; use either.

Interactive startup can start the local server automatically when tmux is
available. Creating a session in the TUI asks for a name, then an orchestrator
provider and absolute project directory; submitting starts that provider. Add
workers with **F3 → Add agent**, entering their provider, name, and directory.
Use **F1** for Help.

See [INSTALLATION.md](INSTALLATION.md) for system dependencies, installing from a
source checkout, manual server startup, Windows limitations, and instructions an
AI agent can follow to install and verify the application. Release ZIPs from
`build_release.py` do not include the TUI.

## For AI agents

If you are an AI coding agent asked to install or use yapp, follow these steps.
A condensed summary is in [`llms.txt`](llms.txt); the full checklist is
[INSTALLATION.md](INSTALLATION.md).

1. Check `curl --version` and `tmux -V`. The recommended installer selects or
   downloads a compatible Python; pipx and source installs still require Python 3.11+.
2. Install: `curl -fsSL https://yapp.riggedcode.com/install.sh | sh` (or `pipx install git+https://github.com/Ankitkkkk/yapp.git` if pipx is present). Never use `pip install --break-system-packages`.
3. Verify without starting anything: `yapp --help`.
4. With a server running, read state as JSON (no side effects):

```sh
yapp status --json
yapp sessions --json
```

5. With user authorization, create a session, start an agent, and send work
   (these commands change state; `--json` only selects the output format):

```sh
yapp new billing --json
yapp spawn claude --session billing --cwd /absolute/project --agent-name reviewer
yapp send --session billing --name Pat "@reviewer please review the latest changes"
yapp read --session billing --limit 20 --json
```

A plain `yapp new` creates no orchestrator. Add
`--orchestrator-provider claude --cwd /absolute/project` to start one during
creation. Interactive `chat` and `attach` do not support `--json`.

Once registered, agents talk through the MCP tools `chat_send`, `chat_read`, and
`chat_join` on the `yapp` MCP server (`http://127.0.0.1:8200/mcp`). Always use
absolute paths for `--cwd`. Do not stop, archive, or remove sessions and agents
you did not create.

## TUI guide

**yapp** includes a full-screen terminal UI and shell client.
See [INSTALLATION.md](INSTALLATION.md) for installation steps and a checklist an
AI agent can follow on a new system. Every example below works with `goon` in
place of `yapp`.

yapp works from a terminal without opening a browser:

```sh
yapp
yapp --plain
yapp chat --plain
```

`yapp chat` explicitly starts interactive mode. In a source checkout,
`python yapp.py` and `python cli.py` accept the same arguments, and
`python run.py` starts only the server.

`yapp` opens the full-screen terminal UI and
its session picker. Create a session or select an existing one; the picker can
also show archived sessions and asks before restoring one. Terminal sessions
group a channel, working directories, and persistent agent identities.

Use `yapp --plain` or `yapp chat --plain` for the legacy
scrolling prompt. When stdout is not a terminal, or on macOS/Linux when `TERM`
is unset, empty, or `dumb`, interactive chat automatically uses that renderer
and prints `Full-screen unavailable; using plain mode.` once to stderr. Input
must still be a terminal. Capability fallback is decided before startup; a
failure after full-screen startup exits with one line naming only the exception
type and recommends rerunning with `--plain`.

```sh
yapp
yapp --channel general
yapp --session billing --no-resume
yapp new billing --orchestrator-provider codex --cwd /absolute/project --json
yapp spawn claude --session billing --cwd /absolute/project --agent-name reviewer --history-mode literal --role code-reviewer --personality meticulous
yapp resume reviewer --session billing --fresh
yapp attach reviewer --session billing
yapp unread --session billing
yapp archive billing --yes
```

`--session` accepts an exact session ID, exact name, or unique name prefix.
Duplicate names and ambiguous prefixes require a more specific selector, such
as the full ID. Agent selectors accept a registry name, stable agent ID, or a
provider unique within that session. `--agent-name` names an agent; `--name`
remains the human sender name. `--history N` (also `--limit N`) remains a numeric
message limit; `--history-mode` selects agent catch-up policy.

Use `--channel` for channel-only chat in either renderer; it cannot be combined
with `--session`.
Shell `send` and `read` default to `general` without either flag and accept
`--session billing` to target that session's channel. `sessions` lists active
sessions; `sessions --archived` includes archived ones. Archiving from a script
requires `--yes` and checkpoints and stops that session's agents on the server.

Use `@all` (or `@both`) to address every running agent in the current session.
`@everyone` is not an alias. Explicitly mention a stopped agent by name to
leave it a message for later.

Interactive chat supports live messages, `@mentions`, channel switching, recent
history, agent status, and reconnection after server restarts. Incoming replies
do not overwrite the input prompt. Messages entered while disconnected are
rejected rather than queued for later delivery.

On wide terminals, sessions, agent status, and pending input share a left rail.
The conversation and message input use the main column. Agent details expand
beside the rail for readable paths and recovery commands. Narrow terminals stack
the controls below chat. Accent borders mark the focused pane, and the footer
shows NORMAL, INSERT, or COPY mode.

The TUI uses [Catppuccin Mocha](https://github.com/catppuccin/palette): explicit
dark surfaces, lavender focus accents, colored agent states, and matching dialogs
and completion menus. A header keeps the session and connection visible; padded
messages wrap at a readable width on large terminals. Kitty and terminals that
advertise `COLORTERM=truecolor` or `24bit` use RGB colors. Other terminals use
their detected color depth; `PROMPT_TOOLKIT_COLOR_DEPTH` and `NO_COLOR` overrides
are respected. Restart the TUI after updating to load the theme.

Conversation messages, Activity, Help, and agent details render Markdown:
headings, emphasis, lists, quotes, links, tables, and syntax-highlighted fenced
code. Narrow tables stack their values to keep them readable. F7 copy mode
shows the original Markdown source, including backticks and code indentation;
press F7 again to restore formatting. Drafts and saved messages keep their
original text. Source checkouts need the updated `requirements-cli.txt` installed.

### Keyboard controls

| Key | Action |
| --- | --- |
| F2 | Open Sessions, or Channels in channel-only chat |
| F3 | Open the agent list; from agent actions, return to that list |
| F4 | Open the command palette |
| F5 | Open Activity diagnostics |
| F6 | Choose an agent to attach; always opens the agent list |
| F7 | Toggle terminal text selection: drag to select, use your terminal's Copy shortcut, then F7 to restore app mouse controls |
| F1 | Open Help |
| Tab / Shift+Tab | Move between visible controls outside forms; in forms, cycle choices or directory suggestions |
| Up / Down in forms | Move between editable fields and action buttons |
| i / I | Enter INSERT mode at the cursor / first nonblank character |
| a / A | Enter INSERT mode after the cursor / at the line end |
| Enter | In INSERT: accept the highlighted completion or add a newline. In NORMAL: send the message. In dialogs: select the highlighted choice |
| Escape, then Enter | Return to NORMAL and send the message |
| h / j / k / l, w / b, 0 / $ | In NORMAL: move left/down/up/right, by word, or to line start/end |
| PageUp / PageDown | Scroll a page in the focused conversation or Activity pane |
| End | Follow the latest messages in the focused conversation and clear its new-message count; jump to the bottom in Activity |
| Mouse wheel | Scroll three wrapped lines in conversation or Activity; conversation wheel-up leaves follow, and reaching the bottom resumes follow and clears its new-message count |
| Escape | Return the message composer to NORMAL; cancel the current dialog, close Help/Activity, or return from Agents to the message box |
| Ctrl+C | Cancel the current dialog while preserving the message draft |
| Ctrl+D | In INSERT: delete at the cursor. In either mode: Quit when the composer is empty |
| Ctrl+Q | Confirm unsent drafts when needed, checkpoint, and quit |

To release agent CPU and memory, open **F4 Commands → Stop all agents**.
The confirmation shows the number of saved agents across every session,
including stopped entries that may still have leftover processes.
Stopping interrupts active work and closes their terminals and wrappers while
keeping saved sessions and history for **Resume agent**. Individual failures are
reported, and other agents still receive their stop request. Quitting the TUI
alone leaves agents running. This covers agents managed by saved sessions;
unrelated standalone terminals are outside its scope.

Use the visible **Restart server** button, or **F4 Commands → Restart server**,
to restart the connected local `run.py` instance on Linux or macOS. The
confirmation defaults to No and names the connected URL. Chat and MCP disconnect
briefly, then the TUI reconnects with fresh credentials. Agent terminals, the
selected session, composer mode, cursor, and unsent drafts remain intact.
On the first upgrade, restart `run.py` and the TUI manually to load this feature.
Browser tabs need a page reload after a server restart. The restart request is sent once. If readiness
times out, do not click repeatedly: check Activity and the server logs because
the old server may still be draining. Start `run.py` manually only after
confirming that the old server stopped.

Session names have inset rows, a blank separator, and a full-row highlight.
A dot marks the open session. Click a row to open it; blank sidebar space does
not change sessions. Up/Down and mouse-wheel scrolling remain available.

The message box starts in **NORMAL** mode. Press **i** to write in **INSERT**
mode; Enter adds a newline instead of sending. Press **Escape**, then **Enter**
to send deliberately. The message-box title shows the current mode and shortcuts.
Pasting enters INSERT mode automatically. This supports the Normal/Insert modes
and movement keys listed above, rather than the full Vim command set.
After clicking another main pane, press **i** or **I** to focus the message box
and enter INSERT mode again. **a** and **A** also return to editing with their
usual cursor movement. Clicking the message box restores focus while retaining
its current mode. These shortcuts do not interrupt dialogs, Help, or F7 copying.
Mentions such as **@agent-1** appear bright cyan and bold while you write;
their stored and sent text stays unchanged. Mention styling marks handle-shaped
text and does not verify that an agent is online.

To copy a string from a response, press **F7**, drag over the text, then use
your terminal's Copy shortcut (**Ctrl+Shift+C** in GNOME Terminal). Selection
mode hides the sidebar and conversation borders so multiline selections contain
message text without UI separators. Message editing and sending pause during selection.
Press **F7** again to restore the layout, clicking, and mouse-wheel scrolling,
and return focus to the message input. Your NORMAL/INSERT mode and draft are
preserved, so editing resumes where you left it. On terminals that support it,
holding **Shift** while dragging also bypasses app mouse capture. Inside tmux,
its mouse mode can still intercept dragging: use Shift-drag or tmux copy mode.
Copying uses your terminal's clipboard; physical selection depends on the terminal.

In the wide layout, **Input pending** sits below Sessions and lists waiting
agents from the open session only. It updates when you switch sessions or
resolve prompts. Click a name, or Tab into the list and use Up/Down then Enter,
to open that agent's terminal. Escape returns to Message. The section hides
with the sidebar in compact layouts; **Review input** and **F3 → Attach** remain
available there.

Chat messages have colored sender headers, muted timestamps, and a slim gutter.
Your messages use cyan, agents use green, and system notices stay subdued.
Spacing separates messages; attachments and choices have distinct accents.
Scrolling, unread markers, and drafts keep their existing behavior.

The command palette exposes Help, Activity, draft clearing, session or channel
navigation, and actions applicable to the selected session and agent. Those
include create, rename, archive, add, attach, resume, stop, remove, unread, retry,
history settings, inspection, refresh, and Quit. Narrow terminals use a compact
layout. Below 80 columns or 18 rows, the UI keeps the current model, draft, and
focus while showing resize, Help, and Quit controls.

To change the loop limit from the CLI, press **F4**, choose **Loop guard**, enter
a whole number from **1 to 50**, and select **Save**. The field starts with the
current server value. Changes are saved and apply immediately to all sessions;
each channel counts its own agent-to-agent hops. Use `/continue` afterward if
a conversation is already paused. Escape cancels without saving.

Choose **New session**, enter its name, then choose its orchestrator provider
and working directory. Existing sessions expose **Enable orchestrator** under
F4 Commands; stopped orchestrators show **Resume orchestrator**. Orchestrators
remain distinct from workers and route new unmentioned human requests.

Explicit mentions, including `@all` and unknown handles, bypass orchestration.
Agent replies, jobs, and structured workflow turns do not create routing requests.
The orchestrator chooses from ready workers in its own session using their names
and saved profiles. A system message shows which workers received the assignment.

An enabled, non-archived session keeps its orchestrator resident even when a
client switches sessions. Stop, Stop all, and Archive pause its automatic recovery;
new requests remain pending until it is resumed. Existing sessions and API calls
that omit orchestrator configuration do not launch one automatically.

If dispatch is interrupted, the notice reports uncertainty. The original request
remains in worker unread history, including assignments older than its read
cursor. Review the worker's state before using Retry; queueing confirms delivery
to the wrapper queue, not task completion.

Click **Add agent**
beneath Actions to open the form directly, even with an empty agent list.
Select a provider, enter an existing absolute working directory, and review
the history mode. Next, choose a saved role and personality. Roles include
generalist, implementer, code-reviewer, planner, tester, and debugger;
personalities include pragmatic, meticulous, concise, and supportive. These
choices supply startup instructions and stay locked across Stop and Resume.
Each launch receives its assigned session
name and channel explicitly; `none` history mode still delivers this identity
prompt without requesting old chat. Subsequent triggers reinforce the current
name, including after a rename. Click the directory or name input to edit it,
or use Up/Down to move between fields and action buttons. Choices show one
selected value and its position in the list. Tab/Shift+Tab or the clickable
chevrons cycle provider, history mode, role, and personality. Directory fields
suggest matching folders as you type; Tab/Shift+Tab cycles suggestions without
leaving the field. Enter accepts a highlighted suggestion; press Enter again
to submit, or use Up/Down to keep that path and continue editing. The same
controls work in New session and other input forms. **F3 → Add agent** also
remains available. Click an agent row and
then **Attach** to open its terminal directly. **F6** always opens the agent
chooser, including after a previous selection or attach. Waiting agents show **Review input**
in the same button position. **F3 → Attach** and the command palette also work.
Detaching returns to the current draft.

**Provider flags** in Add/Resume passes optional arguments to the agent CLI,
for example `--model MODEL_NAME`. Quote values containing spaces. Flags are saved
per agent and prefilled on Resume; edit them to replace the saved flags or clear
the field to remove them. Existing running agents pick up changes on their next
launch. The shell and slash commands support the same option:

```sh
yapp spawn codex --session billing --cwd /absolute/project --provider-flags='--model MODEL_NAME'
yapp resume reviewer --session billing --provider-flags='--model MODEL_NAME'
yapp resume reviewer --session billing --provider-flags=''
```

In chat, use `/spawn codex --provider-flags='--model MODEL_NAME'` or
`/resume reviewer --provider-flags='--model MODEL_NAME'`. Omitting the option on
Resume keeps the saved flags. Arguments pass directly to the selected provider;
shell substitutions and pipelines are not evaluated.

Resume shows the saved working directory as read-only and reuses it automatically;
you do not need to enter it again. Add agent still lets you choose a directory.
If submission fails, the form keeps your entries and shows the error at the top.
Use **Attach** (**F6**) to open an agent whose terminal is already running.
After a temporary heartbeat disconnect, the same running wrapper restores its
status when it reconnects; a deliberately stopped agent still needs Resume.

F3 always opens the agent chooser, including empty sessions. Its **Add agent**
button opens the form directly and stays visible when search finds no matches.
Use **Down** past the last result to reach the buttons, **Left/Right** to choose
a button, **Enter** to activate it, and **Up** to return to the list. Inside
agent actions, press **F3**, **Escape**, or **Back** to return to the chooser.
Escape from the chooser returns to Message. Enter on an agent row or the
Actions button opens actions for the highlighted agent directly.

To remove an agent from the list, select it and choose **F3 → Remove agent**
(or use the command palette). Confirm **Yes** to stop its wrapper and exact tmux
session, discard its pending deliveries, and delete its saved entry. This works
for running and terminated agents. Session chat, provider conversation files, and
logs are kept. Successful removal returns focus to Message and keeps your draft.
If cleanup fails, the entry stays available for retry. Restart the server and
CLI to load this action. On macOS, a wrapper left over from a previous server
process must be stopped in its original terminal before removal can proceed;
Linux can verify and stop that orphan safely.

Agent rows show the name, a colored status badge, an unread count, and the directory path:
`● Ready` (green), `◆ Working` (cyan), `◌ Starting` (amber), `○ Stopped` (gray),
and `× Error` (red). `! Input` is amber and takes priority while a prompt waits.
The selected name has a cyan marker without hiding the status color. Open
**Inspect agent** for the full path, history, session status, and recovery details.
Long paths shorten to fit the row; status and Review input stay visible.

On macOS/Linux, common terminal approval and confirmation prompts show
**! Input · Attach** beside the agent and an amber **Review input** button for
the selected waiting agent. Click that button (or Tab to it and press Enter) to
open its tmux terminal and answer the prompt. **F3 → Attach** also works.
Detach with **Ctrl+B, then D** to return to chat and your draft. This is best-effort detection of visible
confirmation footers and yes/no prompts; custom dialogs may not be recognized.
The hint clears when the prompt disappears. If terminal reports stop, the signal
expires after 20 seconds and clears on the next status refresh. It does not
approve anything or change the agent's running state.
Existing server, CLI and wrapper processes need restarting to load this feature;
Windows wrappers do not currently report this hint.

Codex also recognizes MCP permission forms such as **Allow / Allow for this
session / Always allow / Cancel** with an `enter to submit | esc to cancel`
footer. For earlier notification through Codex lifecycle hooks, install the
project hooks using the Python environment you intend to keep using:

```sh
python waiting_hooks.py install --provider codex --project /absolute/project/path
```

This adds notification commands to the project's `.codex/hooks.json`, preserving
existing entries. In Codex, use **`/hooks`** to inspect and trust the added
commands. New or changed hook definitions require native trust; the installer
does not bypass it. Start/resume the agent through an updated yapp
wrapper to enable its private event stream. Terminal detection works even when
hooks are not installed or trusted. Existing running wrappers need relaunching.

`PermissionRequest` signals waiting, including for MCP tools. `PostToolUse`,
`Stop`, `Interrupt`, and `SessionEnd` clear matching tool, turn, or session hints.
A visible prompt disappearing also clears the hint before a long tool finishes.
Hook-only hints without matching terminal evidence expire after 60 seconds;
this is advisory status, not an exact approval state machine. Each wrapper launch
uses a separate private directory, so agent signals cannot mix across launches.
Commands record only event/session/turn/tool identifiers, return no decision,
and remain silent outside managed wrappers. Tool inputs and arguments are not
stored. See the [Codex hook reference](https://learn.chatgpt.com/docs/hooks).

Provider adapters can override `waiting_for_input(output)` for terminal forms,
`prompt_event(payload)` to return a normalized `PromptEvent`, and
`prompt_hook_config(command)` to describe native hooks. Codex is the first
supported hook installer; other adapters retain generic terminal detection.

| Interactive command | Action |
| --- | --- |
| `/channels` | List channels |
| `/join NAME` | Switch channels in plain channel mode |
| `/create NAME` | Create and switch channels in plain channel mode |
| `/agents` | Show agent availability and roles |
| `/history` | Jump to the conversation and follow latest messages; `--plain` shows recent messages |
| `/spawn PROVIDER` | Prompt for working directory and history policy, then start an agent |
| `/resume AGENT [--fresh] [--cwd PATH]` | Resume an agent, optionally starting a fresh conversation |
| `/attach AGENT`, `/stop AGENT` | Open or stop an agent terminal |
| `/unread [AGENT]`, `/retry AGENT` | Inspect unread messages or retry their delivery |
| `/history AGENT MODE` | Set agent history policy to `literal` or `none` |
| `/rename "New name"` | Rename the selected session |
| `/sessions` | Open session navigation; `--plain` checkpoints before opening its picker |
| `/archive` | Confirm archival, stop session agents, and return to the picker |
| `/jobs`, `/rules` | List jobs or rules |
| `/help` | Show commands |
| `/quit` | Checkpoint the selected session and exit; server and agents keep running |

Completion suggestions cover commands, agent handles, and channel names. In
INSERT mode, Tab chooses a suggestion and Enter applies it; Enter without a
selected suggestion adds a newline. Escape returns to NORMAL, where Enter sends.
Escape cancels dialogs without altering the composer. Alt+Enter can produce the
same terminal bytes as Escape followed by Enter, so use plain Enter in INSERT
mode for newlines. Draft text and cursor position survive
session/channel switches and resize. Up to 50 nonempty drafts are kept in
memory, each at most 64 KiB of UTF-8 text. At capacity, send or clear one before
opening another destination. Drafts are not persisted and disappear when the
CLI process exits.

In `--plain` mode, Tab completes words, Ctrl+C returns to a fresh prompt, and
Ctrl+D exits. Other slash commands are forwarded to the server. In full-screen
mode, `/sessions` opens navigation without checkpointing; committing a switch
checkpoints the previous session, and Quit checkpoints the selected session. In
plain mode, `/sessions`, `/quit`, and Ctrl+D keep the legacy checkpoint behavior.
Leaving chat does not stop its agents. Selecting a session can offer to resume
stopped agents; `--no-resume` suppresses that question. If a project moved, use
`/resume AGENT --cwd /absolute/new/project`. An unknown native conversation ID
is shown as `id unknown`; if ordinary resume is unavailable, use `--fresh` to
start a new provider conversation while keeping the session agent identity.

History prompts offer `none` and `literal`, defaulting to `literal`. Explicit
`summary` is unavailable in this version; summary generation and failed-summary
recovery prompts are deferred. Literal catch-up may show `catching up…`.
New Claude projects without `.claude` state may require accepting a trust
prompt through `/attach AGENT`. Detach from tmux with Ctrl+B, then D; the agent
keeps running. Attaching inside tmux switches clients; switch back with
`tmux switch-client -l`.

For scripts and one-shot commands:

```sh
yapp send --channel general --name Pat "@claude review the latest changes"
yapp read --channel general --limit 20
yapp status
yapp channels
yapp read --json
yapp send --json - < task.txt
```

`send` waits for a server acknowledgment after persistence (or command handling).
Shell commands return exit code 0 on success, 1 for request failures, and 2 for
invalid command-line arguments. `--json` emits JSON to stdout; errors go to
stderr. Delivery can be uncertain after a transport failure, so inspect history
before retrying a send. Messages are not retried automatically.

Use `--url http://127.0.0.1:18300` to select another local instance. Otherwise,
the port comes from the shared configuration, including `YAPP_PORT`.
`--timeout 15` bounds shell requests. Options work before or after the subcommand.
For `attach`, it bounds API resolution; foreground tmux attachment lasts until
you detach or the agent terminal exits.
The terminal client connects to localhost only.

Interactive chat without an explicit `--url` can start a missing local server
in the `yapp-server` tmux session. Shell commands and explicit `--url`
never auto-start it. Server logs are at `<resolved data_dir>/logs/server.log`;
startup failures include that path and a manual `python run.py` hint. Connecting
to an existing server with a different data directory prints a warning.
The client bootstraps the local browser session token and uses authenticated
HTTP/WebSocket requests; no token needs to be copied into shell commands.

Spawn, resume, attach, and server auto-start require tmux on Linux/macOS.
Windows supports other HTTP/chat commands against an existing server; use
`wrapper_windows.py` for manual agent launch. Full-screen dispatch is covered by
portable tests, but Windows full-screen terminal behavior has not been tested
end to end. Real provider authentication, trust dialogs, and terminal behavior
depend on the installed provider CLI. Decision buttons, attachment uploads, and
other graphical workflows still use the web UI.

Release ZIPs from `build_release.py` do not ship the CLI files or the
terminal-session core modules; install the package instead.

## Browser and wrapper quickstart (Windows)

These launchers open the inherited browser/server workflow. For yapp's
terminal-agent lifecycle on Windows, use WSL2 and the [installation guide](INSTALLATION.md).

**1. Open the `windows` folder and double-click a launcher** to start your agent — e.g. `start_claude.bat`, `start_codex.bat`, `start_gemini.bat`, etc.

On first launch, the script auto-creates a virtual environment, installs Python dependencies, and configures MCP. Each agent launcher auto-starts the server if one isn't already running, so you can launch in any order. Run multiple launchers for multiple agents — they share the same server.

<details>
<summary>All agent launchers (click to expand)</summary>

- `start.bat` — starts the chat server only
- `start_claude.bat` — starts Claude
- `start_codex.bat` — starts Codex
- `start_gemini.bat` — starts Gemini
- `start_copilot.bat` — starts GitHub Copilot CLI (requires `npm install -g @github/copilot`)
- `start_kimi.bat` — starts Kimi
- `start_qwen.bat` — starts Qwen
- `start_kilo.bat` — starts Kilo
- `start_kilo.bat provider/model` — starts Kilo with a specific model (e.g. `start_kilo.bat anthropic/claude-sonnet-4-20250514`)
- `start_codebuddy.bat` — starts CodeBuddy (first launch prompts interactive login)
- `start_minimax.bat` — starts MiniMax (requires `MINIMAX_API_KEY` env var)

**Auto-approve variants** (agents run tools without asking permission):

- `start_claude_skip-permissions.bat` — Claude with `--dangerously-skip-permissions`
- `start_codex_bypass.bat` — Codex with `--dangerously-bypass-approvals-and-sandbox`
- `start_gemini_yolo.bat` — Gemini with `--yolo`
- `start_qwen_yolo.bat` — Qwen with `--yolo`

</details>

**2. Open the chat:** Go to **http://localhost:8300** in your browser, or double-click `open_chat.html`.

**3. Talk to your agents:** Type `@claude`, `@codex`, `@gemini`, `@copilot`, `@kimi`, `@qwen`, `@kilo`, `@codebuddy`, or `@minimax` in your message, or use the toggle buttons above the input. The agent will wake up, read the chat, and respond.

> **Tip:** To manually prompt an agent to check chat, type `mcp read #general` in their terminal.

## Browser and wrapper quickstart (Mac / Linux)

**1. Make sure tmux is installed:**

```bash
brew install tmux    # macOS
# apt install tmux   # Ubuntu/Debian
```

**2. Launch an agent:**

Open a terminal in the `macos-linux` folder (right-click → "Open Terminal Here", or `cd` into it) and run a launcher — e.g. `sh start_claude.sh`, `sh start_codex.sh`, `sh start_gemini.sh`, etc.

On first launch, the script auto-creates a virtual environment, installs Python dependencies, and configures MCP. Each agent launcher auto-starts the server in a separate terminal window if one isn't already running. The agent opens inside a **tmux** session. Detach with `Ctrl+B, D` — the agent keeps running in the background. Reattach with `tmux attach -t yapp-claude`.

<details>
<summary>All agent launchers (click to expand)</summary>

- `sh start.sh` — starts the chat server only
- `sh start_claude.sh` — starts Claude
- `sh start_codex.sh` — starts Codex
- `sh start_gemini.sh` — starts Gemini
- `sh start_copilot.sh` — starts GitHub Copilot CLI (requires `npm install -g @github/copilot`)
- `sh start_kimi.sh` — starts Kimi
- `sh start_qwen.sh` — starts Qwen
- `sh start_kilo.sh` — starts Kilo
- `sh start_kilo.sh provider/model` — starts Kilo with a specific model (e.g. `sh start_kilo.sh anthropic/claude-sonnet-4-20250514`)
- `sh start_codebuddy.sh` — starts CodeBuddy (first launch prompts interactive login)
- `sh start_minimax.sh` — starts MiniMax (requires `MINIMAX_API_KEY` env var)

**Auto-approve variants** (agents run tools without asking permission):

- `start_claude_skip-permissions.sh` — Claude with `--dangerously-skip-permissions`
- `start_codex_bypass.sh` — Codex with `--dangerously-bypass-approvals-and-sandbox`
- `start_gemini_yolo.sh` — Gemini with `--yolo`
- `start_qwen_yolo.sh` — Qwen with `--yolo`

</details>

**3. Open the chat:** Go to **http://localhost:8300** or open `open_chat.html`.

**4. Talk to your agents:** Type `@claude`, `@codex`, `@gemini`, `@copilot`, `@kimi`, `@qwen`, `@kilo`, `@codebuddy`, or `@minimax` in your message, or use the toggle buttons above the input. The agent will wake up, read the chat, and respond.

---

## How it works

```
You type "@claude what's the status on the renderer?"
  → server detects the @mention
  → wrapper injects "use mcp to read #general - you're mentioned..." into Claude's terminal
  → Claude reads recent messages, sees your question, responds in the channel
  → If Claude @mentions @codex, the same happens in Codex's terminal
  → Agents go back and forth until the loop guard pauses for your review

No copy-pasting between terminals. No manual prompting.
Agents wake each other up, coordinate, and report back.
```

<p align="center">
  <img src="gang.gif" alt="yapp gang" width="600"><br>
  <sub>the gang after <code>/hatmaking</code></sub>
</p>

## Browser and server features

The following capabilities come from the underlying Yapp server and
browser interface. Graphical features such as image uploads and decision cards
remain available in the browser; they are separate from the TUI controls above.

### Agent-to-agent communication
Agents @mention each other and the server auto-triggers the target. Claude can wake Codex, Codex can respond back, Gemini can jump in — all autonomously. A per-channel loop guard pauses after N hops to prevent runaway conversations — a busy channel won't block other channels. Human @mentions always pass through, even when the loop guard is active. Type `/continue` to resume.

`/continue` resets the guard and wakes the blocked recipients in that channel,
once per agent. It respects current session membership and stopped agents.
Only humans can resume; repeated `/continue` does not repeat successful wakes.
If a wake cannot be queued, the chat reports it and `/continue` retries it.
Guard state and pending wakes reset on server restart; use a fresh `@mention`
to restart a conversation paused before the restart.

### Channels
Conversations are organized into channels (like Slack). The default channel is `#general`. Create new channels by clicking the `+` button in the channel bar, rename or delete them by clicking the active tab to reveal edit controls. Channels persist across server restarts.

Agents interact with channels via MCP: `chat_send(channel="debug")`, `chat_read(channel="debug")`. Omitting the channel parameter in `chat_read` returns messages from all channels. The `chat_channels` tool lets agents discover available channels.

When agents are triggered by an @mention, the wrapper injects `use mcp to read #channel-name - you're mentioned, take appropriate action and respond` so the agent reads the right channel automatically. Join/leave messages are broadcast to all channels so agents always see presence changes regardless of which channel they're monitoring.

### Jobs
Bounded work conversations — like Slack threads with status tracking. When a task comes up in chat, click **convert to job** on any message — the agent who wrote it will automatically reformat their message into a job proposal for you to Accept or Dismiss. You can also create jobs manually from the jobs panel. Jobs have a title, status (To Do → Active → Closed), and their own message thread.

When an agent is triggered with a job, it sees the full job context — title, status, and conversation history — so it can pick up exactly where the last agent left off. Jobs are visible regardless of which channel you're in.

Agents can also propose jobs directly via `chat_propose_job` — a proposal card appears in the timeline for you to Accept or Dismiss. The jobs panel opens from the header. Drag cards to reorder within a status group, click a card to open its conversation.

### Agent roles
Saved terminal agents choose a role and personality when they are created. Their
expanded startup instructions stay fixed for that saved agent across stop,
resume, and fresh conversations. Browser role pills show a lock for these agents;
create a new agent to choose a different profile. The session orchestrator has
its own fixed routing instructions and is separate from worker roles.

Unmanaged agents retain editable roles — Planner, Builder, Reviewer, Researcher,
or a custom role. These guide behavior through the wrapper's wake prompt.

For unmanaged agents, click a **status pill** for the role picker, or click the
**role pill** in a message header. Choose a preset or custom role (max 20
characters). Custom roles appear in both pickers; hover one to delete it. These
roles are global per agent, persist across server restarts, and can be cleared
with "None". Structured workflow role assignments remain separate from saved
agent profiles.

### Rules
Rules set the working style for your agents. Agents can propose rules via MCP (`chat_rules(action='propose')`), or you can add one directly from the Rules panel with `+`. Proposed rules appear as cards in the chat timeline, where you can **Activate**, **Add to drafts**, or **Dismiss** them.

The Rules panel opens from the header. Rules are grouped into **Active**, **Drafts**, and **Archive**. Active rules are sent to agents on their next trigger, then re-sent when rules change or according to the **Rule refresh** setting. Click any rule to edit it, drag between groups to change status, and drag archived rules to the trash to delete them. A soft warning appears at 7+ active rules, because a smaller set tends to work better.

**Remind agents** re-sends the current rules on the next trigger. The badge on the Rules button shows unseen proposals only. Max 160 chars per rule.

### Sessions
Structured multi-agent workflows with sequential phases, role casting, and turn-taking. Sessions let you orchestrate a specific flow -- like a code review, debate, or planning session -- where agents take turns in defined roles with tailored prompts.

**Built-in templates:** Code Review, Debate, Design Critique, and Planning. Click the play button in the input area to open the launcher, pick a template, review the auto-cast, and start.

**Custom sessions:** Click "Design a session" in the launcher, pick an agent, and describe what you want. The agent proposes a session draft as a card in the timeline. From there:
- **Run** -- opens a cast preview where you assign agents to roles, then starts the session
- **Save Template** -- saves the draft as a reusable template in the launcher
- **Request Changes** -- inline feedback form; the agent revises and the old draft is superseded
- **Dismiss** -- demotes the proposal to a compact chat summary

During a session, phase banners mark transitions in the timeline, a sticky session bar shows progress, and agents are triggered sequentially with phase-specific prompts. The output phase is highlighted when the session completes.

Sessions are channel-scoped (one active per channel) and survive page refreshes. Custom templates persist across restarts.

### Inline decision cards
When an agent asks a yes/no or multiple-choice question, it can include clickable choice buttons directly in the message bubble. Click a choice to send your answer as a tagged reply — no typing needed.

Agents include choices via `chat_send(message="Should I merge?", choices=["Yes", "No", "Show diff first"])`. When `choices` is empty or omitted, the message renders normally. Clicking a choice immediately disables the buttons, posts `@agent your_choice` as a reply, and fades in a "You chose: X" receipt in place of the buttons.

Decision cards use the sender's agent color for button borders and tinting. Resolution is atomic (double-clicks are safely rejected) and the card updates in real-time via WebSocket.

### Activity indicators
Status pills show a spinning border in each agent's color when that agent is actively working — so you can minimize the terminals and still know at a glance who's busy. Detection works by hashing the agent's terminal screen buffer every second: if anything changes (spinner, streaming text, tool output), the pill lights up. When the screen stops changing, it stops instantly. Cross-platform — Windows uses `ReadConsoleOutputW`, Mac/Linux uses `tmux capture-pane`.

### Multi-instance agents
Run multiple instances of the same provider — double-click the launcher again and a second instance auto-registers with its own identity, color, status pill, and @mention routing. No configuration needed.

- First Claude gets `claude`, second gets `claude-2`, third gets `claude-3`, etc.
- Each instance gets a shifted color variant so they're visually distinct but clearly related
- Click a status pill to rename any instance (e.g. "claude-2" → "code-review")
- When a second instance connects, a naming lightbox prompts you to give it a role name
- Renames update all existing messages in the DOM — sender names, colors, and avatars refresh instantly
- Identity breadcrumbs in `chat_read` responses let agents reclaim previous names after `/resume`
- MCP proxy per instance ensures each agent's tool calls route through the correct identity

<details>
<summary>Note on renaming and <code>/resume</code></summary>

When an agent resumes a previous session, it reads its chat history and tries to reclaim its old name automatically. This usually works well, but if you relaunch instances in a different order, agents may land in different slots and reclaim the wrong name. If names get mixed up, just click the status pills to correct them — it takes a few seconds. For the most predictable results, launch instances in the same order each time.
</details>

### Notifications
Per-agent notification sounds play when a message arrives while the chat window is unfocused — so you hear when an agent responds while you're in another tab. Pick from 7 built-in sounds (or "None") per agent in Settings. Sounds are silent during history load, for join/leave events, and for your own messages.

Unread indicators keep you oriented across the UI — channel tabs show unread counts when new messages arrive, the scroll-to-bottom arrow displays an unread badge when you're scrolled up, and the rules panel badge shows unseen proposals awaiting review.

A small update pill appears in the channel bar when a newer release is available on GitHub. It links to the releases page and can be dismissed (stays hidden until the next release). It uses the same yapp release check as the TUI and `yapp update`: the server asks GitHub for the latest [yapp release](https://github.com/Ankitkkkk/yapp/releases), caches the answer for 6 hours, and the pill stays hidden if anything is uncertain.

### Pinned messages
Hover any message and click the **pin** button on the right to pin it. Click again to mark it done, once more to unpin. The cycle: **not pinned → todo → done → cleared**. A colored strip on the left shows the state (purple = todo, green = done).

Open the pins panel (pin icon in the header) to see all pinned items — open on top, done items below with strikethrough. Pins persist across server restarts.

### Message deletion
Click **del** on any message to enter delete mode. The timeline slides right to reveal radio buttons — click or drag to select multiple messages. A confirmation bar slides up with the count. Hit **Delete** to confirm or **Cancel** / **Escape** to back out. Deletes messages from storage and cleans up any attached images.

### Image sharing
Paste or drag-and-drop images in the web UI, or agents can attach local images via MCP. Images render inline and open in a lightbox modal when clicked.

### Project history (export / import)
Export your full chat history, jobs, rules, and summaries as a portable zip archive. Import it on another machine to pick up where you left off. Open **Settings → Project History** to find the Export and Import buttons.

- **Export** downloads a zip containing your messages, jobs, rules, and channel summaries
- **Import** merges an archive into your current data — new records are added, duplicates are skipped
- Safe to import the same archive twice — the second import changes nothing
- If the archive contains channels that don't exist locally, they're auto-created
- Imported messages don't trigger agent responses — they're treated as history, not new activity

### Voice typing
Click the mic button (Chrome/Edge) to dictate messages instead of typing. Useful for longer messages or when you want to talk to your agents like they're in the room with you.

### Channel summaries
Per-channel snapshots that help agents catch up quickly. Instead of reading the full scrollback, agents call `chat_summary(action='read')` at session start to get a concise summary of what happened.

Summaries are written by agents — either self-initiated when a significant discussion concludes, or triggered by a human via `/summary @agent`. The server enforces a 1000-character cap. Summaries persist across restarts in `summaries.json`.

### Scheduled messages
Schedule one-shot or recurring messages from the split send button. Click the clock icon next to Send to open the schedule popover — pick a date/time for one-shot, or check Recurring and set an interval (minutes, hours, or days). Scheduled messages fire as real chat messages from you, complete with @mentions that trigger agents automatically.

Instead of a fixed date and time, check **In a while from now** and give it hours and minutes to send relative to the moment you schedule it, which helps when you are waiting on something rather than aiming at a clock time. A one-shot time that has already passed is refused with a reason instead of firing straight away.

A schedule strip above the composer shows active and paused schedules. For a single schedule, inline pause and delete controls appear directly in the strip. For multiple schedules, expand the strip to manage them. Schedules persist across server restarts (stored in `data/schedules.json`).

The schedule popover validates that at least one agent is toggled before enabling the Schedule button — a yellow warning tells you what's needed.

### Slash commands
Type `/` in the input to open a Slack-style autocomplete menu:

- `/summary @agent` — ask an agent to summarize recent messages in the current channel
- `/continue` — resume after the loop guard pauses an agent-to-agent chain
- `/clear` — clear messages in the current channel

### Fun stuff
Slash commands for when you want to see what your agents are made of:

- `/hatmaking` — all agents design an SVG hat for their avatar (see the gang above)
- `/artchallenge` — SVG art challenge with optional theme — agents create artwork and share it in chat
- `/roastreview` — all agents review and roast each other's recent work
- `/poetry haiku` — agents write a haiku about the codebase
- `/poetry limerick` — agents write a limerick about the codebase
- `/poetry sonnet` — agents write a sonnet about the codebase

Hats are SVG overlays (viewBox `0 0 32 16`, max 5KB) that sit above agent avatars in chat. They persist across page reloads. Drag a hat to the trash icon to remove it.

### Web chat UI

The optional browser interface retains the Yapp appearance:

![Yapp browser interface](screenshot.png)

Dark-themed chat at `localhost:8300` with real-time updates:

- @mention autocomplete with live agent list — type `@` to search online agents, "all agents", and the human user. Arrow keys to navigate, Enter/Tab to insert
- Pre-@ mention toggles to "lock on" to specific agents
- Reply threading with inline quotes that link back to the parent message
- GitHub-flavored markdown with code blocks, tables, and copy buttons
- Per-message copy button (raw markdown to clipboard)
- Slack-style colored @mention pills
- Clickable file paths (Explorer on Windows, Finder on macOS, file manager on Linux)
- Date dividers between different days
- Configurable history limit per channel
- Auto-linked URLs (no longer double-wraps URLs inside existing links)
- Configurable name, font (mono/sans), and high contrast mode
- Auto-saving settings (no Save button needed)
- Agent status pills (online/working/offline) with animated activity indicators
- Drag-scroll on overflowing pill bars and mention toggles
- Instance naming lightbox when multi-instance agents connect

### Token cost

Compared to manually copy-pasting messages between agent CLIs, yapp adds this overhead:

| Overhead | Extra tokens | Notes |
|----------|-------------|-------|
| Tool definitions in system prompt | ~850 input | One-time cost, persists in context all session |
| Per `chat_read` call | 30 + 40 per message | Tool invocation + JSON metadata wrapping each message |
| Per `chat_send` call | 45 | Tool invocation + response confirmation |

The message *content* itself costs the same either way — you'd read those words whether they arrive via MCP or pasted into your CLI. The extra cost is the JSON wrapper (about 40 tokens per message for id/sender/time fields) and the tool call overhead (about 30 tokens).

**Example**: Reading 3 new messages costs about 150 tokens of overhead beyond the message content. Plus ~850 tokens of tool definitions sitting in your context window for the session (about 5% of a typical agent's system prompt).

### Token-overload minimization
yapp is designed to keep coordination lightweight:

- `chat_read(sender=...)` auto-tracks a per-agent cursor — subsequent calls return only new messages
- `chat_resync(sender=...)` gives an explicit full refresh when you actually need it
- loop guard pauses long agent-to-agent chains and requires `/continue`
- reply threading + targeted `@mentions` reduce irrelevant context fanout
- only 10 MCP tools — minimizes system prompt overhead

### Presence & heartbeats
The wrapper sends a heartbeat ping every 5 seconds to keep the agent marked as "online". Any MCP tool call (chat_read, chat_send, etc.) also refreshes presence. If no activity is seen for 10 seconds, the agent is marked offline. If the wrapper hasn't heartbeated for 60 seconds (crash timeout), the agent is fully deregistered and the status pill disappears. Clean shutdown deregisters immediately.

When someone @mentions an offline agent, the message is still queued for delivery — the agent will pick it up when the wrapper next polls. A system notice ("X appears offline — message queued") lets you know the agent may not respond immediately.

### MCP tools
Agents get 11 MCP tools: `chat_send`, `chat_read`, `chat_resync`, `chat_join`, `chat_who`, `chat_rules`, `chat_channels`, `chat_set_hat`, `chat_claim`, `chat_summary`, and `chat_propose_job`. All message tools accept an optional `channel` parameter. Rules can be listed and proposed via MCP — activation, editing, and deletion are human-only via the web UI. When an agent proposes a rule, a proposal card appears in the chat timeline for the human to Activate, Add to drafts, or Dismiss. Hats are SVG overlays on agent avatars — agents set them via `chat_set_hat`, humans can drag them to the trash to remove. Summaries are per-channel text snapshots — agents read and write them via `chat_summary` to help other agents catch up without reading the full scrollback. Pinned messages are managed through the web UI only. `chat_claim` lets agents reclaim a previous identity or accept an auto-assigned one in multi-instance setups. Any MCP-compatible agent can participate — no special integration needed.

Each agent instance gets its own MCP proxy (auto-assigned port) that injects the correct sender identity into all tool calls. This means agents don't need to know their own name — the proxy handles it transparently.

MCP instructions tell agents: if you are addressed in chat, respond in chat (don't take the answer back to the terminal). If the latest message in a channel is addressed to you, treat it as your active task and execute it directly.

## Advanced setup

### Manual MCP registration

The start scripts auto-configure MCP on launch. If you prefer to register by hand:

**Claude Code:**
```bash
claude mcp add yapp --transport http http://127.0.0.1:8200/mcp
```

**Codex / other agents** — add to `.mcp.json` in your project root:
```json
{
  "mcpServers": {
    "yapp": {
      "type": "http",
      "url": "http://127.0.0.1:8200/mcp"
    }
  }
}
```

**Gemini** — add to `.gemini/settings.json` in your project root:
```json
{
  "mcpServers": {
    "yapp": {
      "type": "sse",
      "url": "http://127.0.0.1:8201/sse"
    }
  }
}
```

**Qwen** — add to `.qwen/settings.json` in your project root:
```json
{
  "mcpServers": {
    "yapp": {
      "type": "http",
      "url": "http://127.0.0.1:8200/mcp"
    }
  }
}
```

**Kilo** — add to `~/.config/kilo/kilo.json`:
```json
{
  "mcp": {
    "yapp": {
      "type": "remote",
      "url": "http://127.0.0.1:8200/mcp",
      "enabled": true
    }
  }
}
```

**CodeBuddy** — use the `start_codebuddy` launcher. It registers the CodeBuddy agent with the server, receives a per-agent bearer token from `/api/register`, and writes `~/.codebuddy/.mcp.json` with that token baked in. Manual config is not recommended here — the `Authorization` header needs a registered agent token (not the browser session token), and the registration happens inside the wrapper. If you really need to see the generated config, open `~/.codebuddy/.mcp.json` after the first launcher run.

**GitHub Copilot CLI** — use the `start_copilot` launcher. It writes `~/.copilot/mcp-config.json` with a registered agent bearer token, the same way CodeBuddy does. Install the CLI first with `npm install -g @github/copilot`. Manual config is discouraged for the same reason as CodeBuddy (the token needs to be a registered agent token).

### Starting the server separately

If you want to run the server without a launcher:

```bash
# Windows — Terminal 1: server only
windows\start.bat

# Mac/Linux — Terminal 1: server only
./macos-linux/start.sh

# Terminal 2 — agent wrapper (any platform)
python wrapper.py claude

# With auto-approve (flags pass through after --)
python wrapper.py claude -- --dangerously-skip-permissions
```

### Configuration

Edit `config.toml` to customize agents, ports, and routing:

```toml
[server]
port = 8300                 # web UI port
host = "127.0.0.1"

[agents.claude]
command = "claude"          # CLI command (must be on PATH)
cwd = ".."                  # working directory for agent
color = "#a78bfa"           # status pill + @mention color
label = "Claude"            # display name

[agents.codex]
command = "codex"
cwd = ".."
color = "#facc15"
label = "Codex"

[agents.gemini]
command = "gemini"
cwd = ".."
color = "#4285f4"
label = "Gemini"

[agents.kimi]
command = "kimi"
cwd = ".."
color = "#1783ff"
label = "Kimi"

[agents.qwen]
command = "qwen"
cwd = ".."
color = "#8b5cf6"
label = "Qwen"

[agents.kilo]
command = "kilo"
cwd = ".."
color = "#f7f677"
label = "Kilo"

[agents.minimax]
type = "api"
base_url = "https://api.minimax.io/v1"
model = "MiniMax-M3"
color = "#2fe898"
label = "MiniMax"
api_key_env = "MINIMAX_API_KEY"
temperature = 1.0

[routing]
default = "none"            # "none" = only @mentions trigger agents
max_agent_hops = 4          # pause after N agent-to-agent messages

[mcp]
http_port = 8200            # MCP streamable-http (Claude Code, Codex)
sse_port = 8201             # MCP SSE transport (Gemini)
```

### Per-project isolation

If you keep one yapp install shared across several repos (e.g. via dotfiles), you can run an isolated instance per project without editing `config.toml` — override the data directory and ports at launch time.

**CLI flags** (accepted by `run.py`, `wrapper.py`, and `wrapper_api.py`):

```bash
# Start the server for project A
python run.py \
  --data-dir ./project-a/.yapp \
  --port 8310 \
  --mcp-http-port 8210 \
  --mcp-sse-port 8211

# Launch a wrapper that connects to that same instance
python wrapper.py claude \
  --data-dir ./project-a/.yapp \
  --port 8310 \
  --mcp-http-port 8210 \
  --mcp-sse-port 8211
```

**Env vars** (equivalent — set once in your shell and every process picks them up):

- `YAPP_DATA_DIR` — overrides `server.data_dir`
- `YAPP_PORT` — overrides `server.port`
- `YAPP_MCP_HTTP_PORT` — overrides `mcp.http_port`
- `YAPP_MCP_SSE_PORT` — overrides `mcp.sse_port`
- `YAPP_UPLOAD_DIR` — overrides `images.upload_dir`

Relative paths resolve against the shell's current directory (not yapp's install location), so `./.yapp` ends up inside your project folder.

Server and wrappers share the same `YAPP_*` env vars and the same flag names, so a launcher/profile can run multiple isolated instances by passing matching values to each process. If no flags or env vars are set, `config.toml` is used exactly as before — zero change for existing setups.

With several instances open at once, set **Settings → This server** to give each one a short name. It appears beside the title in the header and in the browser tab title, so you can tell which project a tab belongs to. It is empty by default, which looks exactly as it does now.

### API agents (local models)

From a source checkout, connect a local model with an OpenAI-compatible API (Ollama, llama-server, LM Studio, vLLM, etc.) to the chat room. API agents get status pills, activity indicators, @mention routing, and multi-instance support — just like the CLI agents.

1. If `config.local.toml` does not already exist, copy the example config.
   Otherwise, preserve it and add the new section to the existing file:
   ```bash
   cp config.local.toml.example config.local.toml
   ```

2. Edit `config.local.toml` with your model's endpoint. Use a new agent name:
   local entries cannot replace built-in names such as `qwen` or `minimax`.
   ```toml
   [agents.local_qwen]
   type = "api"
   base_url = "http://localhost:8189/v1"
   model = "qwen3-4b"
   color = "#8b5cf6"
   label = "Qwen"
   ```

3. Start the server with `.venv/bin/python run.py` after saving the configuration.
   If it is already running, restart it after accounting for active work so it
   registers the new provider name. The wrapper alone cannot add it to a
   server that loaded the old configuration.

4. Start the wrapper:
   ```bash
   # Windows
   windows\start_api_agent.bat local_qwen

   # Mac/Linux
   sh macos-linux/start_api_agent.sh local_qwen

   # Or directly
   .venv/bin/python wrapper_api.py local_qwen
   ```

API agents participate in chat but are not tmux-backed terminal agents in the
TUI Add agent flow. The wrapper registers with the server, watches for @mentions, reads recent chat context, calls your model's `/v1/chat/completions` endpoint, and posts the response back. `config.local.toml` is gitignored so your local endpoints stay out of the repo.

### MiniMax (cloud API)

[MiniMax](https://platform.minimax.io) is a built-in cloud API agent. It uses the MiniMax-M3 model via MiniMax's OpenAI-compatible endpoint. To use it:

1. Get an API key at [platform.minimax.io](https://platform.minimax.io)

2. Set the environment variable:
   ```bash
   export MINIMAX_API_KEY=your-key-here
   ```

3. Launch:
   ```bash
   # Windows
   windows\start_minimax.bat

   # Mac/Linux
   sh macos-linux/start_minimax.sh

   # Or directly
   python wrapper_api.py minimax
   ```

Available models: `MiniMax-M3` (default), `MiniMax-M2.7`, `MiniMax-M2.7-highspeed` (faster). China mainland users can change `base_url` to `https://api.minimaxi.com/v1` in `config.toml`.

## Architecture

`yapp.py` (the `yapp` and `goon` commands) launches the terminal client. Its UI, forms, and terminal handoff
live in `cli_tui.py`, `cli_tui_view.py`, and `cli_tui_dialogs.py`. The client uses
authenticated HTTP and WebSocket connections to the same local server shown
below; `cli.py` remains a compatible entry point.

```
┌──────────────┐     WebSocket      ┌──────────────┐
│  Browser UI  │◄──────────────────►│   FastAPI     │
│  (chat.js)   │    port 8300       │   (app.py)    │
└──────────────┘                    │               │
                                    │  ┌──────────┐ │
┌──────────────┐    MCP (HTTP)      │  │  Store    │ │
│  AI Agent    │◄──► MCP Proxy ◄───►│  │ (JSONL)  │ │
│  (Claude,    │   (per-instance)   │  └──────────┘ │
│   Codex...)  │    auto port       │  ┌──────────┐ │
└──────┬───────┘                    │  │ Registry  │ │
       │                            │  │ (runtime) │ │
       │  stdin injection           │  └──────────┘ │
┌──────┴───────┐  POST /api/register│  ┌──────────┐ │
│  wrapper.py  │───────────────────►│  │  Router   │ │
│  Win32 /tmux │  watches queue     │  │ (@mention)│ │
└──────────────┘  files for triggers│  └──────────┘ │
                                    └──────────────┘
```

**Key files:**

| File | Purpose |
|------|---------|
| `run.py` | Entry point — starts MCP + web server |
| `app.py` | FastAPI WebSocket server, REST endpoints, registration API, security middleware |
| `store.py` | JSONL message persistence with observer callbacks |
| `registry.py` | Runtime agent registry — slot assignment, identity claims, rename tracking |
| `jobs.py` | Job store — JSON persistence, status tracking, threaded conversations |
| `rules.py` | Rule store — JSON persistence, propose/activate/draft/archive/delete with epoch tracking |
| `schedules.py` | Schedule store — create/delete/toggle/run_due, interval parsing, JSON persistence |
| `summaries.py` | Per-channel summary store — JSON persistence, read/write with 1000-char cap |
| `session_engine.py` | Session orchestration — phase advancement, turn triggering, prompt assembly |
| `session_store.py` | Session persistence — run state, template loading/validation, custom template storage |
| `session_templates/` | Built-in session templates (JSON) — code review, debate, design critique, planning |
| `router.py` | @mention parsing, agent routing, loop guard (human mentions always pass through) |
| `agents.py` | Writes trigger queue files for wrapper to pick up |
| `mcp_bridge.py` | MCP tool definitions (`chat_send`, `chat_read`, `chat_claim`, etc.) |
| `mcp_proxy.py` | Per-instance MCP proxy — injects sender identity into all tool calls |
| `wrapper.py` | Cross-platform dispatcher — registration, auto-trigger, heartbeat, activity monitor |
| `wrapper_windows.py` | Windows: keystroke injection + screen buffer activity detection |
| `wrapper_unix.py` | Mac/Linux: tmux bracketed-paste injection + pane capture activity detection |
| `config.toml` | All configuration (agents, ports, routing) |
| `windows/start_*_yolo/bypass.bat` | Auto-approve launchers (Windows) |
| `macos-linux/start_*_yolo/bypass.sh` | Auto-approve launchers (Mac/Linux) |

## Requirements

- **Python 3.11+** (uses `tomllib`)
- Provider CLIs installed and authenticated separately to start agents
- **Linux/macOS/WSL2**: `tmux` for TUI agent lifecycle and server auto-start
- An interactive terminal at least **80 × 18** for the full-screen TUI

Installing the package pulls in the server and terminal dependencies
(`requirements-cli.txt` lists the same set for manual setups). The browser/wrapper
quickstart scripts install their own server dependencies on first launch.
Native Windows chat can connect to a manually running server, but the TUI's
tmux lifecycle actions require Linux/macOS or WSL2.

## Platform notes

The inherited provider wrappers support auto-trigger on these platforms:

- **Windows** — `wrapper_windows.py` injects keystrokes into the agent's console via Win32 `WriteConsoleInput`. The agent runs as a direct subprocess.
- **Mac/Linux** — `wrapper_unix.py` runs the agent inside a `tmux` session and delivers each prompt as a single bracketed paste (`tmux paste-buffer -p`), so a CLI that supports bracketed paste reassembles a long prompt even when the pty splits it across reads. Detach with `Ctrl+B, D` to leave the agent running in the background; reattach with `tmux attach -t yapp-claude`.

The chat server and web UI are fully cross-platform (Python + browser).

## Security

yapp is designed for **localhost use only** and includes several protections:

- **Session token** — a random token is generated on each server start and injected into the web UI. All API and WebSocket requests must present this token.
- **Loopback-only registration** — agent registration, deregistration, and heartbeat endpoints only accept connections from localhost, preventing remote agent impersonation.
- **Origin checking** — the server rejects requests from origins that don't match `localhost` / `127.0.0.1`, preventing cross-origin and DNS rebinding attacks.
- **No `shell=True`** — subprocess calls avoid shell injection by passing argument lists directly.
- **Network binding warning** — if the server is configured to bind to a non-localhost address, it refuses to start unless you explicitly pass `--allow-network`.

The session token is displayed in the terminal on startup and is only accessible to processes on the same machine.

> **`--allow-network` warning:** Network mode binds to a LAN IP, which exposes the server to your local network over unencrypted HTTP. Anyone on the same network can sniff the session token and gain full access — including the ability to @mention agents and trigger tool execution. If agents are running with auto-approve flags, this effectively grants remote code execution on your machine. **Only use `--allow-network` on a trusted home network. Never on public or shared WiFi.**

## FAQ

### How do I make Claude Code and Codex talk to each other?
Install yapp, create a session, and add a Claude agent and a Codex agent. Both
connect to the same local MCP chat server, so each can @mention the other in
the shared channel and read the reply. See [Agent-to-agent communication](#agent-to-agent-communication).

### How do I run multiple AI coding agents in one terminal?
Run `yapp` (or `goon`). Each agent runs in its own tmux session; the TUI lists
them with status and unread counts, and **F6** attaches to any of them.

### Which AI agents and CLIs are supported?
Claude Code, OpenAI Codex CLI, Gemini CLI, Antigravity (agy), GitHub Copilot CLI,
Kimi, Qwen Code, Kilo CLI, CodeBuddy, MiniMax, and OpenAI-compatible local models
(for example Ollama or LM Studio) through [API agents](#api-agents-local-models).

### Is yapp free and open source?
Yes. yapp is MIT licensed and stores chats locally; the server binds to
localhost by default. Provider subscriptions and API usage are separate. Cloud
agents still send prompts and relevant context to their providers.

### Does it work on Windows or macOS?
Use Linux, macOS, or WSL2 for the tmux-backed terminal workflow. Native Windows
can use browser chat and wrapper launchers, but this CLI's terminal lifecycle
actions require tmux. Native Windows full-screen behavior is not validated end to end.

### What is the `goon` command?
An alias. Installing yapp creates both `yapp` and `goon`, and they run the same app.

### How is yapp related to Agentchattr?
yapp is built on [Agentchattr](https://github.com/bcurts/agentchattr) and keeps
its MCP chat server and browser UI, adding the full-screen terminal UI, sessions,
agent profiles, orchestration, and one-command installation.

### Why does install fail with `externally-managed-environment`?
That error comes from running plain `pip` against the system Python on
Ubuntu/Debian (often because the shell auto-corrected `pipx` to `pip`). Use the
installer (`curl -fsSL https://yapp.riggedcode.com/install.sh | sh`), or install pipx first with `sudo apt install pipx`.
Do not pass `--break-system-packages`.

## Project and upstream

Report yapp issues and feature requests in
[Ankitkkkk/yapp](https://github.com/Ankitkkkk/yapp/issues).
The server and browser foundation is [Agentchattr](https://github.com/bcurts/agentchattr).
Its upstream community is available on the [Agentchattr Discord](https://discord.gg/qzfn5YTT9a).

### Publishing a release

Use the source checkout's `release.py` with Python 3.11+, Git, and the
[GitHub CLI](https://cli.github.com/). Authenticate once with `gh auth login`.
Commit and push the changes you want to release first, and verify their tests.
Run the script from a clean `main` checkout synchronized with `origin/main`:

```sh
python3 release.py --dry-run  # preview only, including without gh or network access
python3 release.py            # minor: 0.5.0 -> 0.6.0; 1.9.7 -> 1.10.0
python3 release.py --major    # major: 0.6.0 -> 1.0.0; 1.9.7 -> 2.0.0
```

Choose either the default command or `--major` for each release. The script
commits only `VERSION`, creates a matching annotated tag, atomically pushes
the release commit and tag to `origin`, and publishes a stable GitHub release
with generated notes. It rechecks published versions before publication and
uses GitHub's automatic version-based latest selection. No separate ZIP upload is needed: installed
copies download GitHub's source archive, which contains the TUI and its package
dependencies. This does not use the older `build_release.py` ZIP builder.

Publishing requires `origin` to point to `Ankitkkkk/yapp` (the repository used
by the updater), permission to push to `main` and create releases, and working
Git and GitHub authentication. Dirty checkouts, detached/other branches,
unsynchronized commits, existing tags, and conflicting release versions stop
the script before it changes `VERSION`. Dry-run only shows the plan; it does
not check these Git/GitHub prerequisites or run tests.

If a push or publication fails after the version commit, inspect `git status`
and finish the same release with:

```sh
python3 release.py --resume
```

Resume requires the clean version-only release commit at `HEAD`. It verifies
existing tags, reuses the version, and publishes only if the release is missing.
If the version commit itself failed, inspect the remaining `VERSION` change
and complete that commit with the shown `release: vX.Y.Z` subject first.
The script never resets work, moves existing tags, or force-pushes. Existing
GitHub drafts/prereleases are left for you to finish in GitHub.

For a manual release, bump `VERSION`, commit and push to `main`, then create a
GitHub Release using the matching `vX.Y.Z` tag on that commit.

Installed copies pick it up within 6 hours. Keep the tag and `VERSION` in sync:
after installing, yapp checks that the installed version equals the tag, so a
tag that does not match `VERSION` on that commit makes every update fail
verification, on every check, until a matching release is published.

## License

MIT
