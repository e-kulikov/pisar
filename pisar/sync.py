"""`pisar sync`: an explicit, pull-only integration of a Git remote into the knowledge repository.

pisar never pushes. It talks to a remote only in `fetch`, when a sync command is run. Fetched content is
never checked out or merged by Git: its trees are read as objects (`cat-file`, `ls-tree`), merged by the
three-way rules of `merge_trees` (text only, through `merge-file`, which runs no driver), validated as a
whole candidate root (`documents.check`), and only then does the local branch move (compare-and-swap).

Journals and work areas live under the runtime directory of the root: `<state>/roots/<ns>/sync/`.
"""
import json
import os
import stat
import time
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
from . import commit_message, config, documents, gitops, operations, settings
from .operations import Runtime, atomic_bytes, write_json
from .safety import WikiError, concrete_id, safe_path, sha256
from .spaces import Wiki

MIN_GIT = (2, 41)
REF_ROOT = 'refs/pisar/sync'
FORMAT = 1
GITLINK = '160000'
BLOBS = ('100644', '100755')
# Transports a fetch may use; `protocol.allow=never` refuses every other one, `ext::` and helpers included.
PROTOCOLS = ('-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always', '-c', 'protocol.ssh.allow=always',
             '-c', 'protocol.git.allow=always', '-c', 'protocol.file.allow=always')
# Neutral configuration of every sync call: no hook, no monitor, no signing, no attribute file and no end-of-line
# conversion. Together with an attribute source that is the empty tree (see _command), no conversion can run.
HARDENING = ('-c', 'core.attributesFile=/dev/null', '-c', 'core.autocrlf=false', '-c', 'core.hooksPath=/dev/null',
             '-c', 'core.fsmonitor=false', '-c', 'commit.gpgsign=false', '-c', 'core.untrackedCache=false',
             '--no-optional-locks')
# Repository-owned settings that name a program, rewrite a transport or change conversions. A sync refuses any
# repository (its own configuration, and what its own files include) that sets one: nothing may decide which program
# runs (review 20, 2; review 20b, A and B). Keys are matched as Git lists them: the remote name may contain dots.
DANGEROUS_KEY = re.compile(
    r'^(filter\..+\.(clean|smudge|process|required)|diff\..+\.(textconv|command)|'
    r'core\.(fsmonitor|attributesfile|sshcommand|gitproxy)|remote\..+\.(uploadpack|receivepack|vcs|proxy)|'
    r'url\..+\.(insteadof|pushinsteadof)|protocol(\..+)?\.allow)$', re.I)
INCLUDE_KEY = re.compile(r'^include(if\..+)?\.path$', re.I)
# Git's environment for sync: no ambient GIT_* variable, no system configuration or attribute file.
ENVIRONMENT = dict(GIT_CONFIG_NOSYSTEM='1', GIT_ATTR_NOSYSTEM='1', GIT_PAGER='cat', GIT_TERMINAL_PROMPT='0',
                   GIT_LITERAL_PATHSPECS='1', GIT_EDITOR='true')
# Identity of candidate commit objects, which are only read by validation and never published.
SCRATCH_IDENTITY = dict(GIT_AUTHOR_NAME='pisar sync', GIT_AUTHOR_EMAIL='pisar-sync@invalid.invalid',
                        GIT_COMMITTER_NAME='pisar sync', GIT_COMMITTER_EMAIL='pisar-sync@invalid.invalid')
# Attribute names that change how Git stores or converts a file: a fetched .gitattributes naming one is refused.
ATTRIBUTE_NAMES = ('filter', 'merge', 'diff', 'textconv', 'eol', 'text', 'ident', 'working-tree-encoding')
MARKER = re.compile(r'(?m)^(<<<<<<<|>>>>>>>)( |$)')
KINDS = ('content', 'binary', 'add-add', 'delete-modify', 'mode', 'submodule')
# Phases in which the branch has not moved: an abort may drop the sync there (see do_abort).
PRE_MOVE_PHASES = ('prepared', 'planned', 'conflicts')
# Conflicts whose work file carries no markers: only an explicit take decides them.
MARKERLESS = ('binary', 'mode', 'delete-modify', 'submodule')


# --- Git --------------------------------------------------------------------------------------------------

def check_git():
    """Refuse a Git older than MIN_GIT before any sync command runs."""
    text = subprocess.run(['git', '--version'], capture_output=True, text=True).stdout
    match = re.search(r'(\d+)\.(\d+)', text)
    if not match or (int(match[1]), int(match[2])) < MIN_GIT:
        raise WikiError(f'pisar sync needs Git {MIN_GIT[0]}.{MIN_GIT[1]} or newer: {text.strip() or "git not found"}')


_EMPTY_TREES = {}


def empty_tree(repo):
    """The empty tree of REPO's object format (SHA-1 or SHA-256), which is the attribute source of every sync call."""
    if str(repo) not in _EMPTY_TREES:
        _EMPTY_TREES[str(repo)] = subprocess.run(['git', '-C', str(repo), 'hash-object', '-t', 'tree', '--stdin'],
                                                 input=b'', capture_output=True, check=True).stdout.decode().strip()
    return _EMPTY_TREES[str(repo)]


def _command(repo, args, network=False):
    argv = ['git', '-C', str(repo), *HARDENING, *(PROTOCOLS if network else ()), *args]
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(ENVIRONMENT)
    # Attributes come from the empty tree: no filter, driver or conversion named by any file can apply to a sync.
    env['GIT_ATTR_SOURCE'] = empty_tree(repo)
    return argv, env


def git_bytes(repo, *args, input=None, check=True, network=False, index=None, identity=False, dates=None):
    """Raw output of one Git command in REPO; WikiError on failure when CHECK. INDEX names a private index,
    DATES (a Unix time) fixes the author and committer date of a commit object."""
    argv, env = _command(repo, args, network)
    if index is not None:
        env['GIT_INDEX_FILE'] = str(index)
    if identity:
        env.update(SCRATCH_IDENTITY)
    if dates is not None:
        env.update(GIT_AUTHOR_DATE=f'{dates} +0000', GIT_COMMITTER_DATE=f'{dates} +0000')
    result = subprocess.run(argv, input=input, capture_output=True, env=env)
    if check and result.returncode:
        raise WikiError(f'git {args[0]} failed in {repo}: {result.stderr.decode("utf-8", "replace").strip()}')
    return result.stdout if result.returncode == 0 else b''


def git(repo, *args, **options):
    """Text output (surrogateescape) of one Git command; options as in git_bytes."""
    return git_bytes(repo, *args, **options).decode('utf-8', 'surrogateescape')


def _dirty(repo):
    """(status, path) of every dirty, staged or untracked path of REPO alone, read with the neutral configuration.

    The repository is guarded first, and submodules are ignored: their own settings are not read by this status."""
    _guard(repo)
    entries = []
    for record in git_bytes(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--no-renames',
                            '--ignore-submodules=all').split(b'\0'):
        if record:
            entries.append((record[:2].decode(), record[3:].decode('utf-8', 'surrogateescape')))
    return entries


def _clean(repo, allowed=()):
    """Refuse unless the only dirty paths of REPO are ALLOWED (the sync's own planned paths, on a resume)."""
    conflicts = [path for _, path in _dirty(repo) if path not in allowed]
    if conflicts:
        raise WikiError(f'dirty/staged repository {repo}: {", ".join(conflicts)}')


def commit_of(repo, revision):
    """The commit REVISION names, or None."""
    return git(repo, 'rev-parse', '--verify', '-q', f'{revision}^{{commit}}', check=False).strip() or None


def head_commit(repo):
    head = commit_of(repo, 'HEAD')
    if head is None:
        raise WikiError(f'{repo} has no commits yet')
    return head


def symbolic_head(repo):
    return git(repo, 'symbolic-ref', '-q', 'HEAD', check=False).strip()


def is_ancestor(repo, ancestor, descendant):
    argv, env = _command(repo, ('merge-base', '--is-ancestor', ancestor, descendant))
    return subprocess.run(argv, capture_output=True, env=env).returncode == 0


def read_blobs(repo, oids):
    """{oid: raw bytes} of the blobs OIDS, read in one `cat-file --batch` (never filtered)."""
    unique = sorted(set(oids))
    if not unique:
        return {}
    out = git_bytes(repo, 'cat-file', '--batch', input=('\n'.join(unique) + '\n').encode())
    found, position = {}, 0
    for oid in unique:
        end = out.index(b'\n', position)
        header = out[position:end].split()
        position = end + 1
        if len(header) != 3 or header[1] != b'blob':
            raise WikiError(f'object {oid} is not a blob in {repo}')
        size = int(header[2])
        found[oid] = out[position:position + size]
        position += size + 1
    return found


def tree_entries(repo, revision):
    """{path: (mode, oid)} of every blob and gitlink under the commit or tree REVISION (paths decoded)."""
    entries = {}
    for record in filter(None, git_bytes(repo, 'ls-tree', '-r', '-z', '--full-tree', revision).split(b'\0')):
        meta, path = record.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        if kind in ('blob', 'commit'):
            entries[path.decode('utf-8', 'surrogateescape')] = (mode, oid)
    return entries


def write_tree(repo, entries):
    """The tree object of {path: (mode, oid)}, built in a private index (never the repository's own)."""
    with tempfile.TemporaryDirectory(prefix='pisar-sync-index-') as scratch:
        index = Path(scratch) / 'index'
        git_bytes(repo, 'read-tree', '--empty', index=index)
        lines = b''.join(f'{mode} {oid} 0\t{path}\0'.encode('utf-8', 'surrogateescape')
                         for path, (mode, oid) in sorted(entries.items()))
        git_bytes(repo, 'update-index', '-z', '--index-info', input=lines, index=index)
        return git(repo, 'write-tree', index=index).strip()


def scratch_commit(repo, tree):
    """A commit object with TREE and no parents, never referenced: validation reads a candidate through it."""
    return git(repo, 'commit-tree', tree, '-m', 'pisar sync candidate', identity=True).strip()


def hash_blob(repo, data):
    return git(repo, 'hash-object', '-w', '--no-filters', '--stdin', input=data).strip()


# --- repositories and names ----------------------------------------------------------------------------------

def repository(root, relative):
    """The Git repository named by a root-relative path ('.' or empty: the root); its top must be that path."""
    root = Path(root).resolve()
    if relative in (None, '', '.'):
        repo = root
    else:
        repo = safe_path(root, relative)
    if not repo.is_dir() or gitops.owner(repo, root) != repo.resolve():
        raise WikiError(f'--repo must name a Git repository in the knowledge root: {relative}')
    gitops.ancestors(repo, root)  # Every nested repository is a registered submodule of its parent.
    _guard(repo)
    return repo


def _guard(repo):
    """Refuse REPO when its own configuration or attribute file could run a program or rewrite a transport.

    Read for REPO alone: its local and worktree configuration (includes expanded, so an included file counts), its
    info/attributes, and every include that points outside its Git directory. Global and system configuration is the
    user's own and is trusted. Sync inspects each repository through this guard before any Git process reads it, and
    never runs a recursive status, so a submodule's settings are never read as the parent's (review 20b, A)."""
    for line in git(repo, 'config', '--list', '--show-scope', '--show-origin', '--includes', '--name-only',
                     check=False).splitlines():
        scope, origin, key = (line.split('\t', 2) + ['', '', ''])[:3]
        if scope in ('local', 'worktree') and DANGEROUS_KEY.match(key):
            raise WikiError(f'{repo}: {key} ({origin}) is set in this repository; pisar sync refuses a repository '
                            'that names a program or rewrites a transport (remove it, or configure it outside the '
                            'repository)')
    _refuse_outside_includes(repo)
    _refuse_local_attributes(repo)


def _refuse_outside_includes(repo):
    """Refuse an include that reads a file outside the repository's Git directory (its contents are checked above)."""
    git_dir = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-dir').strip()).resolve()
    for record in git(repo, 'config', '--local', '-z', '--get-regexp', INCLUDE_KEY.pattern, check=False).split('\0'):
        if '\n' not in record:
            continue
        key, value = record.split('\n', 1)
        target = Path(os.path.expanduser(value))
        if not target.is_absolute():
            target = git_dir / target
        if not target.resolve().is_relative_to(git_dir):
            raise WikiError(f'{repo}: {key} includes {value}, outside the repository; pisar sync refuses it')


def _refuse_local_attributes(repo):
    """Refuse a repository whose own info/attributes sets anything: Git reads that file whatever the attribute
    source, so a sync there could apply a conversion. The user's own .gitattributes is refused as content later."""
    path = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'info/attributes').strip())
    if path.is_file() and _attribute_problem(path.read_text(encoding='utf-8', errors='replace')):
        raise WikiError(f'{path} sets Git attributes; pisar sync refuses to run in this repository')


def rel(root, repo):
    return '.' if repo == root else repo.relative_to(root).as_posix()


def names(repo, remote=None, branch=None):
    """(remote, remote branch, private ref) for REPO: a remote NAME configured in the local configuration."""
    name, _ = settings.sync_option('remote', remote)
    if not isinstance(name, str) or not config.SYNC_REMOTE.fullmatch(name):
        raise WikiError(f'remote must be the name of a Git remote, not a URL or path: {name!r}')
    if not git(repo, 'config', '--local', '--get', f'remote.{name}.url', check=False).strip():
        raise WikiError(f'no remote named {name} in {repo}; pisar never adds remotes, configure it with git remote add')
    value, _ = settings.sync_option('branch', branch)
    if value is None:
        head = symbolic_head(repo)
        if not head.startswith('refs/heads/'):
            raise WikiError(f'HEAD of {repo} is detached; pass --branch with the remote branch to fetch')
        value = head[len('refs/heads/'):]
    if not isinstance(value, str) or not config.valid_branch(value):
        raise WikiError(f'branch must be a short branch name: {value!r}')
    ref = f'{REF_ROOT}/{name}/{value}'
    if subprocess.run(['git', 'check-ref-format', ref], capture_output=True).returncode:
        raise WikiError(f'remote {name} and branch {value} do not make a valid ref')
    return name, value, ref


def local_branch(repo):
    """The local branch pull moves: the ref HEAD names (a detached HEAD is refused)."""
    head = symbolic_head(repo)
    if not head.startswith('refs/heads/'):
        raise WikiError(f'HEAD of {repo} is detached; pisar sync pull needs a local branch')
    return head


def fetch(repo, remote, branch, ref):
    """Fetch BRANCH of REMOTE into REF only: objects are stored, HEAD, index, worktree and other refs stay."""
    previous = commit_of(repo, ref)
    git(repo, '-c', 'fetch.recurseSubmodules=false', '-c', 'maintenance.auto=false', '-c', 'gc.auto=0',
        '-c', 'fetch.writeCommitGraph=false', '-c', 'fetch.prune=false', '-c', 'fetch.pruneTags=false',
        '-c', 'transfer.fsckObjects=true', '-c', 'fetch.fsckObjects=true',
        '-c', f'remote.{remote}.uploadpack=git-upload-pack', '-c', f'remote.{remote}.receivepack=git-receive-pack',
        'fetch', '--no-tags', '--no-write-fetch-head', '--no-recurse-submodules', '--no-prune', '--refmap=',
        remote, f'+refs/heads/{branch}:{ref}', network=True)
    return previous, commit_of(repo, ref)


# --- what a change contains --------------------------------------------------------------------------------------

def raw_changes(repo, base, target):
    """(path, old mode, new mode, old id, new id, status) of every entry that differs (renames not paired)."""
    fields = [f for f in git_bytes(repo, 'diff-tree', '-r', '-z', '--no-renames', base, target)
              .decode('utf-8', 'surrogateescape').split('\0') if f]
    found = []
    for meta, path in zip(fields[::2], fields[1::2]):
        old_mode, new_mode, old_id, new_id, status = meta[1:].split()
        found.append((path, old_mode, new_mode, old_id, new_id, status[0]))
    return found


def _spaces_in(repo, revision, prefix):
    """{root-relative space directory: space id} of the .wiki.toml files of REVISION (read as objects)."""
    found = {}
    wanted = {path: oid for path, (mode, oid) in tree_entries(repo, revision).items()
              if path.rsplit('/', 1)[-1] == '.wiki.toml' and mode in BLOBS}
    blobs = read_blobs(repo, wanted.values())
    for path, oid in wanted.items():
        try:
            ident = tomllib.loads(blobs[oid].decode('utf-8'))['id']
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            continue
        directory = path.rsplit('/', 1)[0] if '/' in path else ''
        found[f'{prefix}{directory}'.rstrip('/')] = ident
    return found


def summarise(repo, root, base, target):
    """The change from BASE to TARGET grouped by domain and space; every path is root-relative.

    Spaces are read from both sides, so a space the remote adds or removes is mapped. A document carries its
    id and change letter (the id comes from the side it exists on). A submodule pin is listed under
    `submodules`; a path of a domain outside every space is listed under that domain's `files`; a path
    outside every domain under `other`."""
    relative = '' if repo == root else repo.relative_to(root).as_posix()
    prefix = f'{relative}/' if relative else ''
    spaces = {**_spaces_in(repo, base, prefix), **_spaces_in(repo, target, prefix)}
    domains, other, submodules = {}, [], []
    for path, old_mode, new_mode, old_id, new_id, status in raw_changes(repo, base, target):
        full = f'{prefix}{path}'
        if GITLINK in (old_mode, new_mode):
            submodules.append(dict(path=full, change=status, previous=old_id if old_mode == GITLINK else None,
                                   current=new_id if new_mode == GITLINK else None))
            continue
        if '/' not in full:
            other.append(dict(path=full, change=status))
            continue
        domain = full.split('/', 1)[0]
        bucket = domains.setdefault(domain, dict(count=0, spaces={}, files=[]))
        bucket['count'] += 1
        owner = max((d for d in spaces if full.startswith(d + '/')), key=len, default=None)
        if owner is None:
            bucket['files'].append(dict(path=full, change=status))
            continue
        space = bucket['spaces'].setdefault(f'{domain}/{spaces[owner]}', dict(documents=[], files=[]))
        if full.endswith('.md'):
            side = base if status == 'D' else target
            space['documents'].append(dict(path=full, id=_document_id(repo, side, path), change=status))
        else:
            space['files'].append(dict(path=full, change=status))
    return dict(domains=domains, other=other, submodules=submodules)


def _document_id(repo, revision, path):
    entry = tree_entries(repo, revision).get(path)
    if entry is None:
        return None
    try:
        meta = documents.metadata(read_blobs(repo, [entry[1]])[entry[1]].decode('utf-8'))
    except (WikiError, UnicodeDecodeError):
        return None
    return meta.get('id') if isinstance(meta, dict) else None


# --- journals ------------------------------------------------------------------------------------------------------

def sync_dir(runtime):
    directory = runtime.path / 'sync'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def journal_file(runtime, ident):
    return sync_dir(runtime) / f'{concrete_id(ident, "sync id")}.json'


def load(runtime, ident):
    path = journal_file(runtime, ident)
    if not path.is_file():
        raise WikiError(f'no sync {ident} in this root')
    journal = json.loads(path.read_text(encoding='utf-8'))
    if journal.get('operation_id') != ident or journal.get('format') != FORMAT:
        raise WikiError(f'sync journal {ident} is not readable by this pisar')
    return journal


def store(runtime, journal):
    write_json(journal_file(runtime, journal['operation_id']), journal)


class Store:
    """What the commit-message writer needs of a journal store: a base directory and `store`."""

    def __init__(self, runtime):
        self.base = runtime.base
        self.runtime = runtime

    def store(self, journal):
        store(self.runtime, journal)


def journals_of(runtime, relative):
    """Every journal of the repository RELATIVE, oldest first."""
    found = []
    for path in sorted(sync_dir(runtime).glob('sync-*.json')):
        try:
            journal = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if journal.get('repo') == relative:
            found.append(journal)
    return found


def open_journal(runtime, relative):
    """The one unfinished journal of the repository RELATIVE, or None; two at once is refused."""
    opened = [j for j in journals_of(runtime, relative) if j.get('status') in ('open', 'conflicts')]
    if len(opened) > 1:
        raise WikiError(f'more than one sync is open for {relative}: {", ".join(j["operation_id"] for j in opened)}')
    return opened[0] if opened else None


def sync_id(relative, base, fetched, mode, remote_branch):
    return 'sync-' + sha256(json.dumps([relative, base, fetched, mode, remote_branch]).encode())[:12]


def work_dir(runtime, ident):
    """The worktree of a conflicted sync (created by build_work, removed when the sync ends)."""
    return sync_dir(runtime) / ident / 'work'


def safe_work_path(runtime, ident, relative):
    """The path of RELATIVE inside the work tree of sync IDN, confined by safety.safe_path (see mcp.sync_file)."""
    return safe_path(work_dir(runtime, ident), relative)


def scratch_path(runtime, ident, name):
    path = sync_dir(runtime) / ident / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def remove_scratch(runtime, ident):
    """Remove the scratch directory of one sync (never its journal). Only inside the sync directory."""
    directory = sync_dir(runtime) / ident
    if directory.is_symlink():
        raise WikiError(f'sync directory is a symlink: {directory}')
    if directory.exists():
        shutil.rmtree(directory)


def discard_worktree(repo, path):
    """Remove the linked worktree of a sync: its files and its administrative entry."""
    if path.exists() or path.is_symlink():
        git(repo, 'worktree', 'remove', '--force', str(path), check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    git(repo, 'worktree', 'prune', check=False)


def finish_leftovers(runtime, repo, relative):
    """Clean up the work of completed syncs of this repository that were interrupted before their cleanup."""
    for journal in journals_of(runtime, relative):
        if journal.get('status') == 'complete' and journal.get('worktree'):
            discard_worktree(repo, scratch_path(runtime, journal['operation_id'], 'work'))
            journal['worktree'] = False
            store(runtime, journal)
        if journal.get('status') in ('complete', 'aborted'):
            remove_scratch(runtime, journal['operation_id'])


# --- trees: materialisation and validation -------------------------------------------------------------------

def _path_problem(path):
    """Why a path of fetched content is unsafe, or None: fetched content is untrusted."""
    try:
        path.encode('utf-8')
    except UnicodeEncodeError:
        return 'path is not valid UTF-8'
    parts = path.split('/')
    if path.startswith('/') or '\\' in path or '\0' in path or any(p in ('', '.', '..') for p in parts):
        return 'unsafe path'
    if any(p.casefold() == '.git' or p.casefold().startswith('.git~') for p in parts):
        return 'Git administrative names are refused'
    return None


def _attribute_problem(content):
    """The first line of CONTENT that is not a comment, or None when CONTENT is comments and blank lines only.

    Only a line whose first non-blank character is '#' is a comment. Anything else (a pattern, a macro, quoting,
    an escape) is a rule Git would apply, and is refused rather than parsed: refusing is always safe here."""
    for line in content.split('\n'):
        if line.strip() and not line.lstrip().startswith('#'):
            return line.strip()
    return None


def materialise(repo, revision, dest, prefix, overrides, root):
    """Write the tree of REVISION of REPO under DEST: raw bytes, no filter, no checkout.

    A gitlink is filled from its child repository at the commit pinned there, or at OVERRIDES[path] when a
    candidate chain puts another commit there. Refused: symbolic links, unsafe or Git-named paths, a
    .gitattributes that changes how Git converts files, a child repository that is not present."""
    entries = tree_entries(repo, revision)
    blobs = read_blobs(repo, [oid for mode, oid in entries.values() if mode in BLOBS])
    for path, (mode, oid) in sorted(entries.items()):
        where = f'{prefix}{path}'
        if _path_problem(where):
            raise WikiError(f'{where}: {_path_problem(where)}')
        if mode == '120000':
            raise WikiError(f'{where}: symbolic links are refused in synced content')
        target = Path(dest) / where
        if mode == GITLINK:
            child = root / where
            if not (child / '.git').exists():
                raise WikiError(f'{where}: submodule is not present locally (unexpected-submodule)')
            child_revision = overrides.get(where, oid)
            if commit_of(child, child_revision) is None:
                raise WikiError(f'{where}: pinned commit {child_revision} is not present locally')
            target.mkdir(parents=True, exist_ok=True)
            materialise(child, child_revision, dest, f'{where}/', overrides, root)
            continue
        data = blobs[oid]
        if path.rsplit('/', 1)[-1] == '.gitattributes':
            name = _attribute_problem(data.decode('utf-8', 'replace'))
            if name:
                raise WikiError(f'{where}: attributes that change how Git stores files are refused ({name})')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, 0o755 if mode == '100755' else 0o644)


def candidate_root(root, repo, tree, chain):
    """(root tree, overrides) with the candidate TREE of REPO in place of its pin.

    For the root itself this is TREE. For a nested repository every ancestor gets a scratch commit (an object,
    never a ref) whose tree pins the commit of the level below; OVERRIDES maps each child to its scratch commit."""
    if repo == root:
        return tree, {}
    overrides = {}
    current = scratch_commit(repo, tree)
    for parent, link, child in chain:
        overrides[child.relative_to(root).as_posix()] = current
        parent_tree = write_tree(parent, {**tree_entries(parent, head_commit(parent)), link: (GITLINK, current)})
        current = scratch_commit(parent, parent_tree)
    return git(root, 'rev-parse', f'{current}^{{tree}}').strip(), overrides


def problems_of(runtime, ident, root, tree, overrides, changed):
    """Why the candidate root tree is not valid (empty when valid): `pisar check`'s rules plus conflict markers.

    CHANGED lists the root-relative paths the sync writes. Only those are scanned for conflict markers, and never a
    file under a `sources` folder: an immutable original may legitimately contain marker-like lines (its bytes are
    checked against the sides in resolve). The candidate is materialised in a scratch directory that is removed
    afterwards; nothing else is written."""
    scratch = scratch_path(runtime, ident, 'validate')
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(mode=0o700)
    try:
        try:
            materialise(root, tree, scratch, '', overrides, root)
        except WikiError as error:
            return [str(error)]
        result = documents.check(Wiki(scratch))
        problems = list(result['errors'])
        for path in sorted(changed):
            if 'sources' in path.split('/') or not (scratch / path).is_file():
                continue
            text = (scratch / path).read_bytes().decode('utf-8', 'replace')
            if MARKER.search(text):
                problems.append(f'{path}: unresolved conflict markers')
        return problems
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --- the three-way merge of trees (no Git merge, no driver) -----------------------------------------------------

def _merge_text(repo, scratch, ours, base, theirs):
    """(merged bytes, conflicts) of three texts with `git merge-file -p` (no attribute, no driver involved)."""
    paths = []
    for name, data in (('ours', ours), ('base', base), ('theirs', theirs)):
        path = scratch / f'{name}.txt'
        path.write_bytes(data)
        paths.append(str(path))
    argv, env = _command(repo, ('-c', 'merge.conflictStyle=merge', 'merge-file', '-p', '-L', 'ours', '-L', 'base',
                                '-L', 'theirs', *paths))
    result = subprocess.run(argv, capture_output=True, env=env)
    if result.returncode < 0 or result.returncode >= 128:
        raise WikiError(f'merge-file failed in {repo}: {result.stderr.decode("utf-8", "replace").strip()}')
    return result.stdout, result.returncode


def merge_trees(repo, ancestor, ours, theirs, scratch, is_source=lambda path: False):
    """(clean, conflicts): the merged {path: (mode, oid)} and the conflicts left, by three-way rules.

    A path changed on one side only takes that side (a deletion included). Both sides changed the same way take
    that result. Otherwise: two text blobs are merged line by line, and what cannot be merged is a conflict of
    kind `content` (markers in the work file), `add-add` (no common ancestor), `binary`, `mode`, `delete-modify`
    (one side deleted it) or `submodule` (a gitlink). Renames are not detected: they are delete plus add."""
    clean, conflicts = {}, []
    for path in sorted(set(ancestor) | set(ours) | set(theirs)):
        a, o, t = ancestor.get(path), ours.get(path), theirs.get(path)
        if o == t:
            if o is not None:
                clean[path] = o
            continue
        if a == o:
            if t is not None:
                clean[path] = t
            continue
        if a == t:
            if o is not None:
                clean[path] = o
            continue
        sides = [x for x in (a, o, t) if x is not None]
        if any(mode == GITLINK for mode, _ in sides):
            conflicts.append(_conflict(path, 'submodule', a, o, t, None))
            continue
        if any(mode not in BLOBS for mode, _ in sides):
            raise WikiError(f'{path}: symbolic links and other modes cannot be merged; resolve it outside pisar')
        if o is None or t is None:
            present = read_blobs(repo, [(o or t)[1]])[(o or t)[1]]
            conflicts.append(_conflict(path, 'delete-modify', a, o, t, present))
            continue
        blobs = read_blobs(repo, [x[1] for x in sides])
        ours_bytes, theirs_bytes = blobs[o[1]], blobs[t[1]]
        base_bytes = blobs[a[1]] if a is not None else b''
        if o[0] != t[0]:
            conflicts.append(_conflict(path, 'mode', a, o, t, ours_bytes))
            continue
        if any(b'\0' in data for data in (ours_bytes, base_bytes, theirs_bytes)):
            conflicts.append(_conflict(path, 'binary', a, o, t, ours_bytes))
            continue
        if is_source(path):  # An immutable original is never merged line by line: its conflict is taken whole.
            conflicts.append(_conflict(path, 'content', a, o, t, ours_bytes))
            continue
        merged, count = _merge_text(repo, scratch, ours_bytes, base_bytes, theirs_bytes)
        if count:
            conflicts.append(_conflict(path, 'add-add' if a is None else 'content', a, o, t, merged))
        else:
            clean[path] = (o[0], hash_blob(repo, merged))
    _no_clash(sorted(set(clean) | {c['path'] for c in conflicts}))
    return clean, conflicts


def _unsafe_entries(clean, conflicts):
    """Problems of the entries a merge would write: unsafe names and symbolic links, clean or conflicted."""
    problems = []
    for path, (mode, _) in clean.items():
        if _path_problem(path):
            problems.append(f'{path}: {_path_problem(path)}')
        elif mode == '120000':
            problems.append(f'{path}: symbolic links are refused in synced content')
    for conflict in conflicts:
        path = conflict['path']
        if _path_problem(path):
            problems.append(f'{path}: {_path_problem(path)}')
        elif any(entry and entry[0] == '120000' for entry in conflict['stages'].values()):
            problems.append(f'{path}: symbolic links are refused in synced content')
    return problems


def _conflict(path, kind, a, o, t, data):
    return dict(path=path, kind=kind, stages={'1': a, '2': o, '3': t}, data=data)


def _no_clash(paths):
    """Refuse a file on one side and a directory of the same name on the other (not resolvable here)."""
    found = set(paths)
    for path in paths:
        parts = path.split('/')
        for depth in range(1, len(parts)):
            if '/'.join(parts[:depth]) in found:
                raise WikiError(f'unsupported merge: {"/".join(parts[:depth])} is a file on one side and a directory '
                                'on the other; sync cannot merge that')


# --- planning a move and checking it before anything is written ----------------------------------------------

def plan_between(repo, old, new):
    """[{path, old, new}] of every blob or gitlink that differs between two trees (old/new: [mode, oid] or None)."""
    before, after = tree_entries(repo, old), tree_entries(repo, new)
    plan = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) != after.get(path):
            plan.append(dict(path=path, old=list(before[path]) if path in before else None,
                             new=list(after[path]) if path in after else None))
    return plan


def check_children(root, repo, plan):
    """Every gitlink the plan changes must be a registered submodule, present, already at the new pin."""
    for item in plan:
        if item['old'] and item['old'][0] == GITLINK and item['new'] is None:
            raise WikiError(f'{item["path"]}: removing a submodule is not supported by sync')
        if not (item['new'] and item['new'][0] == GITLINK):
            continue
        absolute = repo / item['path']
        relative = rel(root, absolute)
        if not (absolute / '.git').exists() or not git(repo, 'ls-files', '--stage', '--', item['path']
                                                       ).startswith(GITLINK):
            raise WikiError(f'{relative}: unexpected-submodule (not a registered submodule present locally)')
    lagging = [rel(root, repo / item['path']) for item in plan
               if item['new'] and item['new'][0] == GITLINK and head_commit(repo / item['path']) != item['new'][1]]
    if lagging:
        raise WikiError(f'needs-submodule-sync: {", ".join(lagging)}; sync those submodules first (children before parents)')


def unsafe_paths(plan):
    """Problems of the paths a fetched plan would write (reported as an invalid remote, never written)."""
    return [f'{item["path"]}: {_path_problem(item["path"])}' for item in plan if _path_problem(item['path'])]


def _gitlink_item(item):
    return any(x and x[0] == GITLINK for x in (item['old'], item['new']))


def _index_entry(repo, path):
    """(mode, oid) of the stage-0 index entry of PATH, None when there is none. Unmerged entries are refused."""
    rows = [row for row in git(repo, 'ls-files', '--stage', '-z', '--', path).split('\0') if row]
    if not rows:
        return None
    if len(rows) != 1:
        raise WikiError(f'{path}: more than one index entry; not touched')
    meta, name = rows[0].split('\t', 1)
    mode, oid, stage = meta.split()
    if stage != '0' or name != path:
        raise WikiError(f'{path}: unmerged index entry; not touched')
    return mode, oid


def _working_matches(repo, path, state, blobs):
    """Whether the working file of PATH is exactly STATE (mode, oid), or absent when STATE is None."""
    file = repo / path
    if state is None:
        return not (file.exists() or file.is_symlink())
    if file.is_symlink() or not file.is_file() or file.read_bytes() != blobs[state[1]]:
        return False
    return bool(os.access(file, os.X_OK)) == (state[0] == '100755')


def _gitlink_pin(repo, revision, link):
    """The (mode, oid) that REVISION records at LINK, or None."""
    return tree_entries(repo, revision).get(link)


def classify(repo, plan, blobs=None):
    """The state of every planned path, from the journaled old and new entries alone (see _classify_one)."""
    blobs = blobs if blobs is not None else read_blobs(repo, [x[1] for item in plan for x in (item['old'], item['new'])
                                                                if x and x[0] in BLOBS])
    return [_classify_one(repo, item, blobs) for item in plan]


def _classify_one(repo, item, blobs):
    """The state of one planned path:

    `before`: the index and the working copy are still the old state (nothing of the plan applied yet);
    `written`: the working copy is the new state but the index is still the old one (an interruption between the
    two steps); `after`: the index is the new state and the working copy too, so there is nothing left to do.
    Anything else is a change nobody journaled: WikiError, and the caller changes nothing (review 20, 3 and 4)."""
    path = item['path']
    old = tuple(item['old']) if item['old'] else None
    new = tuple(item['new']) if item['new'] else None
    entry = _index_entry(repo, path)
    if _gitlink_item(item):
        if entry == new:
            return 'after'
        if entry == old:
            return 'before'
        raise WikiError(f'{path}: the index holds neither the old nor the new submodule pin; not touched')
    if entry == new:
        if not _working_matches(repo, path, new, blobs):
            raise WikiError(f'{path}: changed after the sync wrote it; not touched (restore the sync\'s version, '
                            'then run the command again)')
        return 'after'
    if entry != old:
        raise WikiError(f'{path}: the index holds content the sync did not write (staged or changed); not touched')
    if _working_matches(repo, path, old, blobs):
        _shape_ok(repo, item)
        return 'before'
    if _working_matches(repo, path, new, blobs):
        return 'written'
    raise WikiError(f'{path}: changed since the sync was planned; untracked or edited file in the way')


def _shape_ok(repo, item):
    """Refuse a planned path that cannot be written: a file in the way of a directory, or a link or a directory."""
    parts = item['path'].split('/')
    for depth in range(1, len(parts)):
        parent = repo / '/'.join(parts[:depth])
        if parent.exists() and (not parent.is_dir() or parent.is_symlink()):
            raise WikiError(f'{item["path"]}: a file is in the way of its directory')
    if item['new'] is not None and gitops.attribute_problems(repo, item['path']):
        raise WikiError(f'{item["path"]}: Git attributes would change how this file is stored; refused')


def check_files(repo, plan):
    """Before the first move: every planned path must be exactly in its old state (see classify)."""
    if any(state != 'before' for state in classify(repo, plan)):
        raise WikiError('the repository changed since the sync was planned; nothing was changed')


def apply_files(repo, runtime, plan, ident):
    """Install the plan, one path at a time: the working file, then the index entry (never a reset, never a re-hash).

    Every path is classified before the first write (a refusal changes nothing), and again immediately before it is
    touched, so an edit made while the install runs is refused and kept, not overwritten (review 20b, C). The install
    stops there; the paths already installed are recognised as `after` on the next run."""
    blobs = read_blobs(repo, [x[1] for item in plan for x in (item['old'], item['new']) if x and x[0] in BLOBS])
    classify(repo, plan, blobs)
    for item in plan:
        state = _classify_one(repo, item, blobs)
        if state == 'after':
            continue
        if state == 'before' and not _gitlink_item(item):
            _write_plan_file(repo, item, blobs, ident)
            checkpoint('file-written')
        publish_entry(repo, runtime, ident, item['path'], item['old'], item['new'])
        checkpoint('index-set')


def publish_entry(repo, runtime, ident, path, old, new):
    """Move the real index entry of PATH from OLD to NEW (each a (mode, oid), or None for no entry), and nothing else.

    This is the only code that writes the real index. It follows Git's own protocol: create `index.lock` exclusively
    (refused, and never removed, when it exists), then, while holding it, compare the entry Git holds now with the
    journaled states, build the new index in a private copy, write it into the lock and rename it over the index. Git
    writers either finish before the lock is taken (and the comparison sees them) or find the lock and fail.

    Everything after the lock is created runs in one try/finally: an unpublished lock created here is removed on every
    unwound exit (a return, an exception, a KeyboardInterrupt). A lock whose creation failed is never removed. Only an
    abrupt exit (no unwinding, as a kill) leaves the lock, and the next run refuses to start while it exists.

    The private copy keeps the real index's timestamps, so Git judges the cached entries exactly as it would for the real
    index (review 20d, 2); the lock gets the timestamp of the private index before it is renamed; the lock gets the
    access mode of the index it replaces, or the mode Git would give a new index (review 20d, 3)."""
    index = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'index').strip())
    lock = index.with_name(index.name + '.lock')
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise WikiError(f'{lock} exists: another Git process holds the index. pisar sync does not remove it; when no '
                        'git process is running, remove it yourself and run the command again') from None
    scratch = scratch_path(runtime, ident, 'index.private')
    published = False
    try:
        os.fchmod(fd, _index_mode(repo, index))
        checkpoint('index-locked')
        current = _index_entry(repo, path)  # read while the lock is held: no Git writer can change it now
        wanted_new = tuple(new) if new else None
        if current == wanted_new:
            return  # nothing to publish: the finally releases our lock
        if current != (tuple(old) if old else None):
            raise WikiError(f'{path}: the index changed while the sync ran (entry {current}); not overwritten')
        _private_copy(repo, index, scratch)
        if new is None:
            git_bytes(repo, 'update-index', '--force-remove', '--', path, index=scratch)
        else:
            git_bytes(repo, 'update-index', '--add', '--cacheinfo', f'{new[0]},{new[1]},{path}', index=scratch)
        checkpoint('index-compared')
        data = scratch.read_bytes()
        stamp = os.stat(scratch)
        with os.fdopen(fd, 'wb') as stream:
            fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.utime(lock, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        os.replace(lock, index)
        published = True
    finally:
        if fd is not None:
            os.close(fd)
        scratch.unlink(missing_ok=True)
        if not published:
            lock.unlink(missing_ok=True)  # created by this call: the only lock we ever remove


def _private_copy(repo, index, scratch):
    """The private index to update: the real index's bytes with the real index's timestamps, so Git loads it as the
    real index and judges its racy entries alike."""
    scratch.unlink(missing_ok=True)
    if not index.exists():
        git_bytes(repo, 'read-tree', 'HEAD', index=scratch)
        return
    stamp = os.stat(index)
    scratch.write_bytes(index.read_bytes())
    os.utime(scratch, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))


def _index_mode(repo, index):
    """The access mode for the index being published: the mode of the existing index, else Git's own rule for a new one
    (core.sharedRepository, else 0666 minus the umask), set explicitly so the umask does not decide it."""
    if index.exists():
        return stat.S_IMODE(index.stat().st_mode)
    shared = git(repo, 'config', '--get', 'core.sharedRepository', check=False).strip().lower()
    if shared in ('group', 'true', '1'):
        return 0o660
    if shared in ('all', 'world', 'everybody', '2'):
        return 0o664
    if re.fullmatch(r'0?[0-7]{3,4}', shared):
        return int(shared, 8) & 0o777
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask




def _temp_name(ident, path):
    """The sync's own temporary file for PATH: derived from the sync id, so a leftover is recognised as ours."""
    parent, _, name = path.rpartition('/')
    temp = f'.pisar-sync-{ident}-{sha256(path.encode())[:12]}.tmp'
    return f'{parent}/{temp}' if parent else temp


def _write_plan_file(repo, item, blobs, ident):
    path = repo / item['path']
    if item['new'] is None:
        if path.exists():
            path.unlink()
        _prune_empty(repo, path.parent)
        return
    _publish_file(path, blobs[item['new'][1]], 0o755 if item['new'][0] == '100755' else 0o644,
                  repo / _temp_name(ident, item['path']))


def _publish_file(path, data, mode, scratch):
    """Replace PATH with DATA and MODE together: the bytes go to a sibling file that already has its final mode, and
    that file is renamed over PATH (review 20b, D). A crash leaves either the old file or the complete new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if scratch.is_symlink():
        raise WikiError(f'{scratch}: a link is in the way of the sync\'s temporary file')
    if scratch.exists():
        scratch.unlink()  # ours by name: left by an earlier interrupted publication of this path
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(scratch, path)


def _prune_empty(repo, directory):
    while directory != repo and directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
        directory = directory.parent


# --- journals of a pull: planning, the move, the files, the ancestors ---------------------------------------

def checkpoint(name):
    """A named point where a run may be interrupted; tests replace this with a fault (see test_sync_resume)."""


def new_journal(root, relname, repo, names_, mode, base, fetched, merge_base, plan, ident, phase='planned'):
    name, value, ref = names_
    heads = {relname: base}
    for parent, _, _ in gitops.ancestors(repo, root):
        heads[rel(root, parent)] = head_commit(parent)
    return dict(schema_version=1, format=FORMAT, operation_id=ident, repo=relname, remote=name, remote_branch=value,
                fetched_ref=ref, local_ref=local_branch(repo), mode=mode, base=base, merge_base=merge_base,
                fetched=fetched, target=None, tree=None, when=int(time.time()), status='open', phase=phase, plan=plan,
                heads=heads, pending={}, commits=[], conflicts=[], choices={}, decided={}, worktree=False, messages={})


def _pending_hook(runtime, journal, relname):
    def record(value):
        journal['pending'][relname] = value
        store(runtime, journal)
    return record


def _landed(journal, relname, commit):
    if not any(c['repo'] == relname and c['commit'] == commit for c in journal['commits']):
        journal['commits'].append(dict(repo=relname, commit=commit))


def move(root, runtime, wiki, repo, journal):
    """Move the local branch to the journal's target, then install the files and pin the ancestors.

    Each step recognises what an earlier run did, and never replays over anything else: the branch by its commit id
    (compare-and-swap from the journaled base), the files and index by their journaled states (see classify), the
    ancestors by their journaled commits and index entries."""
    relname, ident, target, base = journal['repo'], journal['operation_id'], journal['target'], journal['base']
    head = head_commit(repo)
    if journal['phase'] == 'planned':
        if head == base:
            if symbolic_head(repo) != journal['local_ref']:
                raise WikiError(f'the checked-out branch changed since sync {ident}; refusing to move it')
            if (journal['pending'].get(relname) or {}).get('commit') != target:
                journal['pending'][relname] = dict(commit=target, parent=base)
                store(runtime, journal)
            checkpoint('pending')
            git(repo, 'update-ref', '-m', f'pisar sync: {ident}', journal['local_ref'], target, base)
        elif head != target:
            raise WikiError(f'Git HEAD changed outside operation: {relname}')
        checkpoint('moved')
        journal['pending'][relname] = None
        _landed(journal, relname, target)
        journal['phase'] = 'moved'
        store(runtime, journal)
    elif head != target:
        raise WikiError(f'Git HEAD changed outside operation: {relname}')
    apply_files(repo, runtime, journal['plan'], ident)
    checkpoint('files')
    journal['phase'] = 'files'
    store(runtime, journal)
    pin_ancestors(root, runtime, wiki, repo, journal)
    journal['status'] = 'complete'
    journal['phase'] = 'complete'
    store(runtime, journal)
    checkpoint('complete')


def pin_ancestors(root, runtime, wiki, repo, journal):
    """Pin each ancestor to the commit below it, children before parents, one journaled commit each.

    An ancestor that already holds its commit is finished, not skipped: its index entry for the child is brought to
    the commit HEAD records (see _finish_pin) before the ancestor is recorded as done."""
    current = journal['target']
    for parent, link, _ in gitops.ancestors(repo, root):
        relname = rel(root, parent)
        head = head_commit(parent)
        pending = journal['pending'].get(relname)
        known = {journal['heads'][relname], *(c['commit'] for c in journal['commits'] if c['repo'] == relname)}
        if pending:
            known.add(pending['commit'])
        if head not in known:
            raise WikiError(f'Git HEAD changed outside operation: {relname}')
        old_pin = _gitlink_pin(parent, journal['heads'][relname], link)
        pinned = _gitlink_pin(parent, head, link)
        landed = head != journal['heads'][relname]
        if pinned is None or pinned[1] != current:
            _clean(parent, [link])
            head = gitops.commit(parent, [link], journal['operation_id'], pins={link: current},
                                 intent=_pending_hook(runtime, journal, relname),
                                 message=operations.commit_writer(wiki, Store(runtime), journal, parent),
                                 sync=lambda where, paths: _finish_pin(runtime, journal['operation_id'], where, link,
                                                                       old_pin, current))
            head = head_commit(parent)
            landed = True
        else:
            _finish_pin(runtime, journal['operation_id'], parent, link, old_pin, current)
        if landed:
            _landed(journal, relname, head)
        journal['pending'][relname] = None
        store(runtime, journal)
        checkpoint('pinned')
        current = head
    return current


def _finish_pin(runtime, ident, repo, link, old_pin, new_commit):
    """Make the index entry of the gitlink LINK the pin that HEAD records (NEW_COMMIT). Only the journaled old entry
    (OLD_PIN, a (mode, oid)) is replaced; any other index state is refused, and nothing is reset."""
    checkpoint('ancestor-moved')
    publish_entry(repo, runtime, ident, link, old_pin, (GITLINK, new_commit))


def _is_source(path, spaces):
    """True for a path directly inside a `sources` folder of a space (immutable originals).

    SPACES holds the space directories of a repository, relative to it: '' is the space at the repository's own
    root (a submodule whose .wiki.toml sits at its top), and its sources are `sources/...` paths."""
    owners = [space for space in spaces if space == '' or path.startswith(space + '/')]
    if not owners:
        return False
    owner = max(owners, key=len)
    rest = path[len(owner) + 1:] if owner else path
    return rest.startswith('sources/')


def _meta(repo, entry):
    """{id, type, title} of a Markdown side with front matter, or None."""
    if entry is None:
        return None
    try:
        meta = documents.metadata(read_blobs(repo, [entry[1]])[entry[1]].decode('utf-8'))
    except (WikiError, UnicodeDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    return {key: meta[key] for key in ('id', 'type', 'title') if key in meta}


def conflict_records(repo, head, fetched, conflicts):
    """The conflicts as journaled: kind, stages, whether the path is a source, and each side's document identity."""
    spaces = {**_spaces_in(repo, head, ''), **_spaces_in(repo, fetched, '')}
    records = []
    for conflict in conflicts:
        path = conflict['path']
        ours = conflict['stages']['2']
        theirs = conflict['stages']['3']
        records.append(dict(path=path, kind=conflict['kind'], stages=conflict['stages'],
                            sources=_is_source(path, spaces),
                            ours=_meta(repo, ours) if path.endswith('.md') else None,
                            theirs=_meta(repo, theirs) if path.endswith('.md') else None))
    return records


def build_work(repo, runtime, ident, head, clean, conflicts):
    """The dedicated worktree of a conflicted merge: detached at HEAD, with the stages of every conflict in its
    index and the files written as pisar's bytes (text conflicts carry the conflict markers)."""
    work = scratch_path(runtime, ident, 'work')
    discard_worktree(repo, work)  # Left by an interrupted run that wrote no journal: this sync owns it.
    git(repo, 'worktree', 'add', '--no-checkout', '--detach', str(work), head)
    index = Path(git(work, 'rev-parse', '--path-format=absolute', '--git-path', 'index').strip())
    lines = []
    for path, (mode, oid) in clean.items():
        lines.append((mode, oid, 0, path))
    for conflict in conflicts:
        for stage, entry in conflict['stages'].items():
            if entry is not None:
                lines.append((entry[0], entry[1], int(stage), conflict['path']))
    git_bytes(work, 'read-tree', '--empty', index=index)
    git_bytes(work, 'update-index', '-z', '--index-info', index=index,
              input=b''.join(f'{mode} {oid} {stage}\t{path}\0'.encode('utf-8', 'surrogateescape')
                             for mode, oid, stage, path in lines))
    blobs = read_blobs(repo, [oid for mode, oid in clean.values() if mode in BLOBS])
    for path, (mode, oid) in clean.items():
        _write_entry(work, path, mode, blobs.get(oid) if mode in BLOBS else None)
    for conflict in conflicts:
        data = conflict['data']
        ours = conflict['stages']['2'] or conflict['stages']['3'] or conflict['stages']['1']
        _write_entry(work, conflict['path'], ours[0] if ours else '100644', data)
    return work


def _write_entry(work, path, mode, data):
    target = work / path
    if not target.resolve().is_relative_to(work.resolve()) or _path_problem(path):
        raise WikiError(f'{path}: refused: the path would leave the work tree')
    if mode == GITLINK or data is None and mode not in BLOBS:
        target.mkdir(parents=True, exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if data is None:
        if target.exists() or target.is_symlink():
            target.unlink()
        return
    atomic_bytes(target, data)
    os.chmod(target, 0o755 if mode == '100755' else 0o644)


def _report(root, repo, base, target):
    ahead, behind = ahead_behind(repo, base, target)
    return dict(ahead=ahead, behind=behind, summary=summarise(repo, root, base, target))


def ahead_behind(repo, base, target):
    """(ahead, behind): commits HEAD has and the fetched commit lacks, and the reverse."""
    ahead, behind = git(repo, 'rev-list', '--left-right', '--count', f'{base}...{target}').split()
    return int(ahead), int(behind)


# --- commands -------------------------------------------------------------------------------------------------

def do_fetch(root, state_dir, relative=None, remote=None, branch=None):
    """Fetch the remote branch into the private ref and report what it would change. Writes that ref only."""
    check_git()
    root = Path(root).resolve()
    repo = repository(root, relative)
    name, value, ref = names(repo, remote, branch)
    runtime = Runtime(state_dir, root)
    relname = rel(root, repo)
    with runtime.lock():
        for journal in journals_of(runtime, relname):
            if journal.get('status') in ('open', 'conflicts') and journal.get('fetched_ref') == ref:
                raise WikiError(f'sync {journal["operation_id"]} uses {ref}; finish or abort it before fetching again')
        previous, target = fetch(repo, name, value, ref)
        base = head_commit(repo)
        return dict(remote=name, branch=value, repo=relname, ref=ref, previous=previous, head=base, commit=target,
                    **_report(root, repo, base, target))


def do_status(root, state_dir, relative=None, remote=None, branch=None):
    """What the fetched ref would change, and any open sync of the repository. Reads only."""
    check_git()
    root = Path(root).resolve()
    repo = repository(root, relative)
    name, value, ref = names(repo, remote, branch)
    runtime = Runtime(state_dir, root)
    relname = rel(root, repo)
    base = head_commit(repo)
    target = commit_of(repo, ref)
    result = dict(remote=name, branch=value, repo=relname, ref=ref, head=base, commit=target,
                  open=[dict(sync_id=j['operation_id'], status=j['status'], phase=j.get('phase'), mode=j.get('mode'))
                        for j in journals_of(runtime, relname) if j.get('status') in ('open', 'conflicts')])
    if target is not None:
        result.update(_report(root, repo, base, target))
    else:
        result['next'] = 'pisar sync fetch'
    return result


def do_pull(root, state_dir, relative=None, remote=None, branch=None, merge=False, dry_run=False):
    """Bring the fetched branch into the local branch: fast-forward, or a merge with --merge; never a push."""
    check_git()
    root = Path(root).resolve()
    runtime = Runtime(state_dir, root)
    repo = repository(root, relative)
    relname = rel(root, repo)
    names_ = names(repo, remote, branch)
    fetched = commit_of(repo, names_[2])
    if fetched is None:
        raise WikiError(f'nothing fetched for {names_[2]}; run pisar sync fetch first')
    wiki = Wiki(root)
    with runtime.lock():
        if not dry_run:
            finish_leftovers(runtime, repo, relname)
        journal = open_journal(runtime, relname)
        if journal is not None:
            if journal['status'] == 'conflicts':
                return dict(ok=False, status='conflicts', sync_id=journal['operation_id'],
                            conflicts=_conflict_view(journal),
                            next=f'edit the files in {work_dir(runtime, journal["operation_id"])}, then pisar sync '
                                 f'resolve --id {journal["operation_id"]}, or pisar sync abort --id {journal["operation_id"]}')
            if journal['fetched'] != fetched:
                raise WikiError(f'the fetched ref moved since sync {journal["operation_id"]}; abort it, then pull again')
            if dry_run:  # Read only: what the resume would do, decided on the same checks, nothing written.
                return _would_resume(repo, journal)
            return _resume(root, runtime, wiki, repo, journal)
        head = head_commit(repo)
        if is_ancestor(repo, fetched, head):
            return dict(ok=True, status='up-to-date', repo=relname, head=head, commit=fetched)
        _clean(repo)  # Nothing of the user's is dirty, staged or untracked: the move cannot take it.
        for parent, _, _ in gitops.ancestors(repo, root):
            _clean(parent)  # The pin of each parent is committed after the move: it must be clean now.
        mode = 'fast-forward' if is_ancestor(repo, head, fetched) else ('merge' if merge else None)
        if mode is None:
            return dict(ok=False, status='diverged', repo=relname, head=head, commit=fetched,
                        next='pisar sync pull --merge (a merge commit is written, after validation)')
        ident = sync_id(relname, head, fetched, mode, names_[1])
        if mode == 'fast-forward':
            return _start_fast_forward(root, runtime, wiki, repo, relname, names_, head, fetched, ident, dry_run)
        return _start_merge(root, runtime, wiki, repo, relname, names_, head, fetched, ident, dry_run)


def _own_paths(journal):
    """The paths a sync may find dirty when it resumes: its planned paths and their temporary files."""
    ident = journal['operation_id']
    return [item['path'] for item in journal['plan'] or []] + [_temp_name(ident, item['path'])
                                                              for item in journal['plan'] or []]


def _would_resume(repo, journal):
    """The report of a dry run on an open journal: the phase, the steps a resume takes, and the state of each path."""
    result = dict(ok=True, status='would-resume', sync_id=journal['operation_id'], repo=journal['repo'],
                  phase=journal['phase'], steps=[])
    steps = result['steps']
    try:
        _clean(repo, _own_paths(journal))
        if journal['phase'] == 'prepared':
            steps.append('write the merge commit from the journaled tree, move the branch, install the files, pin')
            return result
        if journal['phase'] == 'planned':
            steps.append('move the branch to the journaled commit (compare-and-swap), install the files, pin')
        elif journal['phase'] == 'moved':
            steps.append('install the files, pin the ancestors')
        elif journal['phase'] == 'files':
            steps.append('pin the ancestors')
        result['items'] = [dict(path=item['path'], state=state)
                           for item, state in zip(journal['plan'], classify(repo, journal['plan']))]
    except WikiError as error:
        return dict(ok=False, status='refused', sync_id=journal['operation_id'], repo=journal['repo'],
                    phase=journal['phase'], problems=[str(error)])
    return result


def _start_fast_forward(root, runtime, wiki, repo, relname, names_, head, fetched, ident, dry_run):
    tree = git(repo, 'rev-parse', f'{fetched}^{{tree}}').strip()
    plan = plan_between(repo, head, tree)
    verdict = _verdict(runtime, ident, root, repo, tree, plan)
    if verdict or dry_run:
        remove_scratch(runtime, ident)
        status, problems = verdict or ('would-fast-forward', [])
        return dict(ok=not verdict, status=status, repo=relname, head=head, commit=fetched, problems=problems,
                    changes=[item['path'] for item in plan])
    journal = new_journal(root, relname, repo, names_, 'fast-forward', head, fetched, None, plan, ident)
    journal['target'] = fetched
    store(runtime, journal)
    checkpoint('journal')
    return _run_move(root, runtime, wiki, repo, journal, 'fast-forward')


def _start_merge(root, runtime, wiki, repo, relname, names_, head, fetched, ident, dry_run):
    bases = git(repo, 'merge-base', '--all', head, fetched, check=False).split()
    if len(bases) != 1:
        raise WikiError('the histories have no single common ancestor; sync cannot merge them')
    scratch = scratch_path(runtime, ident, 'merge')
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    spaces = {**_spaces_in(repo, head, ''), **_spaces_in(repo, fetched, '')}
    try:
        clean, conflicts = merge_trees(repo, tree_entries(repo, bases[0]), tree_entries(repo, head),
                                       tree_entries(repo, fetched), scratch,
                                       is_source=lambda path: _is_source(path, spaces))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    unsafe = _unsafe_entries(clean, conflicts)
    if unsafe:  # Nothing of an unsafe remote is written into the work tree or the repository.
        remove_scratch(runtime, ident)
        return dict(ok=False, status='invalid', repo=relname, head=head, commit=fetched, problems=unsafe)
    if conflicts:
        if dry_run:
            return dict(ok=False, status='would-conflict', repo=relname, head=head, commit=fetched,
                        conflicts=[dict(path=c['path'], kind=c['kind']) for c in conflicts])
        journal = new_journal(root, relname, repo, names_, 'merge', head, fetched, bases[0], None, ident)
        journal.update(status='conflicts', phase='conflicts', worktree=True,
                       conflicts=conflict_records(repo, head, fetched, conflicts))
        build_work(repo, runtime, ident, head, clean, conflicts)
        checkpoint('worktree')
        store(runtime, journal)
        checkpoint('conflicts')
        return dict(ok=False, status='conflicts', sync_id=ident, repo=relname, conflicts=_conflict_view(journal),
                    work=str(work_dir(runtime, ident)),
                    next=f'edit the files in {work_dir(runtime, ident)}, take a side with pisar sync take, then '
                         f'pisar sync resolve --id {ident}')
    tree = write_tree(repo, clean)
    plan = plan_between(repo, head, tree)
    verdict = _verdict(runtime, ident, root, repo, tree, plan)
    if verdict or dry_run:
        remove_scratch(runtime, ident)
        status, problems = verdict or ('would-merge', [])
        return dict(ok=not verdict, status=status, repo=relname, head=head, commit=fetched, problems=problems,
                    changes=[item['path'] for item in plan])
    commit_message.require(*_explanation())  # Refused before any journal is written.
    journal = new_journal(root, relname, repo, names_, 'merge', head, fetched, bases[0], plan, ident,
                          phase='prepared')
    journal['tree'] = tree
    store(runtime, journal)  # Prepared: the tree and the plan are journaled before any commit object exists.
    journal['target'] = _merge_commit(repo, runtime, wiki, journal)
    journal['phase'] = 'planned'
    store(runtime, journal)
    checkpoint('journal')
    return _run_move(root, runtime, wiki, repo, journal, 'merge')


def _merge_commit(repo, runtime, wiki, journal):
    """The merge commit of this sync, from the journaled tree. Its message comes from the writer once (cached in the
    journal); its dates come from the journal, so a retry after an interruption rebuilds the same commit object."""
    commit_message.require(*_explanation())
    message = operations.commit_writer(wiki, Store(runtime), journal, repo)(
        dict(repo=repo, old=journal['base'], tree=journal['tree'], paths=[item['path'] for item in journal['plan']]))
    checkpoint('message-ready')
    commit = git(repo, 'commit-tree', journal['tree'], '-p', journal['base'], '-p', journal['fetched'],
                 input=f'{message}\n'.encode(), dates=journal['when']).strip()
    checkpoint('merge-commit')
    return commit


def _explanation():
    current = commit_message.current()
    return current.get('what'), current.get('why')


def _verdict(runtime, ident, root, repo, tree, plan):
    """None when the move may go ahead; otherwise (status, problems) and nothing is written.

    A fetched path that is unsafe, or a candidate that breaks the check rules, is `invalid`; a local state that
    does not match the plan (a child not yet synced, an untracked file in the way) is `refused`."""
    unsafe = unsafe_paths(plan)
    if unsafe:
        return 'invalid', unsafe
    try:
        check_children(root, repo, plan)
        check_files(repo, plan)
    except WikiError as error:
        status = 'needs-submodule-sync' if str(error).startswith('needs-submodule-sync') else 'refused'
        return status, [str(error)]
    problems = _problems(runtime, ident, root, repo, tree, plan)
    return ('invalid', problems) if problems else None


def _problems(runtime, ident, root, repo, tree, plan):
    tree_root, overrides = candidate_root(root, repo, tree, gitops.ancestors(repo, root))
    prefix = '' if repo == root else f'{repo.relative_to(root).as_posix()}/'
    return problems_of(runtime, ident, root, tree_root, overrides, [f'{prefix}{item["path"]}' for item in plan])


def _run_move(root, runtime, wiki, repo, journal, outcome):
    base, target = journal['base'], journal['target']
    move(root, runtime, wiki, repo, journal)
    result = dict(ok=True, status=outcome, sync_id=journal['operation_id'], repo=journal['repo'], head=base,
                  commit=target, **_report(root, repo, base, target))
    discard_worktree_of(runtime, repo, journal)
    return result


def discard_worktree_of(runtime, repo, journal):
    if journal.get('worktree'):
        discard_worktree(repo, work_dir(runtime, journal['operation_id']))
        journal['worktree'] = False
        store(runtime, journal)
    remove_scratch(runtime, journal['operation_id'])


def _resume(root, runtime, wiki, repo, journal):
    """Continue an open journal. Nothing is replayed over user work: the paths are classified first (see classify)."""
    _clean(repo, _own_paths(journal))  # Only the sync's own planned paths and temporary files may be dirty.
    for parent, link, _ in gitops.ancestors(repo, root):
        _clean(parent, [link])  # Only the pin of the child that already moved may differ.
    if journal['phase'] == 'prepared':
        if head_commit(repo) != journal['base']:
            raise WikiError(f'Git HEAD changed outside operation: {journal["repo"]}')
        check_children(root, repo, journal['plan'])
        check_files(repo, journal['plan'])
        journal['target'] = _merge_commit(repo, runtime, wiki, journal)
        journal['phase'] = 'planned'
        store(runtime, journal)
        checkpoint('journal')
    elif journal['phase'] == 'planned' and head_commit(repo) == journal['base']:
        check_children(root, repo, journal['plan'])
        check_files(repo, journal['plan'])
    return _run_move(root, runtime, wiki, repo, journal, journal['mode'])


def _conflict_view(journal):
    return [dict(path=c['path'], kind=c['kind'], sources=c['sources'], ours=c.get('ours'), theirs=c.get('theirs'))
            for c in journal['conflicts']]


def _open_conflict(runtime, ident):
    journal = load(runtime, ident)
    if journal['status'] != 'conflicts':
        raise WikiError(f'sync {ident} is {journal["status"]}, not waiting for conflict resolution')
    return journal


def _unmerged(work):
    """{path: [(stage, mode, oid)]} of the unmerged entries of the worktree WORK."""
    found = {}
    for record in filter(None, git_bytes(work, 'ls-files', '-u', '-z').split(b'\0')):
        meta, path = record.split(b'\t', 1)
        mode, oid, stage = meta.decode().split()
        found.setdefault(path.decode('utf-8', 'surrogateescape'), []).append((int(stage), mode, oid))
    return found


def _index_path(work):
    return Path(git(work, 'rev-parse', '--path-format=absolute', '--git-path', 'index').strip())


def _restage(work, index, path, entry):
    """Make the index of WORK hold exactly ENTRY (mode, oid) at stage 0 for PATH, or nothing when ENTRY is None."""
    git_bytes(work, 'update-index', '--force-remove', '--', path, index=index)
    if entry is not None:
        git_bytes(work, 'update-index', '--add', '--cacheinfo', f'{entry[0]},{entry[1]},{path}', index=index)


def _side_bytes(repo, entry):
    return None if entry is None else read_blobs(repo, [entry[1]])[entry[1]]


def do_take(root, state_dir, ident, path, side):
    """Resolve one conflicted path by taking one side whole (the only way to resolve a source)."""
    check_git()
    root = Path(root).resolve()
    runtime = Runtime(state_dir, root)
    if side not in ('ours', 'theirs'):
        raise WikiError('--side is ours or theirs')
    with runtime.lock():
        journal = _open_conflict(runtime, ident)
        repo = repository(root, journal['repo'])
        conflict = next((c for c in journal['conflicts'] if c['path'] == path), None)
        if conflict is None:
            raise WikiError(f'{path} is not a conflicted path of sync {ident}')
        work = work_dir(runtime, ident)
        stage = '2' if side == 'ours' else '3'
        entry = conflict['stages'][stage]
        index = _index_path(work)
        journal['decided'][path] = side
        if conflict['kind'] == 'submodule':
            if entry is None:
                raise WikiError(f'{path}: the {side} side removes the submodule; sync does not remove submodules')
            journal['choices'][path] = entry[1]
            store(runtime, journal)
            _restage(work, index, path, (GITLINK, entry[1]))
        else:
            store(runtime, journal)
            _restage(work, index, path, entry)
            data = _side_bytes(repo, entry)
            _write_entry(work, path, entry[0] if entry else '100644', data)
        return dict(ok=True, status='taken', sync_id=ident, path=path, side=side,
                    unmerged=sorted(_unmerged(work)))


def do_resolve(root, state_dir, ident):
    """Finish a conflicted merge: the worktree is the result; validate it, then write the merge commit and move."""
    check_git()
    root = Path(root).resolve()
    runtime = Runtime(state_dir, root)
    wiki = Wiki(root)
    with runtime.lock():
        journal = _open_conflict(runtime, ident)
        repo = repository(root, journal['repo'])
        work = work_dir(runtime, ident)
        if not work.is_dir():
            raise WikiError(f'the work tree of sync {ident} is missing; abort it and pull again')
        if head_commit(repo) != journal['base'] or symbolic_head(repo) != journal['local_ref']:
            raise WikiError('the local branch changed since the conflicts were recorded; abort and pull again')
        if commit_of(repo, journal['fetched_ref']) != journal['fetched']:
            raise WikiError('the fetched ref moved since the conflicts were recorded; abort and pull again')
        unmerged = _unmerged(work)
        index = _index_path(work)
        by_path = {c['path']: c for c in journal['conflicts']}
        for path in sorted(unmerged):
            conflict = by_path.get(path)
            if conflict is None:
                raise WikiError(f'{path} is unmerged but not a recorded conflict of sync {ident}')
            if conflict['kind'] in MARKERLESS and path not in journal['decided'] and not conflict['sources']:
                raise WikiError(f'{path}: a {conflict["kind"]} conflict has no markers; take a side with '
                                'pisar sync take')
            if conflict['kind'] == 'submodule':
                _restage(work, index, path, (GITLINK, journal['choices'][path]))
                continue
            file = work / path
            if file.is_symlink():
                raise WikiError(f'{path}: a symbolic link cannot be a resolution')
            if conflict['sources']:
                _resolve_source(repo, work, index, path, conflict, file)
                continue
            if not file.exists():
                _restage(work, index, path, None)
                continue
            text = file.read_bytes().decode('utf-8', 'replace')
            if MARKER.search(text):
                raise WikiError(f'{path}: conflict markers remain; edit the file or take a side with pisar sync take')
            ours = conflict['stages']['2'] or conflict['stages']['3'] or conflict['stages']['1']
            mode = '100755' if os.access(file, os.X_OK) and ours and ours[0] == '100755' else '100644'
            _restage(work, index, path, (mode, hash_blob(repo, file.read_bytes())))
        leftover = _unmerged(work)
        if leftover:
            raise WikiError(f'unmerged entries remain: {", ".join(sorted(leftover))}')
        entries = _worktree_entries(repo, work, index)
        _sources_unchanged(repo, journal, entries)
        tree = write_tree(repo, entries)
        plan = plan_between(repo, journal['base'], tree)
        problems = unsafe_paths(plan) or _problems(runtime, ident, root, repo, tree, plan)
        if problems:
            return dict(ok=False, status='invalid', sync_id=ident, problems=problems,
                        next='fix the files and resolve again, or abort')
        check_children(root, repo, plan)
        check_files(repo, plan)
        commit_message.require(*_explanation())  # Refused before the journal changes state.
        journal.update(plan=plan, tree=tree, status='open', phase='prepared', target=None, when=int(time.time()))
        store(runtime, journal)
        journal['target'] = _merge_commit(repo, runtime, wiki, journal)
        journal['phase'] = 'planned'
        store(runtime, journal)
        checkpoint('journal')
        return _run_move(root, runtime, wiki, repo, journal, 'resolved')


def _sources_unchanged(repo, journal, entries):
    """Every source of the result is exactly one side's bytes (or absent where that side is): never an edit.

    This holds for taken sources, for clean ones and for local ones, whatever the agent or user did in the work
    tree: a source is an immutable original and can only be taken whole."""
    spaces = {**_spaces_in(repo, journal['base'], ''), **_spaces_in(repo, journal['fetched'], '')}
    ours, theirs = tree_entries(repo, journal['base']), tree_entries(repo, journal['fetched'])
    for path in sorted((set(entries) | set(ours) | set(theirs))):
        if not _is_source(path, spaces):
            continue
        final = entries.get(path, (None, None))[1]
        sides = {ours.get(path, (None, None))[1], theirs.get(path, (None, None))[1]}
        if final not in sides:
            raise WikiError(f'{path}: a source is an immutable original and cannot be edited; take one side with '
                            'pisar sync take')


def _resolve_source(repo, work, index, path, conflict, file):
    """A source is taken whole: its bytes must equal one side, or be absent where that side is absent."""
    ours, theirs = conflict['stages']['2'], conflict['stages']['3']
    if not file.exists():
        if ours is None or theirs is None:  # Deleting it is exactly what the side without the file has.
            _restage(work, index, path, None)
            return
        raise WikiError(f'{path}: a source cannot be deleted; take one side with pisar sync take')
    data = file.read_bytes()
    if ours is not None and _side_bytes(repo, ours) == data:
        _restage(work, index, path, ours)
    elif theirs is not None and _side_bytes(repo, theirs) == data:
        _restage(work, index, path, theirs)
    else:
        raise WikiError(f'{path}: a source is an immutable original and cannot be edited; take one side with pisar sync take')


def _worktree_entries(repo, work, index):
    """{path: (mode, oid)} of the resolved worktree: every file as it is now, gitlinks from the index."""
    entries = {}
    for line in filter(None, git_bytes(work, 'ls-files', '-s', '-z', index=index).split(b'\0')):
        meta, path = line.split(b'\t', 1)
        mode, oid, _ = meta.decode().split()
        if mode == '120000':
            raise WikiError(f'{path.decode("utf-8", "surrogateescape")}: symbolic links cannot be synced')
        if mode == GITLINK:
            entries[path.decode('utf-8', 'surrogateescape')] = (mode, oid)
    for directory, names_, files in os.walk(work):
        if Path(directory) == work and '.git' in names_:
            names_.remove('.git')  # The worktree's own Git file: not content.
        for name in files:
            path = Path(directory, name)
            relative = path.relative_to(work).as_posix()
            if path.is_symlink():
                raise WikiError(f'{relative}: symbolic links cannot be synced')
            if relative == '.git':
                continue
            mode = '100755' if os.access(path, os.X_OK) else '100644'
            entries[relative] = (mode, hash_blob(repo, path.read_bytes()))
    return entries


def do_abort(root, state_dir, ident):
    """Drop a sync whose branch has not moved. A prepared or planned sync is dropped (its worktree too); a prepared
    conflicted resolution goes back to editing, with its worktree kept, and a second abort drops it. A sync whose
    branch carries its commit cannot be taken back: a pull finishes it. The repository is never changed here."""
    check_git()
    root = Path(root).resolve()
    runtime = Runtime(state_dir, root)
    with runtime.lock():
        journal = load(runtime, ident)
        if journal['status'] not in ('open', 'conflicts') or journal.get('phase') not in PRE_MOVE_PHASES:
            raise WikiError(f'sync {ident} is {journal["status"]} at {journal.get("phase")}; the branch cannot be '
                            'taken back by abort (pisar sync pull finishes it)')
        repo = repository(root, journal['repo'])
        target = journal.get('target')
        if target and is_ancestor(repo, target, head_commit(repo)):  # The branch carries the sync's commit.
            raise WikiError(f'sync {ident} has moved the branch, which cannot be taken back by abort; '
                            'pisar sync pull finishes it')
        if journal['phase'] == 'prepared' and journal.get('worktree'):
            journal.update(status='conflicts', phase='conflicts', target=None, tree=None, plan=None, when=None,
                           messages={})
            store(runtime, journal)
            return dict(ok=True, status='back-to-conflicts', sync_id=ident, repo=journal['repo'],
                        work=str(work_dir(runtime, ident)),
                        next=f'edit the files in {work_dir(runtime, ident)}, then pisar sync resolve --id {ident}, '
                             f'or pisar sync abort --id {ident} again')
        if journal.get('worktree'):
            discard_worktree(repo, work_dir(runtime, ident))
            journal['worktree'] = False
        remove_scratch(runtime, ident)
        journal['status'] = 'aborted'
        store(runtime, journal)
        return dict(ok=True, status='aborted', sync_id=ident, repo=journal['repo'])


