"""Explicit local Git ownership and commits; no hooks, remotes or reset."""
from pathlib import Path
import os
import subprocess
from .safety import WikiError


def run(repo, *args, check=True):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, env=env)
    if check and result.returncode:
        raise WikiError(f'git {args[0]} failed in {repo}: {result.stderr.strip()}')
    return result.stdout


def owner(path, root):
    existing = path if path.is_dir() else path.parent
    while not existing.exists():
        existing = existing.parent
    repo = Path(run(existing, 'rev-parse', '--show-toplevel').strip()).resolve()
    if not repo.is_relative_to(root):
        raise WikiError('owning Git repository is outside wiki root')
    if Path(run(root, 'rev-parse', '--show-toplevel').strip()).resolve() != root:
        raise WikiError('write root must be the top of its own Git repository')
    return repo


def ancestors(repo, root):
    """Nearest owner through registered submodules to wiki root."""
    links = []
    while repo != root:
        parent = Path(run(repo.parent, 'rev-parse', '--show-toplevel').strip()).resolve()
        if parent == repo or not parent.is_relative_to(root):
            raise WikiError('invalid Git ownership chain')
        link = repo.relative_to(parent).as_posix()
        stage = run(parent, 'ls-files', '--stage', '--', link).strip()
        if not stage.startswith('160000 '):
            raise WikiError(f'nested repo is not a registered submodule: {repo}')
        links.append((parent, link, repo))
        repo = parent
    return links


def dirty(repo):
    entries = run(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all').split('\0')
    result = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        result.append((entry[:2], entry[3:]))
        if 'R' in entry[:2] or 'C' in entry[:2]:
            result.append(('rename', entries[i]))
            i += 1
    return result


def clean_except(repo, allowed=()):
    conflicts = [p for status, p in dirty(repo) if p not in allowed or status == 'rename']
    if conflicts:
        raise WikiError(f'dirty/staged repository {repo}: {", ".join(conflicts)}')


def commit(repo, paths, operation_id):
    paths = sorted(set(paths))
    if not paths:
        return None
    run(repo, 'add', '--', *paths)
    if not run(repo, 'diff', '--cached', '--name-only', '--', *paths).strip():
        return None
    # Suppress repository hooks and signing: writes cannot run user publishing scripts.
    # The 'wiki: OPERATION' subject is a stable data protocol: retries recognize their
    # own commits by it and the documented rollback recipe selects commits with it.
    run(repo, '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
        'commit', '-qm', f'wiki: {operation_id}', '--', *paths)
    return run(repo, 'rev-parse', 'HEAD').strip()
