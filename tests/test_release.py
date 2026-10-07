"""Release publishing: real temporary Git repos, mocked GitHub calls only."""
import importlib
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'release.py'


def is_publication(command):
    return command[:2] == ['gh', 'api'] and '--method' in command and 'POST' in command


class ReleasePreviewTests(unittest.TestCase):
    def preview(self, version, *args):
        with tempfile.TemporaryDirectory(prefix='yapp-release-preview-') as directory:
            root = Path(directory)
            (root / 'VERSION').write_text(version + '\n')
            if SCRIPT.is_file():
                shutil.copy2(SCRIPT, root / 'release.py')
            result = subprocess.run([sys.executable, str(root / 'release.py'), '--dry-run', *args],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual((root / 'VERSION').read_text(), version + '\n')
            self.assertFalse((root / '.git').exists())
            return result

    def test_default_preview_increments_minor_and_resets_patch_without_git_or_gh(self):
        result = self.preview('1.9.7')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('1.9.7 -> 1.10.0', result.stdout)
        self.assertIn('v1.10.0', result.stdout)

    def test_major_preview_resets_minor_and_patch(self):
        result = self.preview('1.9.7', '--major')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('1.9.7 -> 2.0.0', result.stdout)

    def test_invalid_versions_fail_without_changing_files(self):
        for version in ('', 'v1.2.3', '1.2', '01.2.3', '1.2.3-beta', '-1.2.3'):
            with self.subTest(version=version):
                result = self.preview(version)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('VERSION', result.stderr)


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'release.py has not been implemented')
        self.release = importlib.import_module('release')
        directory = tempfile.TemporaryDirectory(prefix='yapp-release-test-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / 'checkout'
        self.remote = self.root / 'origin.git'
        self.repo.mkdir()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT='0')
        self.git('init', '--bare', str(self.remote))
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Release Test')
        self.git('config', 'user.email', 'release@example.invalid')
        (self.repo / 'VERSION').write_text('0.5.7\n')
        self.git('add', 'VERSION')
        self.git('commit', '-m', 'Existing TUI improvements')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', 'origin', 'main')
        self.original_head = self.git('rev-parse', 'HEAD')
        self.calls = []
        self.releases = []
        self.push_url = 'git@github.com:Ankitkkkk/yapp.git'
        self.failure = None
        self.out = []
        self.errors = []

    def git(self, *args, root=None):
        result = subprocess.run(['git', *args], cwd=root or self.repo, env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def runner(self, command, **kwargs):
        self.calls.append(command)
        if self.failure and self.failure(command):
            return subprocess.CompletedProcess(command, 1, '', 'simulated failure')
        # Git pushes operate on a real isolated bare repo. Only the declared
        # GitHub push URL is substituted so production destination checks apply.
        if command[:3] == ['git', 'remote', 'get-url']:
            return subprocess.CompletedProcess(command, 0, self.push_url + '\n', '')
        if command[0] == 'gh':
            if command[1:3] == ['auth', 'status']:
                return subprocess.CompletedProcess(command, 0, '', '')
            if command[1] == 'api' and not is_publication(command):
                self.assertIn('--paginate', command)
                self.assertIn('--slurp', command)
                return subprocess.CompletedProcess(command, 0, json.dumps([self.releases]), '')
            if is_publication(command):
                fields = dict(value.split('=', 1) for index, value in enumerate(command)
                              if index > 0 and command[index - 1] in ('-f', '-F'))
                tag = fields['tag_name']
                self.assertEqual(fields['make_latest'], 'legacy')
                self.assertEqual(fields['generate_release_notes'], 'true')
                self.assertEqual(fields['draft'], 'false')
                self.assertEqual(fields['prerelease'], 'false')
                self.assertEqual(command[2], 'repos/Ankitkkkk/yapp/releases')
                # A publish may only happen after the exact version reached Git.
                self.assertEqual(self.git('show', f'{tag}:VERSION', root=self.remote), tag[1:])
                self.assertEqual(self.git('rev-parse', f'{tag}^{{}}', root=self.remote), fields['target_commitish'])
                url = 'https://github.com/Ankitkkkk/yapp/releases/tag/' + tag
                record = {'tag_name': tag, 'draft': False, 'prerelease': False, 'html_url': url}
                self.releases.append(record)
                return subprocess.CompletedProcess(command, 0, json.dumps(record), '')
            self.fail(f'Unexpected GitHub command: {command}')
        kwargs['env'] = self.env
        return subprocess.run(command, **kwargs)

    def run_release(self, *args):
        return self.release.main(list(args), root=self.repo, runner=self.runner,
                                 output=self.out.append, error_output=self.errors.append)

    def assert_untouched(self):
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.original_head)
        self.assertEqual((self.repo / 'VERSION').read_text(), '0.5.7\n')
        self.assertFalse(any(is_publication(call) for call in self.calls))

    def test_default_release_publishes_matching_minor_version_commit_and_tag(self):
        self.assertEqual(self.run_release(), 0, self.errors)
        self.assertEqual((self.repo / 'VERSION').read_text(), '0.6.0\n')
        head = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.git('rev-parse', 'main', root=self.remote), head)
        self.assertEqual(self.git('rev-parse', 'v0.6.0^{}', root=self.remote), head)
        self.assertEqual(self.git('diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD'), 'VERSION')
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertIn('https://github.com/Ankitkkkk/yapp/releases/tag/v0.6.0', '\n'.join(self.out))

    def test_major_release_publishes_one_zero_zero(self):
        self.assertEqual(self.run_release('--major'), 0, self.errors)
        self.assertEqual(self.git('show', 'v1.0.0:VERSION', root=self.remote), '1.0.0')

    def test_git_follow_tags_setting_does_not_publish_unrelated_tags(self):
        self.git('config', 'push.followTags', 'true')
        self.git('tag', '-a', 'private-review-marker', '-m', 'Keep this local')
        self.assertEqual(self.run_release(), 0, self.errors)
        self.assertEqual(self.git('tag', '--list', root=self.remote), 'v0.6.0')
        self.assertIn('private-review-marker', self.git('tag', '--list'))

    def test_dirty_tree_is_not_committed(self):
        (self.repo / 'private.txt').write_text('unrelated local work')
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()
        self.assertIn('clean', '\n'.join(self.errors))

    def test_wrong_branch_is_not_released(self):
        self.git('switch', '-c', 'feature')
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()

    def test_unpushed_commits_require_sync_before_bumping(self):
        self.git('commit', '--allow-empty', '-m', 'Unpushed work')
        self.original_head = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()

    def test_upstream_destination_is_rejected(self):
        self.push_url = 'https://github.com/bcurts/agentchattr.git'
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()

    def test_multiple_push_destinations_are_rejected(self):
        self.push_url += '\nhttps://github.com/another/repo.git'
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()

    def test_auth_failure_is_reported_before_version_changes(self):
        self.failure = lambda c: c[:3] == ['gh', 'auth', 'status']
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()

    def test_release_lookup_failure_is_not_treated_as_missing_release(self):
        self.failure = lambda c: c[:2] == ['gh', 'api']
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()

    def test_malformed_github_lookup_does_not_bump_version(self):
        original = self.runner

        def malformed(command, **kwargs):
            if command[:2] == ['gh', 'api'] and not is_publication(command):
                return subprocess.CompletedProcess(command, 0, '{"message":"unexpected"}', '')
            return original(command, **kwargs)

        self.runner = malformed
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()

    def test_newer_published_release_is_not_replaced_as_latest(self):
        self.releases.append({'tag_name': 'v2.0.0', 'draft': False,
                              'prerelease': False, 'html_url': 'https://github.com/Ankitkkkk/yapp/releases/tag/v2.0.0'})
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()

    def test_missing_github_cli_fails_before_version_changes(self):
        original = self.runner

        def without_gh(command, **kwargs):
            if command[0] == 'gh':
                raise FileNotFoundError('gh')
            return original(command, **kwargs)

        self.runner = without_gh
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()
        self.assertIn('gh is required', '\n'.join(self.errors))

    def test_failed_version_commit_leaves_changes_visible_and_no_tag(self):
        self.failure = lambda c: c[:2] == ['git', 'commit']
        self.assertEqual(self.run_release(), 1)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.original_head)
        self.assertIn('VERSION', self.git('status', '--porcelain'))
        self.assertEqual(self.git('tag', '--list'), '')
        self.assertEqual(self.git('rev-parse', 'main', root=self.remote), self.original_head)

    def test_version_rewritten_during_commit_is_not_published(self):
        def rewrite(command):
            if command[:2] == ['git', 'commit']:
                (self.repo / 'VERSION').write_text('9.0.0\n')
            return False

        self.failure = rewrite
        self.assertEqual(self.run_release(), 1)
        self.assertEqual(self.git('tag', '--list'), '')
        self.assertEqual(self.git('rev-parse', 'main', root=self.remote), self.original_head)

    def advance_remote(self, head):
        tree = self.git('rev-parse', 'main^{tree}', root=self.remote)
        result = subprocess.run(['git', '-c', 'user.name=Teammate', '-c',
            'user.email=teammate@example.invalid', 'commit-tree', tree, '-p', head, '-m', 'Later work'],
            cwd=self.remote, env=self.env, text=True, capture_output=True, check=True, timeout=10)
        next_commit = result.stdout.strip()
        self.git('update-ref', 'refs/heads/main', next_commit, root=self.remote)
        return next_commit

    def test_main_race_rejects_both_branch_and_tag_atomically(self):
        advanced = []

        def race(command):
            if command[:2] == ['git', 'push']:
                advanced.append(self.advance_remote(self.original_head))
            return False

        self.failure = race
        self.assertEqual(self.run_release(), 1)
        self.assertEqual(self.git('rev-parse', 'main', root=self.remote), advanced[0])
        self.assertEqual(self.git('tag', '--list', root=self.remote), '')
        self.assertFalse(any(is_publication(c) for c in self.calls))
        self.failure = None
        self.assertEqual(self.run_release('--resume'), 1)
        self.assertIn('Reconcile main', '\n'.join(self.errors))

    def test_newer_release_published_during_push_blocks_older_publication(self):
        original = self.runner

        def publish_race(command, **kwargs):
            result = original(command, **kwargs)
            if command[:2] == ['git', 'push'] and result.returncode == 0:
                self.releases.append({'tag_name': 'v1.0.0', 'draft': False,
                                      'prerelease': False, 'html_url': 'https://github.com/Ankitkkkk/yapp/releases/tag/v1.0.0'})
            return result

        self.runner = publish_race
        self.assertEqual(self.run_release(), 1)
        self.assertFalse(any(is_publication(c) for c in self.calls))

    def test_existing_local_tag_does_not_get_overwritten(self):
        self.git('tag', 'v0.6.0')
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()
        self.assertEqual(self.git('rev-parse', 'v0.6.0'), self.original_head)

    def test_existing_remote_tag_does_not_get_overwritten(self):
        self.git('tag', 'v0.6.0', self.original_head, root=self.remote)
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()

    def test_existing_release_blocks_reusing_version_even_without_a_tag(self):
        self.releases.append({'tag_name': 'v0.6.0', 'draft': True,
                              'prerelease': False, 'html_url': 'https://github.com/Ankitkkkk/yapp/releases/1'})
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()

    def test_failed_push_can_resume_without_another_bump(self):
        self.failure = lambda c: c[:2] == ['git', 'push']
        self.assertEqual(self.run_release(), 1)
        head = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(head, self.original_head)
        self.assertEqual(self.git('tag', '--list', root=self.remote), '')
        self.assertIn('--resume', '\n'.join(self.errors))
        self.failure = None
        self.assertEqual(self.run_release(), 1, 'must not silently skip to v0.7.0')
        self.assertEqual(self.run_release('--resume'), 0, self.errors)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.git('tag', '--list', root=self.remote), 'v0.6.0')

    def test_failed_publish_can_resume_after_main_advances(self):
        self.failure = lambda c: is_publication(c)
        self.assertEqual(self.run_release(), 1)
        head = self.git('rev-parse', 'HEAD')
        # A teammate advances origin/main after our atomic branch/tag push.
        next_commit = self.advance_remote(head)
        self.failure = None
        self.assertEqual(self.run_release('--resume'), 0, self.errors)
        self.assertEqual(self.git('rev-parse', 'main', root=self.remote), next_commit)
        self.assertEqual(self.git('rev-parse', 'v0.6.0^{}', root=self.remote), head)

    def test_resume_of_completed_release_is_idempotent(self):
        self.assertEqual(self.run_release(), 0, self.errors)
        self.assertEqual(self.run_release('--resume'), 0, self.errors)
        creates = [c for c in self.calls if is_publication(c)]
        self.assertEqual(len(creates), 1)

    def test_uncertain_publication_response_resumes_without_duplicate(self):
        original = self.runner

        def lost_response(command, **kwargs):
            result = original(command, **kwargs)
            if is_publication(command):
                return subprocess.CompletedProcess(command, 0, 'invalid JSON response', '')
            return result

        self.runner = lost_response
        self.assertEqual(self.run_release(), 1)
        self.assertIn('--resume', '\n'.join(self.errors))
        self.runner = original
        self.assertEqual(self.run_release('--resume'), 0, self.errors)
        self.assertEqual(len([c for c in self.calls if is_publication(c)]), 1)

    def test_resume_rejects_moved_remote_tag(self):
        self.failure = lambda c: is_publication(c)
        self.assertEqual(self.run_release(), 1)
        self.git('update-ref', 'refs/tags/v0.6.0', self.original_head, root=self.remote)
        self.failure = None
        self.assertEqual(self.run_release('--resume'), 1)
        self.assertIn('different commit', '\n'.join(self.errors))
        self.assertEqual(self.git('rev-parse', 'v0.6.0', root=self.remote), self.original_head)

    def test_resume_requires_a_version_only_release_commit(self):
        self.assertEqual(self.run_release('--resume'), 1)
        self.assert_untouched()

    def test_major_and_resume_are_mutually_exclusive(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.run_release('--major', '--resume')
        self.assertEqual(raised.exception.code, 2)
        self.assert_untouched()


if __name__ == '__main__':
    unittest.main()
