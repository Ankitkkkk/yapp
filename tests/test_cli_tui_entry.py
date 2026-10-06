"""Offline capability selection and terminal entry regressions."""

import io
import builtins
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from types import ModuleType
import unittest
from unittest.mock import AsyncMock, Mock, patch

import cli
from cli_api import CLIError


FALLBACK = "Full-screen unavailable; using plain mode.\n"


class TerminalStream(io.StringIO):
    def __init__(self, value="", *, tty):
        super().__init__(value)
        self.tty = tty

    def isatty(self):
        return self.tty


class EntryHarness(unittest.TestCase):
    def run_main(self, argv=(), *, stdin_tty=True, stdout_tty=True,
                 platform="linux", term="xterm-256color", ensure=None,
                 tui_effect=None, install_tui=True, export_tui=True,
                 tui_import_error=None, legacy_effect=None, extra_env=None):
        stdin = TerminalStream(tty=stdin_tty)
        stdout = TerminalStream(tty=stdout_tty)
        stderr = io.StringIO()
        config = {
            "server": {"port": 18300, "data_dir": "/tmp/entry-data"},
            "images": {"upload_dir": "/tmp/entry-uploads"},
            "mcp": {"http_port": 18200, "sse_port": 18201},
            "agents": {"inert": {}},
        }
        ensure = ensure or Mock(return_value={"paused": False, "data_dir": "/tmp/entry-data"})
        legacy = AsyncMock(side_effect=legacy_effect)
        tui = AsyncMock(side_effect=tui_effect)
        module = ModuleType("cli_tui")
        if export_tui:
            module.interactive_tui = tui
        original_import = builtins.__import__
        def import_module(name, *args, **kwargs):
            if name == "cli_tui" and tui_import_error is not None:
                raise tui_import_error
            return original_import(name, *args, **kwargs)
        environment = {} if term is None else {"TERM": term}
        environment.update(extra_env or {})
        env_after = {}
        with patch.object(cli.sys, "stdin", stdin), \
                patch.object(cli.sys, "platform", platform), \
                patch.dict(os.environ, environment, clear=True), \
                redirect_stdout(stdout), redirect_stderr(stderr), \
                patch.object(cli, "load_config", return_value=config) as load_config, \
                patch.object(cli, "ensure_server", ensure), \
                patch.object(cli, "interactive", legacy), \
                patch("builtins.__import__", side_effect=import_module), \
                patch.dict(sys.modules, {"cli_tui": module if install_tui else None}):
            try:
                cli.main(list(argv))
                code = 0
            except SystemExit as error:
                code = error.code
            env_after.update(os.environ)
        return {"env_after": env_after,
            "code": code, "stdout": stdout.getvalue(), "stderr": stderr.getvalue(),
            "ensure": ensure, "legacy": legacy, "tui": tui, "config": load_config,
        }


class ModeChoiceTests(unittest.TestCase):
    def mode(self, **overrides):
        self.assertTrue(hasattr(cli, "choose_interactive_mode"),
                        "choose_interactive_mode is missing")
        options = dict(plain=False, stdin_tty=True, stdout_tty=True,
                       platform="linux", term="xterm-256color")
        options.update(overrides)
        return cli.choose_interactive_mode(**options)

    def test_explicit_plain_and_non_terminal_output_choose_plain(self):
        self.assertEqual(self.mode(plain=True), "plain")
        self.assertEqual(self.mode(stdout_tty=False), "plain")

    def test_posix_unusable_term_chooses_plain(self):
        for term in (None, "", "dumb", "DUMB"):
            with self.subTest(term=term):
                self.assertEqual(self.mode(term=term), "plain")

    def test_unset_empty_and_dumb_term_do_not_force_windows_fallback(self):
        for term in (None, "", "dumb", "DUMB"):
            with self.subTest(term=term):
                self.assertEqual(self.mode(platform="win32", term=term), "tui")

    def test_false_stdin_is_defensively_plain(self):
        self.assertEqual(self.mode(stdin_tty=False), "plain")


class ParserTests(unittest.TestCase):
    def test_plain_defaults_false_and_survives_both_chat_positions(self):
        parser = cli.build_parser()
        self.assertFalse(parser.parse_args([]).plain)
        self.assertTrue(parser.parse_args(["--plain", "chat"]).plain)
        self.assertTrue(parser.parse_args(["chat", "--plain"]).plain)

    def test_plain_after_shell_command_is_rejected_by_parser(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["status", "--plain"])
        self.assertEqual(caught.exception.code, 2)


class MainEntryTests(EntryHarness):
    def test_missing_markdown_dependency_explains_install_before_server_start(self):
        for name in ('rich.markdown', 'markdown_it'):
            with self.subTest(name=name), patch.dict(sys.modules, {name: None}):
                result = self.run_main()
                self.assertEqual(result['code'], 1)
                self.assertIn('Install terminal dependencies', result['stderr'])
                result['ensure'].assert_not_called()
                plain = self.run_main(['--plain'])
                self.assertEqual(plain['code'], 0, plain['stderr'])

    def test_pre_command_plain_shell_rejects_before_dependencies_or_side_effects(self):
        original_import = builtins.__import__
        imported = []
        def observe_import(name, *args, **kwargs):
            imported.append(name)
            return original_import(name, *args, **kwargs)
        with patch.object(cli, "load_config", side_effect=AssertionError("config")) as config, \
                patch.object(cli, "ChatClient", side_effect=AssertionError("client")) as client, \
                patch("builtins.__import__", side_effect=observe_import), \
                patch.dict(sys.modules, {"cli_tui": None}):
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                cli.main(["--plain", "status"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--plain is only available for chat", stderr.getvalue())
        config.assert_not_called()
        client.assert_not_called()
        self.assertFalse(any(name.startswith("websockets") for name in imported))

    def test_non_tty_stdin_refuses_before_mode_or_side_effects(self):
        with patch.object(cli.sys, "stdin", TerminalStream(tty=False)), \
                patch.object(cli, "choose_interactive_mode",
                             side_effect=AssertionError("mode"), create=True) as mode, \
                patch.object(cli, "load_config", side_effect=AssertionError("config")) as config, \
                patch.dict(sys.modules, {"cli_tui": None}), \
                redirect_stderr(io.StringIO()) as stderr, \
                self.assertRaises(SystemExit) as caught:
            cli.main(["chat"])
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(stderr.getvalue(),
                         "Interactive chat requires a terminal. Use read or send for scripts.\n")
        mode.assert_not_called()
        config.assert_not_called()

    def test_capable_terminal_dispatches_tui_for_default_selector_and_channel(self):
        cases = [
            ((), None, False, "general"),
            (("chat", "--session", "billing", "--no-resume"), "billing", True, "general"),
            (("chat", "--channel", "support"), None, False, "support"),
        ]
        for argv, selector, no_resume, channel in cases:
            with self.subTest(argv=argv):
                result = self.run_main(argv)
                self.assertEqual(result["code"], 0, result["stderr"])
                result["legacy"].assert_not_awaited()
                result["tui"].assert_awaited_once()
                client, controller = result["tui"].await_args.args
                self.assertEqual(controller.selector, selector)
                self.assertEqual(controller.no_resume, no_resume)
                self.assertEqual(client.channel, channel)
                self.assertEqual(controller.plain_channel, channel == "support")
                self.assertEqual(result["tui"].await_args.kwargs["initial_notices"], [])
                self.assertEqual(result["stderr"], "")

    def test_explicit_plain_both_positions_use_legacy_without_notice_or_tui_import(self):
        for argv in (("--plain",), ("--plain", "chat"), ("chat", "--plain")):
            with self.subTest(argv=argv):
                result = self.run_main(argv)
                self.assertEqual(result["code"], 0, result["stderr"])
                result["legacy"].assert_awaited_once()
                result["tui"].assert_not_awaited()
                self.assertEqual(result["stderr"], "")
        result = self.run_main(("chat", "--plain"), install_tui=False)
        self.assertEqual(result["code"], 0, result["stderr"])
        result["legacy"].assert_awaited_once()

    def test_automatic_fallback_prints_one_notice_and_uses_legacy(self):
        cases = [
            dict(stdout_tty=False, platform="linux", term="xterm-256color"),
            dict(stdout_tty=True, platform="linux", term=None),
            dict(stdout_tty=True, platform="linux", term="dumb"),
        ]
        for options in cases:
            with self.subTest(options=options):
                result = self.run_main(**options)
                self.assertEqual(result["code"], 0, result["stderr"])
                result["legacy"].assert_awaited_once()
                result["tui"].assert_not_awaited()
                self.assertEqual(result["stderr"], FALLBACK)

    def test_windows_unset_term_still_dispatches_tui(self):
        result = self.run_main(platform="win32", term=None)
        self.assertEqual(result["code"], 0, result["stderr"])
        result["tui"].assert_awaited_once()
        result["legacy"].assert_not_awaited()
        self.assertEqual(result["stderr"], "")

    def test_startup_notices_are_sanitized_printed_once_and_forwarded_in_order(self):
        def ensure(url, *, explicit_url, config, output):
            output("Started\x1b[31m server")
            output("Warning:\u202e mismatch")
            return {"paused": False, "data_dir": "/tmp/entry-data"}
        result = self.run_main(ensure=Mock(side_effect=ensure))
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["stdout"], "Started[31m server\nWarning: mismatch\n")
        self.assertEqual(result["tui"].await_args.kwargs["initial_notices"],
                         ["Started[31m server", "Warning: mismatch"])

    def test_update_relaunch_execs_same_command_with_state_path(self):
        import updates
        seen = {}
        def execv(executable, argv):
            seen['env'] = os.environ.get(updates.RELAUNCH_ENV)
        with patch.object(cli.os, "execv", side_effect=execv) as mocked, \
                patch.object(cli.sys, "argv", ["yapp", "chat"]):
            result = self.run_main(("chat",), tui_effect=lambda *a, **k: True)
        self.assertEqual(result["code"], 0, result["stderr"])
        mocked.assert_called_once_with(sys.executable, [sys.executable, "yapp", "chat"])
        self.assertEqual(seen['env'], str(updates.relaunch_path("/tmp/entry-data")))
        updater = result["tui"].await_args.kwargs["updater_factory"](Mock())
        self.assertEqual(updater.data_dir, "/tmp/entry-data")
        with patch.object(cli.os, "execv") as mocked:
            self.run_main(("chat",), tui_effect=lambda *a, **k: False)
        mocked.assert_not_called()

    def run_relaunched(self, data, argv, state_path):
        import updates
        seen = {}
        def ensure(*args, **kwargs):
            seen['env'] = os.environ.get(updates.RELAUNCH_ENV)
            return {"paused": False, "data_dir": data}
        extra = {} if state_path is None else {updates.RELAUNCH_ENV: str(state_path)}
        result = self.run_main(argv, ensure=Mock(side_effect=ensure), extra_env=extra)
        result["env_at_server_start"] = seen.get('env')
        result["env_after"] = result["env_after"].get(updates.RELAUNCH_ENV)
        return result

    def test_relaunch_state_comes_only_from_the_environment_path(self):
        import tempfile
        import updates
        with tempfile.TemporaryDirectory() as data:
            path = updates.save_relaunch_state(data, drafts=[(("session", "w1"), "hi", 2)],
                                               session_id="w1", channel=None,
                                               notices=["Updated to yapp 0.6.0."])
            result = self.run_relaunched(data, ("chat",), None)
            self.assertIsNone(result["tui"].await_args.kwargs["restored"])
            self.assertTrue(path.exists())
            result = self.run_relaunched(data, ("chat",), path)
            self.assertEqual(result["code"], 0, result["stderr"])
            _, controller = result["tui"].await_args.args
            self.assertIsNone(controller.selector)  # A soft preference, applied by the TUI.
            restored = result["tui"].await_args.kwargs["restored"]
            self.assertEqual(restored["session_id"], "w1")
            self.assertEqual(restored["drafts"], [(("session", "w1"), "hi", 2)])
            self.assertFalse(path.exists())
            self.assertIsNone(result["env_after"])
            self.assertIsNone(result["env_at_server_start"])
            path = updates.save_relaunch_state(data, drafts=[], session_id=None, channel="ops", notices=[])
            result = self.run_relaunched(data, ("chat", "--channel", "support"), path)
            client, controller = result["tui"].await_args.args
            self.assertEqual(client.channel, "ops")
            self.assertIsNone(controller.selector)

    def test_startup_failure_keeps_automatic_fallback_before_error(self):
        result = self.run_main(stdout_tty=False,
            ensure=Mock(side_effect=CLIError("The local server is unavailable.\nStart it manually")))
        self.assertEqual(result["code"], 1)
        self.assertEqual(result["stderr"],
                         FALLBACK + "The local server is unavailable.\nStart it manually\n")
        result["legacy"].assert_not_awaited()
        result["tui"].assert_not_awaited()

    def test_tui_clierror_uses_existing_exit_one_path(self):
        for message in ("No session matches missing", "Ambiguous session selector: bill",
                        "Archived session was not selected"):
            with self.subTest(message=message):
                result = self.run_main(tui_effect=CLIError(message))
                self.assertEqual(result["code"], 1)
                self.assertEqual(result["stderr"], message + "\n")
                result["legacy"].assert_not_awaited()
        result = self.run_main(tui_effect=ValueError("Invalid full-screen selection"))
        self.assertEqual(result["code"], 1)
        self.assertEqual(result["stderr"], "Invalid full-screen selection\n")
        result["legacy"].assert_not_awaited()

    def test_unexpected_full_screen_errors_name_only_type_and_recommend_plain(self):
        secret = "ws://127.0.0.1:18300/ws?token=authenticated-secret"
        cases = [
            (dict(tui_effect=RuntimeError(secret)), "RuntimeError"),
            (dict(tui_import_error=ImportError(secret)), "ImportError"),
            (dict(tui_effect=TimeoutError(secret)), "TimeoutError"),
        ]
        for options, type_name in cases:
            with self.subTest(type_name=type_name):
                result = self.run_main(**options)
                self.assertEqual(result["code"], 1)
                self.assertEqual(result["stderr"],
                    "Full-screen terminal stopped after an unexpected local error "
                    f"({type_name}); rerun with --plain.\n")
                self.assertNotIn("authenticated-secret", result["stderr"])
                self.assertNotIn("Start it manually", result["stderr"])
                self.assertNotIn("Server request", result["stderr"])
                result["legacy"].assert_not_awaited()

    def test_full_screen_keyboard_interrupt_keeps_130_and_plain_error_mapping_stays_legacy(self):
        result = self.run_main(tui_effect=KeyboardInterrupt())
        self.assertEqual(result["code"], 130)
        self.assertEqual(result["stderr"], "")
        result = self.run_main(("--plain",),
                               legacy_effect=RuntimeError("plain local error"))
        self.assertEqual(result["code"], 1)
        self.assertEqual(result["stderr"],
            "Server request failed or timed out. Check run.py and --url. For send, "
            "delivery may be uncertain; check history before retrying.\n")
        self.assertNotIn("Full-screen terminal stopped", result["stderr"])
        result["legacy"].assert_awaited_once()

    def test_shell_json_stays_exact_and_never_imports_tui(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli.sys, "stdin", TerminalStream(tty=False)), \
                redirect_stdout(stdout), redirect_stderr(stderr), \
                patch.object(cli, "shell_command", AsyncMock(return_value={"paused": False})), \
                patch.dict(sys.modules, {"cli_tui": None}):
            cli.main(["status", "--json", "--url", "http://127.0.0.1:18300"])
        self.assertEqual(stdout.getvalue(), '{"paused": false}\n')
        self.assertEqual(stderr.getvalue(), "")


class ImportOrderTests(unittest.TestCase):
    def test_cli_and_tui_import_cleanly_in_both_orders(self):
        for statement in ("import cli; import cli_tui", "import cli_tui; import cli"):
            with self.subTest(statement=statement):
                result = subprocess.run([sys.executable, "-c", statement],
                                        cwd=os.path.dirname(cli.__file__),
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
