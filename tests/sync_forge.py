"""Hand-built Git objects for hostile-remote tests.

A remote can send trees that a checkout never creates: names `..`, `.git`, `.GIT`, backslashes, duplicate entries,
symbolic links at any depth. These helpers write such objects into a repository (objects only, no ref moves), so a
test can publish them exactly as a hostile remote would. Nothing built here is ever checked out by the tests.
"""
import subprocess


# A synthetic identity for the objects built here (they are never published by the tests themselves).
IDENTITY = dict(GIT_AUTHOR_NAME='Synthetic fixture', GIT_AUTHOR_EMAIL='synthetic@example.invalid',
                GIT_COMMITTER_NAME='Synthetic fixture', GIT_COMMITTER_EMAIL='synthetic@example.invalid')


def _git(repo, *args, input=None):
    env = {**__import__('os').environ, **IDENTITY}
    return subprocess.run(['git', '-C', str(repo), *args], input=input, capture_output=True, check=True,
                          env=env).stdout


def _blob(repo, data):
    return _git(repo, 'hash-object', '-w', '--stdin', input=data).decode().strip()


def _tree(repo, entries):
    """A tree object of ENTRIES [(name bytes, mode bytes, hex id)], sorted the way Git sorts tree entries."""
    ordered = sorted(entries, key=lambda e: e[0] + (b'/' if e[1] == b'40000' else b''))
    raw = b''.join(mode + b' ' + name + b'\0' + bytes.fromhex(oid) for name, mode, oid in ordered)
    return _git(repo, 'hash-object', '-t', 'tree', '-w', '--literally', '--stdin', input=raw).decode().strip()


def _nested(repo, flat):
    """The tree of FLAT {path: (mode bytes, hex id)}, every '/'-separated component taken literally."""
    children = {}
    for path, (mode, oid) in flat.items():
        head, _, rest = path.partition('/')
        if rest:
            children.setdefault(head, ['dir', {}])[1][rest] = (mode, oid)
        else:
            children[head] = ['leaf', mode, oid]
    entries = []
    for name, node in children.items():
        raw = name.encode('utf-8', 'surrogateescape')
        if node[0] == 'leaf':
            entries.append((raw, node[1], node[2]))
        else:
            entries.append((raw, b'40000', _nested(repo, node[1])))
    return _tree(repo, entries)


def _flat(repo, commit):
    """{path: (mode, oid)} of every entry of COMMIT (blobs, links and gitlinks), paths as stored."""
    flat = {}
    for record in filter(None, _git(repo, 'ls-tree', '-r', '-z', '--full-tree', commit).split(b'\0')):
        meta, path = record.split(b'\t', 1)
        mode, _, oid = meta.decode().split()
        flat[path.decode('utf-8', 'surrogateescape')] = (mode.encode(), oid)
    return flat


def commit(repo, parent, files=None, links=None, raw_root=None, message='forged'):
    """A commit on PARENT that adds FILES {path: bytes}, LINKS {path: target} (symbolic links) and, when
    RAW_ROOT is given, replaces the top level with those raw entries [(name, mode, hex id)] plus the files.

    Paths may contain any name: `..`, `.git`, `.GIT`, backslashes. The commit is never checked out here."""
    flat = {} if raw_root is not None else _flat(repo, parent)
    for path, data in (files or {}).items():
        flat[path] = (b'100644', _blob(repo, data))
    for path, target in (links or {}).items():
        flat[path] = (b'120000', _blob(repo, target.encode()))
    if raw_root is None:
        tree = _nested(repo, flat)
    else:
        top = [(name, mode, oid) for name, mode, oid in raw_root]
        for path, (mode, oid) in flat.items():
            if '/' not in path:
                top.append((path.encode(), mode, oid))
        nested = {p: v for p, v in flat.items() if '/' in p}
        if nested:
            for head in {p.partition('/')[0] for p in nested}:
                sub = {p.partition('/')[2]: v for p, v in nested.items() if p.partition('/')[0] == head}
                top.append((head.encode(), b'40000', _nested(repo, sub)))
        tree = _tree(repo, top)
    args = ['commit-tree', tree, '-m', message]
    if parent:
        args[2:2] = ['-p', parent]
    return _git(repo, *args).decode().strip()


def set_branch(repo, branch, commit_id):
    """Point BRANCH of REPO at COMMIT_ID (the publishing clone's own branch, never the local root's)."""
    _git(repo, 'update-ref', f'refs/heads/{branch}', commit_id)
