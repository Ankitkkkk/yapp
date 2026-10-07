"""Offline version reporting through the parser and executable entry point."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import cli

ROOT = Path(__file__).resolve().parents[1]


class VersionCommandTests(unittest.TestCase):
    def test_version_exits_successfully_before_loading_config_or_starting_server(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('updates.RUNNING_VERSION', '1.2.3'), \
                patch.object(cli, 'load_config') as config, \
                patch.object(cli, 'ensure_server') as server, \
                redirect_stdout(stdout), redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as stopped:
            cli.main(['--version'], prog='yapp')
        self.assertEqual(stopped.exception.code, 0, stderr.getvalue())
        self.assertEqual(stdout.getvalue(), 'yapp 1.2.3\n')
        self.assertEqual(stderr.getvalue(), '')
        config.assert_not_called()
        server.assert_not_called()

    def test_missing_version_is_reported_as_unknown(self):
        stdout = io.StringIO()
        with patch('updates.RUNNING_VERSION', ''), redirect_stdout(stdout), \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            cli.main(['--version'], prog='yapp')
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(stdout.getvalue(), 'yapp unknown\n')

    def test_entry_point_uses_bundled_version_from_another_working_directory(self):
        with tempfile.TemporaryDirectory(prefix='yapp-version-') as directory:
            # A project's VERSION must not override the CLI's own version.
            Path(directory, 'VERSION').write_text('99.98.97\n')
            result = subprocess.run([sys.executable, str(ROOT / 'yapp.py'), '--version'],
                                    cwd=directory, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f'yapp {(ROOT / "VERSION").read_text().strip()}\n')
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
