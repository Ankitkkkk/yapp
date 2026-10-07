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
from urllib.error import HTTPError

SCRIPT = Path(__file__).resolve().parents[1] / 'release.py'


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
        workflow = self.repo / '.github/workflows/release.yml'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('name: Publish release\n')
        self.git('add', 'VERSION', '.github/workflows/release.yml')
        self.git('commit', '-m', 'Existing TUI improvements')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', 'origin', 'main')
        self.original_head = self.git('rev-parse', 'HEAD')
        self.calls = []
        self.releases = []
        self.requests = []
        self.api_failure = None
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
        if command[0] != 'git':
            raise FileNotFoundError(command[0])
        kwargs['env'] = self.env
        return subprocess.run(command, **kwargs)

    def opener(self, request, timeout):
        self.requests.append(request)
        if self.api_failure:
            return self.api_failure(request)
        self.assertEqual(request.full_url,
                         'https://api.github.com/repos/Ankitkkkk/yapp/releases?per_page=100&page=1')
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.get_header('Authorization'))
        return io.BytesIO(json.dumps(self.releases).encode())

    def run_release(self, *args):
        return self.release.main(list(args), root=self.repo, runner=self.runner,
                                 opener=self.opener, output=self.out.append,
                                 error_output=self.errors.append)

    def assert_untouched(self):
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.original_head)
        self.assertEqual((self.repo / 'VERSION').read_text(), '0.5.7\n')
        self.assertFalse(any(r.get_method() == 'POST' for r in self.requests))

    def test_default_release_pushes_matching_tag_without_gh_or_local_token(self):
        self.assertEqual(self.run_release(), 0, self.errors)
        self.assertEqual((self.repo / 'VERSION').read_text(), '0.6.0\n')
        head = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.git('rev-parse', 'main', root=self.remote), head)
        self.assertEqual(self.git('rev-parse', 'v0.6.0^{}', root=self.remote), head)
        self.assertEqual(self.git('diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD'), 'VERSION')
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertIn('GitHub Actions', '\n'.join(self.out))
        self.assertIn('actions/workflows/release.yml', '\n'.join(self.out))
        self.assertFalse(any(r.get_method() == 'POST' for r in self.requests))

    def test_major_release_pushes_one_zero_zero(self):
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

    def test_ssh_failure_is_reported_before_version_changes(self):
        self.failure = lambda c: c[:2] == ['git', 'ls-remote']
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()

    def test_release_lookup_failure_is_not_treated_as_missing_release(self):
        def unavailable(request):
            raise HTTPError(request.full_url, 403, 'rate limited', {}, None)

        self.api_failure = unavailable
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()
        self.assertIn('403', '\n'.join(self.errors))

    def test_malformed_github_lookup_does_not_bump_version(self):
        self.api_failure = lambda request: io.BytesIO(b'{"message":"unexpected"}')
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()

    def test_newer_published_release_is_not_replaced_as_latest(self):
        self.releases.append({'tag_name': 'v2.0.0', 'draft': False,
                              'prerelease': False, 'html_url': 'https://github.com/Ankitkkkk/yapp/releases/tag/v2.0.0'})
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()

    def test_missing_workflow_stops_before_bumping(self):
        self.git('rm', '.github/workflows/release.yml')
        self.git('commit', '-m', 'Remove publisher')
        self.git('push', 'origin', 'main')
        self.original_head = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.run_release(), 1)
        self.assert_untouched()
        self.assertIn('release.yml', '\n'.join(self.errors))

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
        self.assertFalse(any(r.get_method() == 'POST' for r in self.requests))
        self.failure = None
        self.assertEqual(self.run_release('--resume'), 1)
        self.assertIn('Reconcile main', '\n'.join(self.errors))

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

    def test_resume_after_push_reports_actions_retry_without_another_push(self):
        self.assertEqual(self.run_release(), 0, self.errors)
        head = self.git('rev-parse', 'HEAD')
        next_commit = self.advance_remote(head)
        pushes = len([c for c in self.calls if c[:2] == ['git', 'push']])
        self.assertEqual(self.run_release('--resume'), 0, self.errors)
        self.assertEqual(self.git('rev-parse', 'main', root=self.remote), next_commit)
        self.assertEqual(self.git('rev-parse', 'v0.6.0^{}', root=self.remote), head)
        self.assertEqual(len([c for c in self.calls if c[:2] == ['git', 'push']]), pushes)
        self.assertIn('Re-run', '\n'.join(self.out))

    def test_resume_of_completed_release_is_idempotent(self):
        self.assertEqual(self.run_release(), 0, self.errors)
        self.releases.append({'tag_name': 'v0.6.0', 'draft': False, 'prerelease': False})
        self.assertEqual(self.run_release('--resume'), 0, self.errors)
        self.assertIn('Already published', '\n'.join(self.out))
        self.assertEqual(len([c for c in self.calls if c[:2] == ['git', 'push']]), 1)

    def test_resume_rejects_moved_remote_tag(self):
        self.assertEqual(self.run_release(), 0, self.errors)
        self.git('update-ref', 'refs/tags/v0.6.0', self.original_head, root=self.remote)
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


class GitHubReleaseAPITests(unittest.TestCase):
    def setUp(self):
        self.release = importlib.import_module('release')

    def test_public_lookup_follows_pages_without_sending_credentials(self):
        calls = []
        first = [{'tag_name': 'v0.5.0', 'draft': False, 'prerelease': False}] * 100
        last = [{'tag_name': 'v0.4.0', 'draft': False, 'prerelease': False}]

        def opener(request, timeout):
            calls.append(request.full_url)
            self.assertIsNone(request.get_header('Authorization'))
            page = first if request.full_url.endswith('page=1') else last
            return io.BytesIO(json.dumps(page).encode())

        releases = self.release.github_releases(self.release.GitHubAPI(opener))
        self.assertEqual(len(releases), 101)
        self.assertEqual(releases[-1]['tag_name'], 'v0.4.0')
        self.assertEqual(calls, [
            'https://api.github.com/repos/Ankitkkkk/yapp/releases?per_page=100&page=1',
            'https://api.github.com/repos/Ankitkkkk/yapp/releases?per_page=100&page=2'])

    def test_api_failures_do_not_expose_tokens_or_response_body(self):
        token = 'test-secret-value'

        def opener(request, timeout):
            raise HTTPError(request.full_url, 403, token, {}, io.BytesIO(token.encode()))

        with self.assertRaises(self.release.ReleaseError) as raised:
            self.release.GitHubAPI(opener, token=token)('releases')
        self.assertIn('403', str(raised.exception))
        self.assertNotIn(token, str(raised.exception))


class ActionsPublicationTests(unittest.TestCase):
    # Share setup/helpers, not inherited test methods.
    setUp = ReleaseWorkflowTests.setUp
    git = ReleaseWorkflowTests.git
    runner = ReleaseWorkflowTests.runner
    advance_remote = ReleaseWorkflowTests.advance_remote

    def prepare_tag(self, version='0.6.0', push=True):
        (self.repo / 'VERSION').write_text(version + '\n')
        self.git('commit', '-am', f'release: v{version}')
        self.git('tag', '-a', f'v{version}', '-m', f'Yapp {version}')
        if push:
            self.git('push', '--atomic', 'origin', 'main', f'v{version}')
        self.git('checkout', '--detach', f'v{version}')

    def opener(self, request, timeout):
        self.requests.append(request)
        self.assertEqual(request.get_header('Authorization'), 'Bearer workflow-test-token')
        if self.api_failure:
            return self.api_failure(request)
        if request.get_method() == 'GET':
            self.assertEqual(request.full_url,
                             'https://api.github.com/repos/Ankitkkkk/yapp/releases?per_page=100&page=1')
            return io.BytesIO(json.dumps(self.releases).encode())
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.full_url, 'https://api.github.com/repos/Ankitkkkk/yapp/releases')
        fields = json.loads(request.data)
        self.assertEqual(fields, {
            'tag_name': 'v0.6.0', 'target_commitish': self.git('rev-parse', 'HEAD'),
            'name': 'Yapp 0.6.0', 'generate_release_notes': True,
            'draft': False, 'prerelease': False, 'make_latest': 'legacy'})
        self.assertEqual(self.git('show', 'v0.6.0:VERSION', root=self.remote), '0.6.0')
        record = {'tag_name': 'v0.6.0', 'draft': False, 'prerelease': False}
        self.releases.append(record)
        return io.BytesIO(json.dumps(record).encode())

    def run_publisher(self, tag='v0.6.0', **env):
        environment = dict(GITHUB_ACTIONS='true', GITHUB_REPOSITORY='Ankitkkkk/yapp',
                           GITHUB_TOKEN='workflow-test-token')
        environment.update(env)
        return self.release.main(['--publish-tag', tag], root=self.repo, runner=self.runner,
                                 opener=self.opener, environment=environment,
                                 output=self.out.append, error_output=self.errors.append)

    def test_tag_publication_uses_workflow_token_and_exact_commit_without_gh(self):
        self.prepare_tag()
        self.assertEqual(self.run_publisher(), 0, self.errors)
        self.assertEqual(len(self.releases), 1)
        self.assertIn('Published:', '\n'.join(self.out))
        self.assertFalse(any(c[:2] in (['git', 'commit'], ['git', 'push']) for c in self.calls))

    def test_publisher_requires_workflow_token(self):
        self.prepare_tag()
        self.assertEqual(self.run_publisher(GITHUB_TOKEN=''), 1)
        self.assertEqual(self.requests, [])

    def test_publisher_rejects_fork_or_local_invocation(self):
        self.prepare_tag()
        for env in ({'GITHUB_REPOSITORY': 'someone/fork'}, {'GITHUB_ACTIONS': ''}):
            with self.subTest(env=env):
                self.assertEqual(self.run_publisher(**env), 1)
                self.assertEqual(self.requests, [])

    def test_mismatched_or_invalid_tag_is_never_published(self):
        self.prepare_tag()
        for tag in ('v0.7.0', 'v0.6.0-beta', 'main', 'v01.2.3', '../../main'):
            with self.subTest(tag=tag):
                self.assertEqual(self.run_publisher(tag), 1)
                self.assertEqual(self.releases, [])

    def test_absent_or_moved_remote_tag_is_never_published(self):
        self.prepare_tag()
        self.git('update-ref', 'refs/tags/v0.6.0', self.original_head, root=self.remote)
        self.assertEqual(self.run_publisher(), 1)
        self.git('tag', '-d', 'v0.6.0', root=self.remote)
        self.assertEqual(self.run_publisher(), 1)
        self.assertEqual(self.releases, [])

    def test_tag_outside_main_is_not_published(self):
        self.git('switch', '-c', 'unmerged')
        self.prepare_tag(push=False)
        self.git('push', 'origin', 'v0.6.0')
        self.assertEqual(self.run_publisher(), 1)
        self.assertEqual(self.releases, [])

    def test_newer_published_version_blocks_older_publication(self):
        self.prepare_tag()
        self.releases.append({'tag_name': 'v1.0.0', 'draft': False, 'prerelease': False})
        self.assertEqual(self.run_publisher(), 1)
        self.assertFalse(any(r.get_method() == 'POST' for r in self.requests))

    def test_rerunning_completed_workflow_is_idempotent(self):
        self.prepare_tag()
        self.assertEqual(self.run_publisher(), 0, self.errors)
        self.assertEqual(self.run_publisher(), 0, self.errors)
        self.assertEqual(len(self.releases), 1)
        self.assertIn('Already published', '\n'.join(self.out))

    def test_draft_or_prerelease_is_left_unchanged(self):
        self.prepare_tag()
        for draft, prerelease in ((True, False), (False, True)):
            with self.subTest(draft=draft):
                self.releases[:] = [{'tag_name': 'v0.6.0', 'draft': draft, 'prerelease': prerelease}]
                self.assertEqual(self.run_publisher(), 1)
                self.assertFalse(any(r.get_method() == 'POST' for r in self.requests))

    def test_failed_publication_can_retry_after_main_advances(self):
        self.prepare_tag()
        original = self.opener

        def fail_post(request, timeout):
            if request.get_method() == 'POST':
                raise HTTPError(request.full_url, 500, 'unavailable', {}, None)
            return original(request, timeout)

        self.opener = fail_post
        self.assertEqual(self.run_publisher(), 1)
        head = self.git('rev-parse', 'HEAD')
        later = self.advance_remote(head)
        self.opener = original
        self.assertEqual(self.run_publisher(), 0, self.errors)
        self.assertEqual(self.git('rev-parse', 'main', root=self.remote), later)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)

    def test_uncertain_response_retries_without_duplicate_publication(self):
        self.prepare_tag()
        original = self.opener

        def lose_response(request, timeout):
            result = original(request, timeout)
            return io.BytesIO(b'not json') if request.get_method() == 'POST' else result

        self.opener = lose_response
        self.assertEqual(self.run_publisher(), 1)
        self.opener = original
        self.assertEqual(self.run_publisher(), 0, self.errors)
        self.assertEqual(len(self.releases), 1)


if __name__ == '__main__':
    unittest.main()
