"""Explicit local Git ownership and commits; no hooks, remotes or history rewriting."""
from pathlib import Path
import os
import subprocess
import tempfile
from .safety import WikiError


def run(repo, *args, check=True, input=None, index=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    if index is not None:
        env['GIT_INDEX_FILE'] = str(index)  # A private index: the shared one is never consulted.
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


def blob(repo, spec):
    """The exact bytes of a Git object, e.g. COMMIT:path (never filtered)."""
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(repo), 'cat-file', 'blob', spec], capture_output=True, env=env)
    if result.returncode:
        raise WikiError(f'git cat-file failed in {repo}: {result.stderr.decode().strip()}')
    return result.stdout


def entry_mode(repo, relative, index=None):
    entry = run(repo, 'ls-files', '--stage', '--', relative, index=index).split()
    return entry[0] if entry and entry[0] in ('100644', '100755') else '100644'


def stage_entry(repo, index, relative, data):
    """Put exactly DATA at RELATIVE into the private INDEX (blob written without any Git filter)."""
    oid = run(repo, 'hash-object', '-w', '--no-filters', '--stdin', input=data).strip()
    run(repo, 'update-index', '--add', '--cacheinfo', f'{entry_mode(repo, relative, index)},{oid},{relative}',
        index=index)


def commit(repo, paths, operation_id, verified=None):
    """Commit PATHS without ever using the shared index.

    The tree is built in a private index read from HEAD; VERIFIED maps a path to the exact journaled
    bytes for it, other files are taken from the working tree and submodules as their current HEAD
    (gitlink). A concurrent `git add` or an edit of the working file can therefore never enter the
    commit. The branch then advances by compare-and-swap, and the real index is made to agree for
    ONLY these paths: every other staged or unstaged change of the user is left as it was."""
    paths = sorted(set(paths))
    verified = verified or {}
    if not paths:
        return None
    old = run(repo, 'rev-parse', 'HEAD').strip()
    with tempfile.TemporaryDirectory(prefix='pisar-index-') as scratch:
        index = Path(scratch) / 'index'
        run(repo, 'read-tree', old, index=index)
        for relative in paths:
            target = repo / relative
            if relative in verified:
                stage_entry(repo, index, relative, verified[relative])
            elif (target / '.git').exists():
                child = run(target, 'rev-parse', 'HEAD').strip()
                run(repo, 'update-index', '--add', '--cacheinfo', f'160000,{child},{relative}', index=index)
            elif target.is_file() and not target.is_symlink():
                stage_entry(repo, index, relative, target.read_bytes())
            else:
                run(repo, 'update-index', '--force-remove', '--', relative, index=index)
        tree = run(repo, 'write-tree', index=index).strip()
        if tree == run(repo, 'rev-parse', f'{old}^{{tree}}').strip():
            return None
        # Hooks and signing stay off: writes cannot run user publishing scripts. The 'wiki: OPERATION'
        # subject is a stable data protocol: retries recognize their own commits by it and the
        # documented rollback recipe selects commits with it.
        new = run(repo, '-c', 'commit.gpgsign=false', 'commit-tree', tree, '-p', old,
                  '-m', f'wiki: {operation_id}').strip()
        for relative, data in verified.items():
            if blob(repo, f'{new}:{relative}') != data:
                raise WikiError(f'committed bytes of {relative} differ from the journal in {repo}')
        run(repo, 'update-ref', '-m', f'wiki: {operation_id}', 'HEAD', new, old)
    run(repo, 'reset', '-q', '--', *paths, check=False)
    return new


def commit_move(repo, old, new, operation_id):
    """Commit a staged `git mv` of OLD to NEW (repo-relative) and nothing else."""
    run(repo, 'add', '-A', '--', new)
    if not run(repo, 'diff', '--cached', '--name-only', '--', old, new).strip():
        return None
    run(repo, '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
        'commit', '-qm', f'wiki: {operation_id}', '--', old, new)
    return run(repo, 'rev-parse', 'HEAD').strip()
