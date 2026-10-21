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


def prepare_graph(operation, root_candidate, *, message=None):
    """Reconcile every incoming pin with its live child, bottom-up."""
    from . import operations, sync
    operation.pop('_graph_conflicts', None)
    root = Path(operation['root']).resolve()
    live = discover(root, operation['ours'])
    original_urls = {path: url for path, (url, _) in _links(root, operation['ours']).items()}
    incoming_urls = {path: url for path, (url, _) in _links(root, operation['theirs']).items()}
    if original_urls != incoming_urls:
        raise WikiError('incoming submodule topology or URL registration changed')
    root_candidate = Path(root_candidate)
    live_by_path = {item['path']: item for item in live}
    incoming_commits = {'.': operation['theirs']}
    candidates_by_path = {}
    deferred_gitlink_conflicts = set()
    prior_graph = {entry['path']: entry for entry in operation.get('graph', [])}
    # Parent repositories precede descendants in `live`, so each incoming child pin
    # is read from its exact incoming parent commit while its result is staged upward.
    for item in live:
        relative = item['path']
        candidate_path = root_candidate if relative == '.' else root_candidate / relative
        if relative == '.':
            candidates_by_path[relative] = dict(path=relative, candidate=str(candidate_path),
                commit=sync_git.head(candidate_path), branch=item['branch'], ours=item['ours'],
                theirs=operation['theirs'])
            continue
        parent_path = max((name for name in live_by_path if name == '.' or relative.startswith(name + '/')),
                          key=len)
        parent_live = live_by_path[parent_path]
        parent_incoming = incoming_commits[parent_path]
        parent_candidate = root_candidate if parent_path == '.' else Path(candidates_by_path[parent_path]['candidate'])
        remainder = relative if parent_path == '.' else relative[len(parent_path) + 1:]
        incoming_pin = _pin_at(parent_live['repo'], remainder, parent_incoming)
        incoming_commits[relative] = incoming_pin
        source = item['repo']
        upstream = sync_git.text(source, 'for-each-ref', '--format=%(upstream)',
                                 f'refs/heads/{item["branch"].removeprefix("refs/heads/")}').strip()
        if not upstream:
            raise WikiError(f'submodule has no configured upstream: {relative}')
        remote, _, branch = upstream.removeprefix('refs/remotes/').partition('/')
        if not branch:
            raise WikiError(f'submodule upstream is ambiguous: {relative}')
        target = sync_git.fetch_tip(source, remote, branch)
        if not sync_git._ancestor(source, incoming_pin, target):
            raise WikiError(f'incoming submodule pin is unavailable from its upstream: {relative}')
        original = {path: url for path, (url, _) in _links(source, item['ours']).items()}
        incoming = {path: url for path, (url, _) in _links(source, incoming_pin).items()}
        if original != incoming:
            raise WikiError(f'incoming submodule topology or URL registration changed: {relative}')
        if sync_git._ancestor(source, item['ours'], incoming_pin):
            commit = incoming_pin
        elif sync_git._ancestor(source, incoming_pin, item['ours']):
            commit = item['ours']
        else:
            commit = None
        if candidate_path.exists():
            if candidate_path.is_symlink() or not candidate_path.is_dir():
                raise WikiError(f'candidate submodule path is unsafe: {relative}')
            top = Path(sync_git.text(candidate_path, 'rev-parse', '--show-toplevel', check=False).strip()).resolve()
            gitdir = Path(sync_git.text(candidate_path, 'rev-parse', '--absolute-git-dir', check=False).strip()).resolve()
            if top != candidate_path.resolve() or not gitdir.is_relative_to(candidate_path.resolve()):
                if top == candidate_path.resolve() and sync_git.text(candidate_path, 'status', '--porcelain=v1', '-z',
                        '--untracked-files=all', '--ignore-submodules=none'):
                    raise WikiError(f'candidate submodule has manual edits and cannot be rematerialized: {relative}')
                deinit = subprocess.run(sync_git.command(parent_candidate, 'submodule', 'deinit', '--force', '--', remainder),
                                        capture_output=True, env=gitops.environment())
                if deinit.returncode:
                    raise WikiError(f'could not deinitialize candidate submodule checkout: {relative}')
                git_marker = candidate_path / '.git'
                if git_marker.is_file():
                    git_marker.unlink()
                if candidate_path.exists() and candidate_path.is_dir():
                    if any(candidate_path.iterdir()):
                        raise WikiError(f'candidate submodule checkout did not deinitialize cleanly: {relative}')
                    candidate_path.rmdir()
                candidate_path.parent.mkdir(parents=True, exist_ok=True)
                clone = subprocess.run(['git', 'clone', '--no-local', '--no-checkout', '--',
                                        str(source), str(candidate_path)], capture_output=True,
                                       env=gitops.environment())
                if clone.returncode:
                    raise WikiError(f'could not materialize ordinary candidate submodule: {relative}')
        else:
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(['git', 'clone', '--no-local', '--no-checkout', '--', str(source), str(candidate_path)],
                                 capture_output=True, env=gitops.environment())
            if proc.returncode:
                raise WikiError(f'could not clone candidate submodule {relative}')
        sync_git._identity(source, candidate_path)
        sync_git.text(candidate_path, 'fetch', '--no-tags', '--no-recurse-submodules', '--',
                      str(source), item['ours'], incoming_pin)
        candidate_head = sync_git.head(candidate_path)
        candidate_urls = {path: url for path, (url, _) in _links(candidate_path, candidate_head).items()}
        if candidate_urls != original:
            raise WikiError(f'candidate submodule topology or URL registration changed: {relative}')
        existing_merge = sync_git.text(candidate_path, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}', check=False).strip()
        prior_candidate = prior_graph.get(relative, {}).get('candidate')
        if (candidate_head != item['ours'] and prior_candidate
                and sync_git._ancestor(candidate_path, prior_candidate, candidate_head)
                and not sync_git.text(candidate_path, 'status', '--porcelain=v1', '-z',
                                       '--untracked-files=all', '--ignore-submodules=none')):
            commit = candidate_head
        elif candidate_head != item['ours'] and sync_git.parents(candidate_path, candidate_head) == (item['ours'], incoming_pin):
            commit = candidate_head
        elif commit is None and existing_merge == incoming_pin:
            unmerged = sync_git.run(candidate_path, 'ls-files', '--unmerged', '-z')
            if unmerged:
                if _registered_gitlink_conflict(unmerged, relative, live_by_path):
                    candidates_by_path[relative] = dict(path=relative, candidate=str(candidate_path),
                        commit=candidate_head, branch=item['branch'], ours=item['ours'], theirs=incoming_pin)
                    deferred_gitlink_conflicts.add(relative)
                    continue
                operation['_graph_conflicts'] = [dict(path=relative, candidate=str(candidate_path),
                    unmerged=unmerged.decode('utf-8', errors='surrogateescape').split('\0'))]
                return CandidateGraph(root_candidate, ())
            sync_git.text(candidate_path, 'diff', '--cached', '--check')
            commit = sync_git.commit_merge(candidate_path, message=message)
        elif commit is None:
            sync_git.text(candidate_path, 'switch', '--detach', item['ours'])
            merge = subprocess.run(sync_git.command(candidate_path, 'merge', '--no-ff', '--no-commit',
                '--no-edit', '--no-autostash', '--no-overwrite-ignore', '-s', 'ort', incoming_pin),
                capture_output=True, env=gitops.environment())
            merge_head = sync_git.text(candidate_path, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}', check=False).strip()
            if merge_head != incoming_pin:
                status_before = sync_git.text(candidate_path, 'status', '--porcelain=v1', check=False).strip()
                raise WikiError(f'could not merge incoming submodule history: {relative}: {merge.stderr.decode(errors="replace").strip()}; status={status_before}')
            if sync_git.run(candidate_path, 'ls-files', '--unmerged', '-z'):
                unmerged = sync_git.run(candidate_path, 'ls-files', '--unmerged', '-z')
                if _registered_gitlink_conflict(unmerged, relative, live_by_path):
                    candidates_by_path[relative] = dict(path=relative, candidate=str(candidate_path),
                        commit=candidate_head, branch=item['branch'], ours=item['ours'], theirs=incoming_pin)
                    deferred_gitlink_conflicts.add(relative)
                    continue
                operation['_graph_conflicts'] = [dict(path=relative, candidate=str(candidate_path),
                    unmerged=unmerged.decode('utf-8', errors='surrogateescape').split('\0'))]
                return CandidateGraph(root_candidate, ())
            sync_git.text(candidate_path, 'diff', '--cached', '--check')
            commit = sync_git.commit_merge(candidate_path, message=message)
        else:
            sync_git.text(candidate_path, 'switch', '--detach', commit)
        sync_git.text(parent_candidate, 'add', '--', remainder)
        unresolved = sync_git.run(parent_candidate, 'ls-files', '--unmerged', '-z')
        if not unresolved:
            merge_head = sync_git.text(parent_candidate, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}', check=False).strip()
            staged = subprocess.run(sync_git.command(parent_candidate, 'diff', '--cached', '--quiet'),
                                    capture_output=True, env=gitops.environment()).returncode != 0
            if merge_head:
                parent_commit = sync_git.commit_merge(parent_candidate, message=message)
            elif staged:
                sync_git.text(parent_candidate, 'diff', '--cached', '--check')
                sync_git.run(parent_candidate, 'commit', '--no-gpg-sign', '-F', '-',
                             input=(message(parent_candidate) if callable(message) else 'wiki: sync pin fixup').encode())
                parent_commit = sync_git.head(parent_candidate)
            else:
                parent_commit = sync_git.head(parent_candidate)
            candidates_by_path[parent_path]['commit'] = parent_commit
            if parent_path == '.':
                operation['candidate'] = parent_commit
        candidates_by_path[relative] = dict(path=relative, candidate=str(candidate_path), commit=commit,
            branch=item['branch'], ours=item['ours'], theirs=incoming_pin)

    # A descendant pin may have completed a deferred parent merge. Publish the
    # reconciled parent pin upward only after its selected commit is known.
    for relative in sorted(deferred_gitlink_conflicts, key=lambda value: value.count('/'), reverse=True):
        item = candidates_by_path[relative]
        parent_path = max((name for name in live_by_path if name == '.' or relative.startswith(name + '/')),
                          key=len)
        parent_candidate = root_candidate if parent_path == '.' else Path(candidates_by_path[parent_path]['candidate'])
        remainder = relative if parent_path == '.' else relative[len(parent_path) + 1:]
        sync_git.text(parent_candidate, 'add', '--', remainder)
        if sync_git.run(parent_candidate, 'ls-files', '--unmerged', '-z'):
            conflicts = sync_git.run(parent_candidate, 'ls-files', '--unmerged', '-z')
            operation['_graph_conflicts'] = [dict(path=parent_path, candidate=str(parent_candidate),
                unmerged=conflicts.decode('utf-8', errors='surrogateescape').split('\0'))]
            return CandidateGraph(root_candidate, ())
        merge_head = sync_git.text(parent_candidate, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}', check=False).strip()
        if merge_head:
            parent_commit = sync_git.commit_merge(parent_candidate, message=message)
        else:
            sync_git.text(parent_candidate, 'diff', '--cached', '--check')
            sync_git.run(parent_candidate, 'commit', '--no-gpg-sign', '-F', '-',
                         input=(message(parent_candidate) if callable(message) else 'wiki: sync pin fixup').encode())
            parent_commit = sync_git.head(parent_candidate)
        candidates_by_path[parent_path]['commit'] = parent_commit
        if parent_path == '.':
            operation['candidate'] = parent_commit

    if sync_git.run(root_candidate, 'ls-files', '--unmerged', '-z'):
        return CandidateGraph(root_candidate, ())
    candidates = []
    for item in live:
        entry = candidates_by_path[item['path']]
        candidate_path = Path(entry['candidate'])
        entry['tree'] = sync_git.text(candidate_path, 'rev-parse', f'{entry["commit"]}^{{tree}}').strip()
        candidates.append(entry)
    graph = CandidateGraph(root_candidate, tuple(candidates))
    for item in candidates:
        source = root if item['path'] == '.' else live_by_path[item['path']]['repo']
        original_urls = {path: url for path, (url, _) in _links(source, item['ours']).items()}
        candidate_urls = {path: url for path, (url, _) in _links(item['candidate'], item['commit']).items()}
        if original_urls != candidate_urls:
            raise WikiError(f'candidate submodule topology or URL registration changed: {item["path"]}')
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


def _registered_gitlink_conflict(unmerged, relative, live_by_path):
    rows = [row for row in unmerged.split(b'\0') if row]
    if not rows:
        return False
    direct = {name[len(relative) + 1:] for name in live_by_path
              if name != relative and name.startswith(relative + '/')
              and max((parent for parent in live_by_path if parent == '.' or name.startswith(parent + '/')),
                      key=len) == relative}
    paths = set()
    for row in rows:
        try:
            metadata, raw_path = row.split(b'\t', 1)
            mode = metadata.split()[0]
        except (ValueError, IndexError):
            return False
        path = raw_path.decode('utf-8', 'surrogateescape')
        if mode != b'160000' or path not in direct:
            return False
        paths.add(path)
    return bool(paths)


def _pin_at(root, relative, commit='HEAD'):
    spec = f'{commit}:{relative}'
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
        sync_git.observe(repo, allow_dirty=True)
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
            raise WikiError(f'live repository has unexpected edits before application: {path}: expected={expected}; actual={actual}; staged={staged}')
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
            direct = [name for name in by_path if name != '.' and _parent_path(name, by_path) == '.']
            expected_paths = {name for name in direct if current == item['ours'] and any(
                done in changed_paths and (done == name or done.startswith(name + '/')) for done in applied)}
            actual_paths = {line[3:] for line in status.splitlines()}
            if actual_paths != expected_paths or any(line[:2] not in (' M', 'M ', 'MM') for line in status.splitlines()):
                raise WikiError(f'unexpected parent edits during graph application: {status.strip()}')
            snapshot_head = current
            snapshot_branch = branch
        else:
            descendants = {done if path == '.' else done[len(path) + 1:] for done in applied
                           if done in changed_paths and _parent_path(done, by_path) == path}
            if descendants and sync_git.head(repo) == item['ours']:
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
            direct_children = [name for name in by_path if name != '.' and _parent_path(name, by_path) == parent_path]
            expected_paths = {name if parent_path == '.' else name[len(parent_path) + 1:]
                              for name in direct_children if any(done in changed_paths and
                                  (done == name or done.startswith(name + '/')) for done in applied)}
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
