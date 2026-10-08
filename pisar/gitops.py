"""Explicit local Git ownership and commits; no hooks, remotes or history rewriting."""
from pathlib import Path
import os
import subprocess
import tempfile
from .safety import WikiError


# Every Git call of the write paths runs with this configuration, centrally and for all callers:
# no repository hook (writes cannot run publishing scripts), no signing prompt, no fsmonitor program.
HARDENED = ('-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', '-c', 'core.fsmonitor=false')


def git_command(repo, *args):
    return ['git', '-C', str(repo), *HARDENED, *args]


def environment():
    return {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}


def run(repo, *args, check=True, input=None, index=None):
    env = environment()
    if index is not None:
        env['GIT_INDEX_FILE'] = str(index)  # A private index: the shared one is never consulted.
    result = subprocess.run(git_command(repo, *args), capture_output=True, env=env,
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
    result = subprocess.run(git_command(repo, 'cat-file', 'blob', spec), capture_output=True, env=environment())
    if result.returncode:
        raise WikiError(f'git cat-file failed in {repo}: {result.stderr.decode().strip()}')
    return result.stdout


def is_ancestor(repo, ancestor, descendant):
    return subprocess.run(git_command(repo, 'merge-base', '--is-ancestor', ancestor, descendant),
                          capture_output=True, env=environment()).returncode == 0


def attribute_problems(repo, relative):
    """Why Git would not store the bytes of RELATIVE unchanged (empty when it would)."""
    found = {}
    for line in run(repo, 'check-attr', 'text', 'eol', 'filter', 'ident', 'working-tree-encoding',
                    '--', relative).splitlines():
        _, name, value = line.split(': ', 2)
        found[name] = value
    problems = []
    if found.get('text') not in ('unspecified', 'unset'):
        problems.append(f'text={found["text"]}')
    if found.get('text') != 'unset' and found.get('eol', 'unspecified') != 'unspecified':  # -text ignores eol
        problems.append(f'eol={found["eol"]}')
    if found.get('filter') not in ('unspecified', 'unset'):
        problems.append(f'filter={found["filter"]}')
    if found.get('ident') == 'set':
        problems.append('ident')
    if found.get('working-tree-encoding', 'unspecified') != 'unspecified':
        problems.append(f'working-tree-encoding={found["working-tree-encoding"]}')
    if found.get('text') == 'unspecified':
        # `input` is not a boolean; every true spelling (yes, on, 1, true) is normalised by Git itself.
        raw = run(repo, 'config', '--get', 'core.autocrlf', check=False).strip().lower()
        if raw == 'input' or run(repo, 'config', '--type=bool', '--get', 'core.autocrlf', check=False).strip() == 'true':
            problems.append(f'core.autocrlf={raw}')
    return problems


def entry_mode(repo, relative, index=None):
    entry = run(repo, 'ls-files', '--stage', '--', relative, index=index).split()
    return entry[0] if entry and entry[0] in ('100644', '100755') else '100644'


def stage_entry(repo, index, relative, data):
    """Put exactly DATA at RELATIVE into the private INDEX (blob written without any Git filter)."""
    oid = run(repo, 'hash-object', '-w', '--no-filters', '--stdin', input=data).strip()
    run(repo, 'update-index', '--add', '--cacheinfo', f'{entry_mode(repo, relative, index)},{oid},{relative}',
        index=index)


def commit(repo, paths, operation_id, verified=None, pins=None):
    """Commit PATHS without ever using the shared index.

    The tree is built in a private index read from HEAD; VERIFIED maps a path to the exact journaled
    bytes for it. A submodule gitlink is PINNED: PINS maps its path to the child commit recorded by
    the operation (None leaves the entry unchanged); without PINS the child's current HEAD is used.
    Other files are taken from the working tree. A concurrent `git add` or an edit of the working file can therefore never enter the
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
                child = run(target, 'rev-parse', 'HEAD').strip() if pins is None else pins.get(relative)
                if child is None:
                    continue
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
        new = run(repo, 'commit-tree', tree, '-p', old,
                  '-m', f'wiki: {operation_id}').strip()
        for relative, data in verified.items():
            if blob(repo, f'{new}:{relative}') != data:
                raise WikiError(f'committed bytes of {relative} differ from the journal in {repo}')
        run(repo, 'update-ref', '-m', f'wiki: {operation_id}', 'HEAD', new, old)
    run(repo, 'reset', '-q', '--', *paths, check=False)
    return new


def tree_entries(repo, revision, prefix):
    """(mode, oid, path) of every committed entry (gitlinks included) under PREFIX."""
    out = run(repo, 'ls-tree', '-r', '-z', revision, '--', prefix)
    entries = []
    for record in filter(None, out.split('\0')):
        meta, path = record.split('\t', 1)
        mode, _, oid = meta.split()
        entries.append((mode, oid, path))
    return entries


def commit_move(repo, old, new, operation_id, replace=None):
    """Commit the rename of OLD to NEW (repo-relative) by blob identity, and nothing else.

    The moved tree is never re-staged through Git attributes or filters: in a private index read
    from HEAD every committed entry under OLD (blobs and gitlinks) reappears under NEW with the SAME
    mode and object id. Only REPLACE (path under NEW -> exact new bytes, e.g. the rewritten marker)
    is new content, written without filters. The branch moves by compare-and-swap and the real index
    is synced for these paths only."""
    replace = replace or {}
    head = run(repo, 'rev-parse', 'HEAD').strip()
    entries = tree_entries(repo, head, old)
    if not entries:
        # Already committed by an interrupted earlier run, or nothing to do.
        committed = {path: oid for _, oid, path in tree_entries(repo, head, new)}
        for path, data in replace.items():
            if path not in committed or blob(repo, f'{head}:{path}') != data:
                raise WikiError(f'nothing committed under {old} and {new} is not the intended result in {repo}')
        return None
    moved = {}
    with tempfile.TemporaryDirectory(prefix='pisar-index-') as scratch:
        index = Path(scratch) / 'index'
        run(repo, 'read-tree', head, index=index)
        lines = []
        for mode, oid, path in entries:
            target = new + path[len(old):]
            moved[target] = (mode, oid)
            lines.append(f'0 {"0" * len(oid)}\t{path}\0')
        for target, (mode, oid) in moved.items():
            if target in replace:
                oid = run(repo, 'hash-object', '-w', '--no-filters', '--stdin', input=replace[target]).strip()
                moved[target] = (mode, oid)
            lines.append(f'{moved[target][0]} {moved[target][1]}\t{target}\0')
        for target in replace:
            if target not in moved:
                raise WikiError(f'{target} is not part of the moved tree in {repo}')
        run(repo, 'update-index', '-z', '--index-info', input=''.join(lines).encode(), index=index)
        tree = run(repo, 'write-tree', index=index).strip()
    new_commit = run(repo, 'commit-tree', tree, '-p', head, '-m', f'wiki: {operation_id}').strip()
    # Verify before the branch moves: identical object ids (except the replaced paths) and exact bytes.
    after = {path: (mode, oid) for mode, oid, path in tree_entries(repo, new_commit, new)}
    for target, (mode, oid) in moved.items():
        if after.get(target) != (mode, oid):
            raise WikiError(f'moved entry {target} differs from its blob identity in {repo}')
    for target, data in replace.items():
        if blob(repo, f'{new_commit}:{target}') != data:
            raise WikiError(f'committed bytes of {target} differ from the intended ones in {repo}')
    if tree_entries(repo, new_commit, old):
        raise WikiError(f'{old} still holds entries after the move in {repo}')
    run(repo, 'update-ref', '-m', f'wiki: {operation_id}', 'HEAD', new_commit, head)
    run(repo, 'reset', '-q', '--', old, new, check=False)
    return new_commit
