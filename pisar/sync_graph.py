"""Git-owned candidate graphs for registered, initialized wiki submodules."""
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
import subprocess

from . import gitops, sync_git
from .documents import check
from .safety import WikiError
from .spaces import Wiki


@dataclass(frozen=True)
class CandidateGraph:
    root: Path
    repositories: tuple[dict, ...]


def _run(repo, *args, check=True):
    return sync_git.run(repo, *args, check=check)


def _links(repo, commit='HEAD'):
    """Read registered paths and pins from Git trees and .gitmodules via Git config."""
    records = _run(repo, 'ls-tree', '-r', '-z', commit).split(b'\0')
    pins = {}
    for record in records:
        if record.startswith(b'160000 '):
            head, path = record.split(b'\t', 1)
            pins[path.decode('utf-8', 'surrogateescape')] = head.decode().split()[2]
    manifest = _run(repo, 'show', f'{commit}:.gitmodules', check=False)
    if not manifest:
        if pins:
            raise WikiError('gitlinks have no pinned .gitmodules manifest')
        return {}
    # `git config --null --list` gives key\nvalue NUL records and handles quoted values.
    result = subprocess.run(sync_git.command(repo, 'config', '--null', '--file', '-', '--list'),
                            input=manifest, capture_output=True, env=gitops.environment())
    if result.returncode:
        raise WikiError('pinned .gitmodules manifest is invalid')
    configured = {}
    for item in result.stdout.split(b'\0'):
        if not item:
            continue
        key, sep, value = item.partition(b'\n')
        if not sep:
            raise WikiError('Git returned an invalid .gitmodules config record')
        key = key.decode('utf-8', 'surrogateescape')
        value = value.decode('utf-8', 'surrogateescape')
        if key.endswith('.path'):
            configured.setdefault(key[:-5], {})['path'] = value
        elif key.endswith('.url'):
            configured.setdefault(key[:-4], {})['url'] = value
    paths = {}
    for entry in configured.values():
        parts = PurePosixPath(entry.get('path', '')).parts
        if (set(entry) != {'path', 'url'} or entry['path'] in paths or not parts
                or PurePosixPath(entry['path']).is_absolute() or any(part in ('', '.', '..') for part in parts)):
            raise WikiError('pinned .gitmodules has incomplete or duplicate registrations')
        paths[entry['path']] = entry['url']
    if set(paths) != set(pins):
        raise WikiError('candidate submodule topology differs from its pinned manifest')
    return {path: (url, pins[path]) for path, url in paths.items()}


def discover(root, commit='HEAD'):
    """Return the existing initialized graph, refusing topology changes and bad live states."""
    root = Path(root).resolve()
    result = []
    pending = [(root, '.', commit, None)]
    seen = set()
    while pending:
        repo, relative, pin, parent = pending.pop(0)
        snapshot = sync_git.observe(repo)
        if parent is not None and snapshot.head != parent:
            raise WikiError(f'live submodule is not pinned by its parent: {relative}')
        if relative == '.' and snapshot.head != pin:
            raise WikiError('root commit changed during graph observation')
        if str(repo) in seen:
            raise WikiError('submodule graph contains a repository cycle or duplicate')
        seen.add(str(repo))
        record = dict(path=relative, repo=repo, branch=snapshot.branch, ours=snapshot.head)
        result.append(record)
        for path, (url, child_pin) in sorted(_links(repo, snapshot.head).items()):
            child = (repo / path).resolve()
            if not child.is_relative_to(root) or not child.is_dir() or child.is_symlink():
                raise WikiError(f'uninitialized or unsafe submodule: {relative}/{path}')
            top = Path(sync_git.text(child, 'rev-parse', '--show-toplevel').strip()).resolve()
            if top != child:
                raise WikiError(f'submodule is not its own repository root: {relative}/{path}')
            pending.append((child, f'{relative}/{path}' if relative != '.' else path,
                            child_pin, child_pin))
    return tuple(result)


def prepare_graph(operation, root_candidate):
    """Materialize exact child pins beneath an ordinary root candidate clone."""
    from . import operations
    root = Path(operation['root']).resolve()
    live = discover(root, operation['ours'])
    candidates = []
    live_by_path = {item['path']: item for item in live}
    root_candidate = Path(root_candidate)
    for item in live:
        relative = item['path']
        candidate_path = root_candidate if relative == '.' else root_candidate / relative
        if relative == '.':
            commit = operation['candidate']
        else:
            parent_path = max((name for name in live_by_path if name == '.' or relative.startswith(name + '/')),
                              key=len)
            parent_candidate = root_candidate if parent_path == '.' else root_candidate / parent_path
            remainder = relative if parent_path == '.' else relative[len(parent_path) + 1:]
            commit = _pin_at(parent_candidate, remainder)
        if relative != '.':
            source = item['repo']
            # Use the owner's configured upstream for this attached branch; never infer by name.
            upstream = sync_git.text(source, 'for-each-ref', '--format=%(upstream)',
                                     f'refs/heads/{item["branch"].removeprefix("refs/heads/")}').strip()
            if not upstream:
                raise WikiError(f'submodule has no configured upstream: {relative}')
            remote, _, branch = upstream.removeprefix('refs/remotes/').partition('/')
            if not branch:
                raise WikiError(f'submodule upstream is ambiguous: {relative}')
            target = sync_git.fetch_tip(source, remote, branch)
            if not sync_git._ancestor(source, commit, target):
                raise WikiError(f'incoming submodule pin is unavailable from its upstream: {relative}')
            if candidate_path.exists():
                if candidate_path.is_symlink() or not candidate_path.is_dir() or any(candidate_path.iterdir()):
                    raise WikiError(f'candidate submodule path already exists: {relative}')
                candidate_path.rmdir()
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(['git', 'clone', '--no-local', '--no-checkout', '--', str(source), str(candidate_path)],
                                 capture_output=True, env=gitops.environment())
            if proc.returncode:
                raise WikiError(f'could not clone candidate submodule {relative}')
            sync_git.text(candidate_path, 'fetch', '--no-tags', '--no-recurse-submodules', '--',
                          str(source), commit)
            sync_git.text(candidate_path, 'switch', '--detach', commit)
            # Keep the child's live branch identity for exact live application.
            candidates.append(dict(path=relative, candidate=str(candidate_path), commit=commit,
                                   tree=sync_git.text(candidate_path, 'rev-parse', f'{commit}^{{tree}}').strip(),
                                   branch=item['branch'], ours=item['ours'], theirs=commit))
    candidates.insert(0, dict(path='.', candidate=str(root_candidate), commit=operation['candidate'],
                              tree=sync_git.text(root_candidate, 'rev-parse', f'{operation["candidate"]}^{{tree}}').strip(),
                              branch=live[0]['branch'], ours=live[0]['ours'], theirs=operation['theirs']))
    graph = CandidateGraph(root_candidate, tuple(candidates))
    from . import sync
    for item in candidates[1:]:
        child_candidate = Path(item['candidate'])
        sync._task_changes(child_candidate, item['ours'], item['theirs'], item['commit'])
        sync._validate_originals(child_candidate, item['ours'], item['theirs'], item['commit'])
    result = verify_graph(graph)
    if not result['ok']:
        raise WikiError('candidate structural check failed: ' + '; '.join(result['errors']))
    operation['graph'] = [dict(path=x['path'], candidate=x['commit'], tree=x['tree'],
                               branch=x['branch'], ours=x['ours'], theirs=x['theirs']) for x in candidates]
    operation['tree'] = candidates[0]['tree']
    operation['revision'] = operations.fingerprint({key: operation.get(key) for key in
        ('root', 'repo', 'branch', 'remote', 'target_branch', 'remote_fingerprint', 'kind', 'ours',
         'theirs', 'candidate', 'tree', 'graph')})
    return graph


def _pin_at(root, relative):
    spec = f'HEAD:{relative}'
    value = sync_git.text(root, 'rev-parse', '--verify', spec, check=False).strip()
    if not value:
        raise WikiError(f'candidate omits registered submodule: {relative}')
    return value


def _parent_path(path, graph_paths):
    parents = [name for name in graph_paths if name == '.' or path.startswith(name + '/')]
    return max(parents, key=len)


def verify_graph(candidate_graph):
    root = Path(candidate_graph.root)
    expected = {item['path'] for item in candidate_graph.repositories}
    by_path = {item['path']: item for item in candidate_graph.repositories}
    actual = set()
    for item in candidate_graph.repositories:
        repo = Path(item['candidate'])
        if sync_git.head(repo) != item['commit']:
            raise WikiError(f'candidate submodule HEAD changed: {item["path"]}')
        if sync_git.text(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
                         '--ignore-submodules=none'):
            raise WikiError(f'candidate repository is dirty: {item["path"]}')
        if item['path'] != '.':
            parent = max((name for name in by_path if name == '.' or item['path'].startswith(name + '/')),
                         key=len)
            parent_repo = Path(by_path[parent]['candidate'])
            remainder = item['path'] if parent == '.' else item['path'][len(parent) + 1:]
            if _pin_at(parent_repo, remainder) != item['commit']:
                raise WikiError(f'candidate parent pin mismatch: {item["path"]}')
        for child_path in _links(repo, item['commit']):
            full = child_path if item['path'] == '.' else f'{item["path"]}/{child_path}'
            actual.add(full)
    if actual != expected - {'.'}:
        raise WikiError('candidate submodule topology differs from the observed graph')
    report = check(Wiki(root))
    return report


def apply_graph(operation, revision, graph=None):
    """Apply the accepted commit vector child-first and resume only exact recorded tips."""
    from .safety import WikiError
    if revision != operation.get('revision'):
        raise WikiError('candidate graph revision changed')
    root = Path(operation['root'])
    by_path = {item['path']: item for item in operation.get('graph', [])}
    # Check every repository before the first FF. On retry, allow only exact child
    # commits already applied by this operation and their corresponding parent dirt.
    heads = {}
    for item in operation.get('graph', []):
        path = item['path']
        repo = root if path == '.' else root / path
        branch = sync_git.text(repo, 'symbolic-ref', '--quiet', 'HEAD', check=False).strip()
        head = sync_git.head(repo)
        if branch != item['branch'] or head not in (item['ours'], item['candidate']):
            raise WikiError(f'live graph changed before application: {path}')
        heads[path] = head
    for item in operation.get('graph', []):
        path = item['path']
        repo = root if path == '.' else root / path
        expected = set()
        for child_path, child in by_path.items():
            if child_path == '.' or _parent_path(child_path, by_path) != path:
                continue
            child_relative = child_path if path == '.' else child_path[len(path) + 1:]
            pin = _pin_at(repo, child_relative)
            if pin != heads[child_path]:
                expected.add(child_relative)
        status = sync_git.text(repo, 'status', '--porcelain=v1', '--ignore-submodules=none').splitlines()
        actual = {line[3:] for line in status}
        staged = subprocess.run(sync_git.command(repo, 'diff', '--cached', '--quiet'),
                                capture_output=True, env=gitops.environment()).returncode
        if staged or actual != expected or any(line[:2] not in (' M', 'M ', 'MM') for line in status):
            raise WikiError(f'live repository has unexpected edits before application: {path}')
    items = list(reversed(operation.get('graph', [])))
    changed_paths = {item['path'] for item in operation.get('graph', [])
                     if item['path'] != '.' and item['candidate'] != item['ours']}
    applied = []
    for item in items:
        path = item['path']
        repo = root if path == '.' else root / path
        if path == '.':
            branch = sync_git.text(repo, 'symbolic-ref', '--quiet', 'HEAD').strip()
            current = sync_git.head(repo)
            if branch != item['branch']:
                raise WikiError('live parent branch changed after review')
            status = sync_git.text(repo, 'status', '--porcelain=v1', '--ignore-submodules=none')
            expected_paths = {done for done in applied if done in changed_paths and done != '.'}
            actual_paths = {line[3:] for line in status.splitlines()}
            if actual_paths != expected_paths or any(line[:2] not in (' M', 'M ', 'MM') for line in status.splitlines()):
                raise WikiError(f'unexpected parent edits during graph application: {status.strip()}')
            snapshot_head = current
            snapshot_branch = branch
        else:
            descendants = {done if path == '.' else done[len(path) + 1:] for done in applied
                           if done in changed_paths and _parent_path(done, by_path) == path}
            if descendants:
                branch = sync_git.text(repo, 'symbolic-ref', '--quiet', 'HEAD').strip()
                snapshot_head = sync_git.head(repo)
                status = sync_git.text(repo, 'status', '--porcelain=v1', '--ignore-submodules=none').splitlines()
                actual_paths = {line[3:] for line in status}
                if actual_paths != descendants or any(line[:2] not in (' M', 'M ', 'MM') for line in status):
                    raise WikiError(f'unexpected submodule edits during graph application: {path}: {status}')
                if branch != item['branch']:
                    raise WikiError(f'live branch changed after review: {path}')
                snapshot_branch = branch
            else:
                snapshot = sync_git.observe(repo)
                if snapshot.branch != item['branch']:
                    raise WikiError(f'live branch changed after review: {path}')
                snapshot_head = snapshot.head
                snapshot_branch = snapshot.branch
        if path != '.' and snapshot_branch != item['branch']:
            raise WikiError(f'live branch changed after review: {path}')
        if snapshot_head == item['candidate']:
            applied.append(path)
            continue
        if snapshot_head != item['ours']:
            raise WikiError(f'live repository changed during graph application: {path}; already applied: {applied}')
        if path != '.':
            parent_path = _parent_path(path, by_path)
            parent_repo = root if parent_path == '.' else root / parent_path
            status = sync_git.text(parent_repo, 'status', '--porcelain=v1', '--ignore-submodules=none').splitlines()
            expected_paths = {done if parent_path == '.' else done[len(parent_path) + 1:] for done in applied
                              if done in changed_paths and _parent_path(done, by_path) == parent_path}
            actual_paths = {line[3:] for line in status}
            if actual_paths != expected_paths or any(line[:2] not in (' M', 'M ', 'MM') for line in status):
                raise WikiError(f'unexpected parent edits during graph application: {parent_path}')
        candidate = Path(operation['_candidate_root']) if path == '.' else Path(operation['_candidate_root']) / path
        try:
            if path == '.':
                _apply_parent(operation, item, repo, candidate)
            else:
                _apply_child(item, repo, candidate)
        except WikiError as error:
            raise WikiError(f'partial graph application; applied {applied}; pending {path}: {error}') from None
        applied.append(path)
    return dict(status='applied', repositories=applied, head=operation['candidate'])


def _apply_parent(operation, item, repo, candidate):
    sync_git.text(repo, 'fetch', '--no-tags', '--no-recurse-submodules', '--', str(candidate), item['candidate'])
    imported = sync_git.text(repo, 'rev-parse', '--verify', 'FETCH_HEAD^{commit}').strip()
    if imported != item['candidate']:
        raise WikiError('parent candidate import did not resolve to the accepted commit')
    if not sync_git._ancestor(repo, item['ours'], imported):
        raise WikiError('parent candidate is not a fast-forward')
    sync_git.text(repo, 'merge', '--ff-only', '--no-autostash', '--no-overwrite-ignore', '--no-edit', imported)
    if sync_git.head(repo) != imported:
        raise WikiError('live parent did not reach accepted commit')


def _apply_child(item, repo, candidate):
    sync_git.text(repo, 'fetch', '--no-tags', '--no-recurse-submodules', '--', str(candidate), item['candidate'])
    imported = sync_git.text(repo, 'rev-parse', '--verify', 'FETCH_HEAD^{commit}').strip()
    if imported != item['candidate']:
        raise WikiError(f'candidate import did not resolve to the accepted commit: {item["path"]}')
    if not sync_git._ancestor(repo, item['ours'], imported):
        raise WikiError(f'candidate is not a fast-forward: {item["path"]}')
    sync_git.text(repo, 'merge', '--ff-only', '--no-autostash', '--no-overwrite-ignore', '--no-edit', imported)
    if sync_git.head(repo) != imported:
        raise WikiError(f'live repository did not reach accepted commit: {item["path"]}')
