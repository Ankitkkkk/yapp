#!/usr/bin/env python3
"""Publish Yapp releases: minor by default, --major for a breaking release.

Use --dry-run to preview without GitHub access or filesystem changes.
Requires a clean main checkout and Git push access (SSH works).
GitHub Actions publishes the pushed tag; no local GitHub CLI or token is needed.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
REPO = 'Ankitkkkk/yapp'
VERSION_PATTERN = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
WORKFLOW = '.github/workflows/release.yml'
ACTIONS_URL = f'https://github.com/{REPO}/actions/workflows/release.yml'


class ReleaseError(Exception):
    """An actionable failure; never automatically reset work or force a push."""


class Commands:
    def __init__(self, root, runner):
        self.root = root
        self.runner = runner

    def __call__(self, *args):
        try:
            result = self.runner(list(args), cwd=self.root, capture_output=True,
                                 text=True, timeout=180)
        except FileNotFoundError as error:
            raise ReleaseError(f'{args[0]} is required. Install it and retry.') from error
        except subprocess.TimeoutExpired as error:
            raise ReleaseError(f'Timed out: {shlex.join(args)}. Check whether it completed before retrying.') from error
        if result.returncode:
            detail = (result.stderr or result.stdout or '').strip()[-2000:]
            raise ReleaseError(f'Failed: {shlex.join(args)}\n{detail}')
        return result.stdout.strip()


def parse_version(value):
    match = VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise ReleaseError('VERSION must contain a numeric MAJOR.MINOR.PATCH version, such as 0.5.0.')
    return tuple(int(part) for part in match.groups())


def validate_origin(commands):
    # Both read and push URLs must address the repository used by Yapp's updater.
    for arguments in (('--all',), ('--push', '--all')):
        urls = commands('git', 'remote', 'get-url', *arguments, 'origin').splitlines()
        allowed = {f'https://github.com/{REPO}', f'git@github.com:{REPO}',
                   f'ssh://git@github.com/{REPO}'}
        if len(urls) != 1 or urls[0].rstrip('/').removesuffix('.git') not in allowed:
            raise ReleaseError(f'origin must have one GitHub URL for {REPO}; check git remote -v.')


def remote_refs(commands, tag):
    output = commands('git', 'ls-remote', 'origin', 'refs/heads/main',
                      f'refs/tags/{tag}', f'refs/tags/{tag}^{{}}')
    return {ref: sha for sha, ref in (line.split() for line in output.splitlines())}


def tag_commit(refs, tag):
    return refs.get(f'refs/tags/{tag}^{{}}', refs.get(f'refs/tags/{tag}'))


class GitHubAPI:
    """Anonymous public checks locally; the Actions token is used only in CI."""

    def __init__(self, opener=urlopen, *, token=''):
        self.opener = opener
        self.token = token

    def __call__(self, path, *, data=None):
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'yapp-release',
                   'X-GitHub-Api-Version': '2022-11-28'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        body = None
        if data is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(data).encode()
        request = Request(f'https://api.github.com/repos/{REPO}/{path}', data=body, headers=headers)
        try:
            with self.opener(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            # Never echo authorization headers or arbitrary API response bodies.
            raise ReleaseError(f'GitHub API returned HTTP {error.code}. Check GitHub access/rate limits '
                               'and retry; a failed lookup is not an empty release list.') from error
        except (URLError, TimeoutError, OSError) as error:
            raise ReleaseError('Could not reach GitHub API. Check your connection and retry.') from error
        except (ValueError, UnicodeError) as error:
            raise ReleaseError('GitHub returned invalid JSON. Check the release on GitHub before retrying.') from error


def github_releases(api):
    releases = []
    page = 1
    while True:
        items = api(f'releases?per_page=100&page={page}')
        if not isinstance(items, list) or any(
                not isinstance(item, dict) or not isinstance(item.get('tag_name'), str)
                or not isinstance(item.get('draft'), bool)
                or not isinstance(item.get('prerelease'), bool) for item in items):
            raise ReleaseError('GitHub returned an unexpected release list; no release was created.')
        releases.extend(items)
        if len(items) < 100:
            return releases
        page += 1


def release_parent(commands, version, head):
    subject = commands('git', 'log', '-1', '--format=%s', head)
    parents = commands('git', 'rev-list', '--parents', '-n', '1', head).split()
    if subject != f'release: v{version}' or len(parents) != 2:
        raise ReleaseError('--resume requires HEAD to be the version-only release commit.')
    changed = commands('git', 'diff', '--name-only', parents[1], head).splitlines()
    if changed != ['VERSION'] or commands('git', 'show', f'{head}:VERSION') != version:
        raise ReleaseError('Release commit must change only VERSION and match its release tag.')
    return parents[1]


def require_newer_release(releases, version):
    for item in releases:
        match = VERSION_PATTERN.fullmatch(item['tag_name'].removeprefix('v'))
        if match and not item['draft'] and not item['prerelease']:
            if tuple(int(part) for part in match.groups()) >= parse_version(version):
                raise ReleaseError('A newer or equal release is already published. Update main before releasing.')


def publish(root, current, version, *, resume, commands, api, output):
    if commands('git', 'branch', '--show-current') != 'main':
        raise ReleaseError('Run releases from main, after merging and verifying your changes.')
    if commands('git', 'status', '--porcelain', '--untracked-files=all'):
        raise ReleaseError('A clean working tree is required. Commit or set aside local changes first.')
    validate_origin(commands)
    head = commands('git', 'rev-parse', 'HEAD')
    # Fail before writing VERSION if commit identity or the publisher is missing.
    commands('git', 'var', 'GIT_AUTHOR_IDENT')
    commands('git', 'var', 'GIT_COMMITTER_IDENT')
    try:
        commands('git', 'cat-file', '-e', f'{head}:{WORKFLOW}')
    except ReleaseError as error:
        raise ReleaseError(f'Commit and push {WORKFLOW} before releasing; GitHub Actions publishes the tag.') from error
    releases = github_releases(api)
    tag = f'v{version}'
    existing = next((item for item in releases if item['tag_name'] == tag), None)
    refs = remote_refs(commands, tag)
    remote_main = refs.get('refs/heads/main')
    remote_tag = tag_commit(refs, tag)
    local_tag = commands('git', 'tag', '--list', tag)
    parent = release_parent(commands, version, head) if resume else head

    if resume:
        if local_tag and commands('git', 'rev-parse', f'{tag}^{{commit}}') != head:
            raise ReleaseError(f'Local {tag} points to a different commit; it was left unchanged.')
        if remote_tag and remote_tag != head:
            raise ReleaseError(f'Remote {tag} points to a different commit; it was left unchanged.')
        if existing:
            if existing['draft'] or existing['prerelease']:
                raise ReleaseError(f'{tag} already exists as a draft or prerelease. Finish it on GitHub.')
            if remote_tag != head:
                raise ReleaseError(f'{tag} is published but its remote tag does not match HEAD.')
            output(f'Already published: https://github.com/{REPO}/releases/tag/{tag}')
            return
        if not remote_tag and remote_main not in (parent, head):
            raise ReleaseError('origin/main moved before the release tag was pushed. Reconcile main before retrying.')
    else:
        pending = commands('git', 'log', '-1', '--format=%s') == f'release: v{current}'
        published_current = any(item['tag_name'] == f'v{current}' and not item['draft']
                                and not item['prerelease'] for item in releases)
        if pending and not published_current:
            raise ReleaseError('The previous release is unfinished. Run python3 release.py --resume.')
        if remote_main != head:
            raise ReleaseError('Local main must match origin/main. Pull or push your changes before releasing.')
        if existing or local_tag or remote_tag:
            raise ReleaseError(f'{tag} already exists. No version or tag was changed.')

    # An old checkout must not replace a newer public release as "latest".
    require_newer_release(releases, version)

    try:
        if not resume:
            (root / 'VERSION').write_text(version + '\n')
            commands('git', 'commit', '--only', '-m', f'release: {tag}', '--', 'VERSION')
            head = commands('git', 'rev-parse', 'HEAD')
            if release_parent(commands, version, head) != parent:
                raise ReleaseError('HEAD changed unexpectedly during the version commit.')
        if commands('git', 'status', '--porcelain', '--untracked-files=all'):
            raise ReleaseError('The working tree changed during release preparation. Inspect it before continuing.')
        if not local_tag:
            commands('git', 'tag', '-a', tag, '-m', f'Yapp {version}', head)
        if not remote_tag:
            targets = [f'refs/tags/{tag}:refs/tags/{tag}']
            if remote_main != head:
                targets.insert(0, f'{head}:refs/heads/main')
            output(f'Pushing {tag} to origin (atomic branch/tag update).')
            commands('git', 'push', '--atomic', '--no-follow-tags', 'origin', *targets)
        if tag_commit(remote_refs(commands, tag), tag) != head:
            raise ReleaseError('The pushed release tag does not match the verified release commit.')
        output(f'Tag {tag} is pushed. GitHub Actions publishes the release asynchronously.')
        output(f'Check the Publish release workflow: {ACTIONS_URL}')
        if remote_tag:
            output('An existing tag push does not restart Actions. Re-run the failed job, '
                   f'or use Run workflow on main with tag {tag}.')
        output('After the workflow succeeds, users can install it with: yapp update')
    except (ReleaseError, OSError) as error:
        raise ReleaseError(f'{error}\nRelease stopped; files and refs were left for inspection. '
                           'Once the version-only release commit is clean, run python3 release.py --resume '
                           'to finish this version. Do not bump it again.') from error


def publish_tag(root, tag, *, commands, api, output):
    """Run on the tagged checkout in Actions, including safe workflow retries."""
    if not tag.startswith('v'):
        raise ReleaseError('Release tags must use vMAJOR.MINOR.PATCH.')
    version = tag[1:]
    parse_version(version)
    if (root / 'VERSION').read_text().strip() != version:
        raise ReleaseError('The release tag must match VERSION in the tagged checkout.')
    validate_origin(commands)
    head = commands('git', 'rev-parse', 'HEAD')
    if tag_commit(remote_refs(commands, tag), tag) != head:
        raise ReleaseError('The remote release tag must exist and match the checked-out commit.')
    commands('git', 'fetch', '--no-tags', 'origin', 'main')
    try:
        commands('git', 'merge-base', '--is-ancestor', head, 'FETCH_HEAD')
    except ReleaseError as error:
        raise ReleaseError('The release tag must point to a commit on origin/main.') from error
    releases = github_releases(api)
    existing = next((item for item in releases if item['tag_name'] == tag), None)
    if existing:
        if existing['draft'] or existing['prerelease']:
            raise ReleaseError(f'{tag} already exists as a draft or prerelease. Finish it on GitHub.')
        output(f'Already published: https://github.com/{REPO}/releases/tag/{tag}')
        return
    require_newer_release(releases, version)
    # Pin the commit for the API's absent-tag fallback. Automatic latest
    # selection avoids forcing an older release over a concurrent newer one.
    created = api('releases', data={
        'tag_name': tag, 'target_commitish': head, 'name': f'Yapp {version}',
        'generate_release_notes': True, 'draft': False, 'prerelease': False,
        'make_latest': 'legacy',
    })
    if (not isinstance(created, dict) or created.get('tag_name') != tag
            or created.get('draft') is not False or created.get('prerelease') is not False):
        raise ReleaseError('GitHub did not confirm publication. Inspect GitHub and re-run this workflow.')
    output(f'Published: https://github.com/{REPO}/releases/tag/{tag}')
    output('Users can install it with: yapp update')


def main(argv=None, *, root=ROOT, runner=subprocess.run, opener=urlopen, environment=None, output=print,
         error_output=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--major', action='store_true', help='Bump MAJOR and reset MINOR/PATCH; default bumps MINOR')
    mode.add_argument('--resume', action='store_true', help='Finish publishing the current version-only release commit')
    mode.add_argument('--publish-tag', metavar='TAG', help='GitHub Actions only: publish an already-pushed tag')
    parser.add_argument('--dry-run', action='store_true', help='Show the plan only; no Git/GitHub checks or changes')
    args = parser.parse_args(argv)
    error_output = error_output or (lambda text: print(text, file=sys.stderr))
    root = Path(root)
    try:
        if args.publish_tag:
            if args.dry_run:
                raise ReleaseError('--publish-tag cannot be combined with --dry-run.')
            env = os.environ if environment is None else environment
            if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_REPOSITORY') != REPO
                    or not env.get('GITHUB_TOKEN')):
                raise ReleaseError('--publish-tag runs only in this repository\'s GitHub Actions workflow '
                                   'with its automatic GITHUB_TOKEN. Locally, run python3 release.py.')
            publish_tag(root, args.publish_tag, commands=Commands(root, runner),
                        api=GitHubAPI(opener, token=env['GITHUB_TOKEN']), output=output)
            return 0
        current = (root / 'VERSION').read_text().strip()
        major, minor, _patch = parse_version(current)
        version = current if args.resume else (f'{major + 1}.0.0' if args.major else f'{major}.{minor + 1}.0')
        output(f'{"Resume" if args.resume else "Release"}: {current} -> {version} (v{version})')
        if args.dry_run:
            output(f'Plan: {"reuse" if args.resume else "commit"} VERSION, push main and tag v{version} '
                   f'to {REPO}; GitHub Actions publishes a stable release with generated notes.')
            output('Preview only: no files changed; clean main, origin, GitHub access, and tags are not checked.')
            return 0
        publish(root, current, version, resume=args.resume,
                commands=Commands(root, runner), api=GitHubAPI(opener), output=output)
        return 0
    except (ReleaseError, OSError) as error:
        error_output(str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
