"""Explicit local Git ownership and commits; no hooks, remotes or reset."""
from pathlib import Path
import os
import subprocess
from .safety import WikiError


def run(repo, *args, check=True, input=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, env=env,
                            **(dict(input=input) if input is not None else dict(text=True)))
    if input is not None:
        result.stdout, result.stderr = result.stdout.decode(), result.stderr.decode()
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


def stage_verified(repo, relative, data):
    """Stage exactly DATA at RELATIVE, never rereading the working file."""
    oid = run(repo, 'hash-object', '-w', '--stdin', '--path', relative, input=data).strip()
    entry = run(repo, 'ls-files', '--stage', '--', relative).split()
    mode = entry[0] if entry and entry[0] in ('100644', '100755') else '100644'
    run(repo, 'update-index', '--add', '--cacheinfo', f'{mode},{oid},{relative}')


def commit(repo, paths, operation_id, verified=None):
    """Commit PATHS. VERIFIED maps a path to the exact bytes to commit for it (journaled, hash-checked):
    those are staged from the bytes, so a later edit of the working file cannot enter the commit; it
    stays a local modification."""
    paths = sorted(set(paths))
    verified = verified or {}
    if not paths:
        return None
    for relative, data in verified.items():
        stage_verified(repo, relative, data)
    rest = [p for p in paths if p not in verified]
    if rest:
        run(repo, 'add', '--', *rest)
    if not run(repo, 'diff', '--cached', '--name-only', '--', *paths).strip():
        return None
    # Suppress repository hooks and signing: writes cannot run user publishing scripts.
    # The 'wiki: OPERATION' subject is a stable data protocol: retries recognize their
    # own commits by it and the documented rollback recipe selects commits with it.
    # The index was verified clean apart from these paths, so commit the index itself
    # (a pathspec commit would reread the working files).
    run(repo, '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
        'commit', '-qm', f'wiki: {operation_id}')
    return run(repo, 'rev-parse', 'HEAD').strip()


def commit_move(repo, old, new, operation_id):
    """Commit a staged `git mv` of OLD to NEW (repo-relative) and nothing else."""
    run(repo, 'add', '-A', '--', new)
    if not run(repo, 'diff', '--cached', '--name-only', '--', old, new).strip():
        return None
    run(repo, '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
        'commit', '-qm', f'wiki: {operation_id}', '--', old, new)
    return run(repo, 'rev-parse', 'HEAD').strip()
