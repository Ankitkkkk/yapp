#!/usr/bin/env python3
"""Publish Yapp releases: minor by default, --major for a breaking release.

Use --dry-run to preview without GitHub access or filesystem changes.
Requires a clean main checkout, Git, and an authenticated GitHub CLI (gh).
"""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
REPO = 'Ankitkkkk/yapp'
VERSION_PATTERN = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')


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


def github_releases(commands):
    # Listing distinguishes an empty release list from an auth/network failure.
    raw = commands('gh', 'api', f'repos/{REPO}/releases?per_page=100',
                   '--hostname', 'github.com', '--paginate', '--slurp')
    try:
        pages = json.loads(raw)
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise ValueError('expected pages of releases')
        releases = [release for page in pages for release in page]
        if any(not isinstance(release, dict)
               or not isinstance(release.get('tag_name'), str)
               or not isinstance(release.get('draft'), bool)
               or not isinstance(release.get('prerelease'), bool) for release in releases):
            raise ValueError('invalid release record')
        return releases
    except (ValueError, TypeError) as error:
        raise ReleaseError('GitHub returned an unexpected release list; no release was created.') from error


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


def publish(root, current, version, *, resume, commands, output):
    if commands('git', 'branch', '--show-current') != 'main':
        raise ReleaseError('Run releases from main, after merging and verifying your changes.')
    if commands('git', 'status', '--porcelain', '--untracked-files=all'):
        raise ReleaseError('A clean working tree is required. Commit or set aside local changes first.')
    validate_origin(commands)
    head = commands('git', 'rev-parse', 'HEAD')
    # Fail before writing VERSION if commit identity or GitHub authentication is missing.
    commands('git', 'var', 'GIT_AUTHOR_IDENT')
    commands('git', 'var', 'GIT_COMMITTER_IDENT')
    commands('gh', 'auth', 'status', '--hostname', 'github.com')
    releases = github_releases(commands)
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
        require_newer_release(github_releases(commands), version)
        output(f'Publishing {tag} with generated release notes.')
        # Unlike --latest, legacy lets GitHub consider semantic versions if
        # another release appears between the final check and publication.
        # Pin target_commitish too, so an absent/deleted tag cannot select a
        # different default-branch commit in the API's tag-creation fallback.
        raw = commands('gh', 'api', f'repos/{REPO}/releases', '--hostname', 'github.com',
                       '--method', 'POST', '-f', f'tag_name={tag}', '-f', f'target_commitish={head}',
                       '-f', f'name=Yapp {version}', '-F', 'generate_release_notes=true',
                       '-F', 'draft=false', '-F', 'prerelease=false', '-f', 'make_latest=legacy')
        try:
            created = json.loads(raw)
            if (not isinstance(created, dict) or created.get('tag_name') != tag
                    or created.get('draft') is not False or created.get('prerelease') is not False):
                raise ValueError('release response did not confirm publication')
        except ValueError as error:
            raise ReleaseError('GitHub did not confirm the published release. Check GitHub before retrying.') from error
        output(f'Published: https://github.com/{REPO}/releases/tag/{tag}')
        output('Users can install it with: yapp update')
    except (ReleaseError, OSError) as error:
        raise ReleaseError(f'{error}\nRelease stopped; files and refs were left for inspection. '
                           'Once the version-only release commit is clean, run python3 release.py --resume '
                           'to finish this version. Do not bump it again.') from error


def main(argv=None, *, root=ROOT, runner=subprocess.run, output=print,
         error_output=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--major', action='store_true', help='Bump MAJOR and reset MINOR/PATCH; default bumps MINOR')
    mode.add_argument('--resume', action='store_true', help='Finish publishing the current version-only release commit')
    parser.add_argument('--dry-run', action='store_true', help='Show the plan only; no Git/GitHub checks or changes')
    args = parser.parse_args(argv)
    error_output = error_output or (lambda text: print(text, file=sys.stderr))
    root = Path(root)
    try:
        current = (root / 'VERSION').read_text().strip()
        major, minor, _patch = parse_version(current)
        version = current if args.resume else (f'{major + 1}.0.0' if args.major else f'{major}.{minor + 1}.0')
        output(f'{"Resume" if args.resume else "Release"}: {current} -> {version} (v{version})')
        if args.dry_run:
            output(f'Plan: {"reuse" if args.resume else "commit"} VERSION, push main and tag v{version} '
                   f'to {REPO}, publish a latest GitHub release with generated notes.')
            output('Preview only: no files changed; clean main, origin, GitHub access, and tags are not checked.')
            return 0
        publish(root, current, version, resume=args.resume,
                commands=Commands(root, runner), output=output)
        return 0
    except (ReleaseError, OSError) as error:
        error_output(str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
