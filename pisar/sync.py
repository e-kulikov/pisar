"""Delivery-I single-repository Git sync lifecycle."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid
from urllib.parse import urlsplit

from . import commit_message, gitops, operations, settings, sync_git, sync_graph
from .documents import check
from .safety import WikiError, concrete_id, sha256
from .spaces import Wiki


@dataclass(frozen=True)
class Target:
    remote: str = 'origin'
    branch: str = 'main'
    bootstrap_map: dict | None = None


def _path(runtime, ident):
    concrete_id(ident, 'operation_id')
    return runtime.path / 'sync' / f'{ident}.json'


def _candidate(runtime, ident):
    return runtime.path / 'candidates' / ident


def _assert_candidate(operation, candidate):
    if sync_git.head(candidate) != operation.get('candidate'):
        raise WikiError('candidate commit changed after preparation')
    tree = sync_git.text(candidate, 'rev-parse', f"{operation['candidate']}^{{tree}}").strip()
    if tree != operation.get('tree'):
        raise WikiError('candidate tree changed after preparation')
    if sync_git.text(candidate, 'status', '--porcelain=v1', '-z', '--untracked-files=all'):
        raise WikiError('candidate has uncommitted changes')
    if operation.get('graph'):
        graph = sync_graph.CandidateGraph(Path(candidate), tuple(
            dict(path=item['path'], candidate=str(candidate) if item['path'] == '.' else str(Path(candidate) / item['path']),
                 commit=item['candidate'], tree=item['tree'], branch=item['branch'], ours=item['ours'])
            for item in operation['graph']))
        sync_graph.verify_graph(graph)


def _load(runtime, ident):
    path = _path(runtime, ident)
    if not path.is_file():
        raise WikiError(f'unknown sync operation: {ident}')
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('operation_id') != ident or value.get('root') != str(runtime.root):
        raise WikiError('sync operation root/identity mismatch')
    return value


def _save(runtime, operation):
    operations.write_json(_path(runtime, operation['operation_id']), operation)


def _repo(wiki, relative):
    if relative not in ('.', ''):
        raise WikiError('sync operates on the complete wiki root')
    if gitops.run(wiki.root, 'rev-parse', '--show-toplevel').strip() != str(wiki.root):
        raise WikiError('sync wiki must be the top of its Git repository')
    return wiki.root


def _remote_fingerprint(repo, remote, *, push=False):
    if push and sync_git.text(repo, 'config', '--bool', '--get', f'remote.{remote}.mirror', check=False).strip() == 'true':
        raise WikiError('mirror-configured publication destinations are refused')
    args = ('remote', 'get-url', '--push', '--all', remote) if push else ('remote', 'get-url', remote)
    output = sync_git.text(repo, *args).splitlines()
    if not output or (push and len(output) != 1):
        raise WikiError('sync requires one unambiguous remote destination')
    return sha256(output[0].encode())


def _target_ref(repo, target):
    if not target.remote or target.remote.startswith('-') or not re.fullmatch(r'[A-Za-z0-9._/-]+', target.branch):
        raise WikiError('invalid sync destination')
    tracking = f'refs/remotes/{target.remote}/{target.branch}'
    for ref in (f'refs/heads/{target.branch}', tracking):
        if subprocess.run(sync_git.command(repo, 'check-ref-format', ref), capture_output=True,
                          env=gitops.environment()).returncode:
            raise WikiError('invalid sync branch/ref')
    return tracking


def _task_changes(candidate, ours, theirs, result, *, wiki_root=None, relative=None):
    changed = set()
    history_commits = set()
    for before, after in ((ours, result), (theirs, result)):
        raw = sync_git.run(candidate, 'diff', '--name-only', '-z', before, after)
        changed.update(p.decode('utf-8', errors='surrogateescape') for p in raw.split(b'\0') if p)
    for base in (ours, theirs):
        commits = sync_git.text(candidate, 'rev-list', '--reverse', f'{base}..{result}').splitlines()
        for commit in commits:
            history_commits.add(commit)
            raw = sync_git.run(candidate, 'diff-tree', '--root', '--no-commit-id', '--name-only', '-r', '-z',
                               '-m', commit)
            changed.update(p.decode('utf-8', errors='surrogateescape') for p in raw.split(b'\0') if p)
    if any('.ruwana' in Path(name).parts for name in changed):
        from .ruwana import Ruwana
        binary = settings.ruwana()
        fields = ('id', 'space_address', 'project', 'source', 'title', 'description', 'due', 'status', 'path',
                  'created', 'modified', 'related', 'tags', 'tasks')

        def snapshot(commit):
            with tempfile.TemporaryDirectory(prefix='pisar-task-snapshot-') as directory:
                if wiki_root is None:
                    checkout = Path(directory) / 'repo'
                    sync_git.run(candidate, 'clone', '--no-local', '--no-checkout', str(candidate), str(checkout))
                    sync_git.run(checkout, 'checkout', '--detach', commit)
                    task_root = checkout
                    selected_spaces = None
                else:
                    checkout = Path(directory) / 'wiki'
                    shutil.copytree(wiki_root, checkout, ignore=shutil.ignore_patterns('.git'))
                    task_repo = checkout / relative
                    if task_repo.is_symlink() or not task_repo.is_dir():
                        raise WikiError('task-validation-unavailable: owning submodule path is missing/unsafe')
                    shutil.rmtree(task_repo)
                    sync_git.run(candidate, 'clone', '--no-local', '--no-checkout', str(candidate), str(task_repo))
                    sync_git.run(task_repo, 'checkout', '--detach', commit)
                    task_root = checkout
                try:
                    from .spaces import Wiki
                    wiki = Wiki(checkout)
                    if wiki_root is None:
                        addresses = None
                    else:
                        addresses = [space.address for space in wiki.spaces
                                     if space.path.is_relative_to(checkout / relative)]
                        if not addresses:
                            raise WikiError('task-validation-unavailable: no owning space in candidate wiki graph')
                    tasks = Ruwana(wiki, binary).snapshot(addresses).tasks
                except WikiError as error:
                    raise WikiError(f'task-validation-unavailable: {error}') from None
                result_tasks = {}
                for task in tasks:
                    value = {field: getattr(task, field) for field in fields}
                    if wiki_root is not None:
                        value['path'] = Path(value['path']).relative_to(Path(relative)).as_posix()
                    result_tasks[(task.space_address, task.id)] = value
                return result_tasks

        try:
            base_commits = sync_git.text(candidate, 'merge-base', '--all', ours, theirs).splitlines()
            if not base_commits:
                raise WikiError('task-validation-unavailable: no common task history')
            versions = {commit: snapshot(commit)
                        for commit in set([*base_commits, ours, theirs, result, *history_commits])}
            known_paths = {task['path'] for tasks in versions.values() for task in tasks.values()}
            changed_task_paths = {name for name in changed if '.ruwana' in Path(name).parts}
            if not changed_task_paths <= known_paths:
                raise WikiError('task-validation-unavailable: changed task file has no Ruwana task identity')
            for base in base_commits:
                base_tasks, ours_tasks = versions[base], versions[ours]
                theirs_tasks, candidate_tasks = versions[theirs], versions[result]
                expected = _merge_task_snapshots(base_tasks, ours_tasks, theirs_tasks)
                if expected != candidate_tasks:
                    raise WikiError('candidate task state is not the coherent three-way result')
        except WikiError as error:
            if 'task-validation-unavailable' in str(error) or 'task ' in str(error):
                raise
            raise WikiError(f'task-validation-unavailable: {error}') from None


def _merge_task_snapshots(base_tasks, ours_tasks, theirs_tasks):
    """Compute the only task state a clean three-way candidate may contain."""
    fields = ('id', 'space_address', 'project', 'source', 'title', 'description', 'due', 'status', 'path',
              'created', 'modified', 'related', 'tags', 'tasks')
    expected = {}
    for identity in base_tasks.keys() | ours_tasks.keys() | theirs_tasks.keys():
        base, ours, theirs = base_tasks.get(identity), ours_tasks.get(identity), theirs_tasks.get(identity)
        if ours == theirs:
            merged = ours
        elif ours == base:
            merged = theirs
        elif theirs == base:
            merged = ours
        elif ours is None or theirs is None:
            raise WikiError(f'task identity conflict requires manual resolution: {identity[0]}/{identity[1]}')
        else:
            merged = {}
            for field in fields:
                base_value = base.get(field) if base else None
                ours_value, theirs_value = ours.get(field), theirs.get(field)
                if ours_value == theirs_value:
                    merged[field] = ours_value
                elif ours_value == base_value:
                    merged[field] = theirs_value
                elif theirs_value == base_value:
                    merged[field] = ours_value
                else:
                    raise WikiError(f'task field conflict requires manual resolution: '
                                    f'{identity[0]}/{identity[1]} ({field})')
        if merged is not None:
            expected[identity] = merged
    return expected


def _validate_originals(candidate, ours, theirs, result):
    def originals(commit):
        found = {}
        for record in sync_git.run(candidate, 'ls-tree', '-r', '-z', commit).split(b'\0'):
            if not record:
                continue
            header, path = record.split(b'\t', 1)
            mode, kind, oid = header.decode('ascii').split()
            parts = Path(path.decode('utf-8', errors='surrogateescape')).parts
            if 'sources' in parts and kind == 'blob':
                found[path] = (mode, oid)
        return found
    first, second, final = originals(ours), originals(theirs), originals(result)
    for path in first.keys() | second.keys():
        old = first.get(path)
        incoming = second.get(path)
        if old is not None and incoming is not None and old != incoming:
            raise WikiError(f'original source changed on both sides: {path.decode("utf-8", errors="replace")}')
        expected = old if old is not None else incoming
        if final.get(path) != expected:
            raise WikiError(f'original source was changed or removed: {path.decode("utf-8", errors="replace")}')


def _validate_processing(candidate, revision=None):
    from .provenance import committed_processing
    from .spaces import Wiki
    inventory = committed_processing(Wiki(candidate), revision=revision)
    if inventory.incomplete:
        reasons = sorted({reason for _, _, reason in inventory.incomplete})
        raise WikiError('processing provenance incomplete: ' + '; '.join(reasons))
    counts = {}
    for item in inventory.processed:
        key = (item.space_address, item.source_sha256)
        counts.setdefault(key, []).append(item)
    duplicates = [items for items in counts.values() if len(items) > 1]
    if duplicates:
        scopes = sorted({f'{items[0].space_address}/{items[0].source_sha256}' for items in duplicates})
        raise WikiError('duplicate-source-decision-required: ' + ', '.join(scopes))


def _revision(operation):
    data = {key: operation[key] for key in ('root', 'repo', 'branch', 'remote', 'target_branch',
                                             'remote_fingerprint', 'kind', 'ours', 'theirs', 'candidate', 'tree', 'graph',
                                             'publication_graph', 'bootstrap_map_fingerprint')
            if key in operation}
    return operations.fingerprint(data)


def _bootstrap_map_fingerprint(value):
    if not isinstance(value, dict):
        raise WikiError('bootstrap map must map registered paths to endpoint and branch')
    for path, entry in value.items():
        if not isinstance(path, str) or not path or not isinstance(entry, dict) or set(entry) != {'url', 'branch'}:
            raise WikiError('bootstrap map entry must contain only url and branch')
        url, branch = entry['url'], entry['branch']
        if (not isinstance(url, str) or not url or not isinstance(branch, str)
                or not re.fullmatch(r'[A-Za-z0-9._/-]+', branch)
                or branch.startswith('-') or '..' in branch.split('/')):
            raise WikiError('bootstrap map endpoint and branch must be non-empty strings')
        parsed = urlsplit(url)
        query_keys = [part.split('=', 1)[0].lower() for part in parsed.query.split('&') if part]
        if (parsed.username or parsed.password
                or any(any(token in key for token in ('token', 'password', 'secret', 'key'))
                       for key in query_keys)):
            raise WikiError('bootstrap map must not contain credentials or secret query parameters')
    return operations.fingerprint(value)


def _has_embedded_credentials(url):
    parsed = urlsplit(url)
    sensitive = ('token', 'password', 'secret', 'key')
    query_keys = [part.split('=', 1)[0].lower() for part in parsed.query.split('&') if part]
    return bool(parsed.username or parsed.password or any(any(word in key for word in sensitive)
                                                          for key in query_keys))


def _decision_binding(accepted):
    return operations.fingerprint({'review_revision': accepted['revision'],
                                   'candidate_revision': accepted['candidate_revision'],
                                   'policy': accepted['policy'], 'decision': accepted.get('decision')})


def _validate_acceptance(runtime, operation):
    accepted = operation.get('accepted') or {}
    if accepted.get('candidate_revision') != operation.get('revision') or _revision(operation) != operation.get('revision'):
        raise WikiError('accepted candidate is stale')
    review = operation.get('review') or {}
    from . import sync_review
    if accepted.get('policy') != sync_review.policy().__dict__:
        raise WikiError('semantic reviewer identity or policy changed after acceptance')
    token = sync_review.validate_stored_report(runtime, operation)
    if not token or accepted.get('revision') != token:
        raise WikiError('acceptance revision no longer matches the reviewed report')
    if accepted.get('decision_binding') != _decision_binding(accepted):
        raise WikiError('acceptance decision binding is invalid')


def _candidate_message(wiki, runtime, candidate, operation_id):
    paths = [p.decode('utf-8', errors='surrogateescape') for p in
             sync_git.run(candidate, 'diff', '--cached', '--name-only', '-z').split(b'\0') if p]
    candidate_wiki = Wiki(candidate)
    spaces = sorted({space.address for path in paths
                     if (space := candidate_wiki.owner(candidate / path)) is not None})
    changes = dict(paths=paths, spaces=spaces,
                   tree=sync_git.text(candidate, 'write-tree').strip(),
                   diff=sync_git.run(candidate, 'diff', '--cached', '--no-color').decode('utf-8', errors='replace'))
    return commit_message.compose(changes, f'sync-{operation_id}', commit_message.current(),
                                  settings.commit_message(), wiki.root, runtime.base)


def fetch(wiki, state_dir, relative='.', target=Target(), *, kind='integration'):
    repo = _repo(wiki, relative)
    bootstrap_map_fingerprint = _bootstrap_map_fingerprint(target.bootstrap_map or {})
    runtime = operations.Runtime(state_dir, wiki.root)
    with runtime.lock():
        snapshot = sync_git.observe(repo)
        initial_graph = sync_graph.discover(repo, snapshot.head)
        registered_paths = {item['path'] for item in initial_graph if item['path'] != '.'}
        if set(target.bootstrap_map or {}) - registered_paths:
            raise WikiError('bootstrap map names a path outside the registered candidate graph')
        tracking = _target_ref(repo, target)
        remote_fp = _remote_fingerprint(repo, target.remote)
        theirs = sync_git.fetch_tip(repo, target.remote, target.branch)
        folder = runtime.path / 'sync'
        if folder.is_dir():
            pending = [json.loads(path.read_text(encoding='utf-8')) for path in folder.glob('*.json')]
            pending = [item for item in pending if item.get('status') not in ('published', 'closed', 'applied')]
            exact = next((item for item in pending if item.get('root') == str(wiki.root)
                          and item.get('kind') == kind and item.get('branch') == snapshot.branch
                          and item.get('ours') == snapshot.head and item.get('remote') == target.remote
                          and item.get('target_branch') == target.branch
                          and item.get('remote_fingerprint') == remote_fp and item.get('theirs') == theirs
                          and item.get('bootstrap_map_fingerprint', _bootstrap_map_fingerprint({}))
                          == bootstrap_map_fingerprint), None)
            if exact:
                return dict(operation_id=exact['operation_id'], status=exact['status'], ours=exact['ours'],
                            theirs=exact['theirs'], branch=exact['branch'], remote=target.remote,
                            target_branch=target.branch, reused=True)
            if pending:
                raise WikiError('another unfinished sync operation reserves this wiki; finish it first')
        # The remote endpoint is re-read after fetch to catch a concurrent target change.
        if remote_fp != _remote_fingerprint(repo, target.remote):
            raise WikiError('sync remote changed during fetch')
        ident = datetime.now(timezone.utc).strftime('sync-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
        operation = dict(format=1, operation_id=ident, root=str(wiki.root), repo='.',
                         branch=snapshot.branch, ours=snapshot.head, theirs=theirs,
                         remote=target.remote, target_branch=target.branch, kind=kind,
                         remote_fingerprint=remote_fp, tracking_ref=tracking, status='fetched',
                         candidate=None, revision=None, review=None, accepted=None, applied=None,
                         created_at=datetime.now(timezone.utc).isoformat(),
                         bootstrap_map=target.bootstrap_map or {},
                         bootstrap_map_fingerprint=bootstrap_map_fingerprint)
        operation['live_graph'] = [dict(path=item['path'], branch=item['branch'], ours=item['ours'])
                                   for item in initial_graph]
        _save(runtime, operation)
        return dict(operation_id=ident, status='fetched', ours=snapshot.head, theirs=theirs,
                    branch=snapshot.branch, remote=target.remote, target_branch=target.branch)


def _operation(runtime, ident):
    operation = _load(runtime, ident)
    candidate = _candidate(runtime, ident)
    operation['_candidate_path'] = candidate
    return operation


def prepare(wiki, state_dir, ident, *, merge=False):
    runtime = operations.Runtime(state_dir, wiki.root)
    with runtime.lock():
        operation = _operation(runtime, ident)
        candidate = operation['_candidate_path']
        if operation.get('candidate') and operation.get('status') in ('applied', 'published'):
            return inspect(wiki, state_dir, ident)
        if candidate.exists() and operation.get('candidate'):
            result = sync_git.head(candidate)
            if result == operation['candidate']:
                return inspect(wiki, state_dir, ident)
            if operation.get('status') not in ('prepared', 'reviewed', 'accepted', 'resolving'):
                raise WikiError('candidate correction is not allowed in this operation state')
            if sync_git.text(candidate, 'status', '--porcelain=v1', '-z', '--untracked-files=all'):
                raise WikiError('corrected candidate must be Git-clean before revalidation')
        elif candidate.exists():
            # A conflict handoff is never overwritten. A user-created merge commit can be sealed on retry.
            merge_head = sync_git.text(candidate, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}', check=False).strip()
            if merge_head:
                unmerged = sync_git.run(candidate, 'ls-files', '--unmerged', '-z')
                if unmerged and operation.get('live_graph') and len(operation['live_graph']) > 1:
                    sync_graph.prepare_graph(operation, candidate,
                        message=lambda path, *_: _candidate_message(wiki, runtime, path, ident))
                    if operation.get('_graph_conflicts'):
                        return dict(operation_id=ident, status='conflict', candidate=str(candidate),
                                    child_conflicts=operation['_graph_conflicts'])
                    unmerged = sync_git.run(candidate, 'ls-files', '--unmerged', '-z')
                if unmerged:
                    return dict(operation_id=ident, status='conflict', candidate=str(candidate),
                                unmerged=unmerged.decode('utf-8', errors='surrogateescape').split('\0'))
                if sync_git.text(candidate, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}', check=False).strip():
                    result = sync_git.commit_merge(candidate, message=_candidate_message(wiki, runtime, candidate, ident))
                else:
                    result = sync_git.head(candidate)
            else:
                result = sync_git.head(candidate)
        else:
            outcome = sync_git.prepare_candidate(wiki.root, candidate, operation['ours'],
                                                 operation['theirs'], merge=merge,
                                                 local_only=operation.get('kind') == 'publish-only',
                                                 message=lambda path, *_: _candidate_message(wiki, runtime, path, ident))
            if outcome['state'] == 'diverged':
                return dict(operation_id=ident, status='diverged', ours=operation['ours'],
                            theirs=operation['theirs'], requires_merge=True)
            if outcome['state'] == 'conflict':
                if operation.get('live_graph') and len(operation['live_graph']) > 1:
                    operation['candidate'] = operation['ours']
                    sync_graph.prepare_graph(operation, candidate,
                        message=lambda path, *_: _candidate_message(wiki, runtime, path, ident))
                    if operation.get('_graph_conflicts'):
                        return dict(operation_id=ident, status='conflict', candidate=str(candidate),
                                    child_conflicts=operation['_graph_conflicts'])
                    remaining = sync_git.run(candidate, 'ls-files', '--unmerged', '-z')
                    if not remaining:
                        result = sync_git.head(candidate)
                    else:
                        return dict(operation_id=ident, status='conflict', candidate=str(candidate),
                                    unmerged=remaining.decode('utf-8', errors='surrogateescape').split('\0'))
                else:
                    return dict(operation_id=ident, status='conflict', candidate=str(candidate),
                                unmerged=outcome['unmerged'].split('\0'))
            else:
                result = outcome['candidate']
        if not sync_git._ancestor(candidate, operation['ours'], result) or not sync_git._ancestor(
                candidate, operation['theirs'], result):
            raise WikiError('candidate no longer preserves the recorded O/T histories')
        operation['candidate'] = result
        if operation.get('live_graph') and len(operation['live_graph']) > 1:
            sync_graph.prepare_graph(operation, candidate,
                                     message=lambda path, *_: _candidate_message(wiki, runtime, path, ident))
            if operation.get('_graph_conflicts'):
                return dict(operation_id=ident, status='conflict', candidate=str(candidate),
                            child_conflicts=operation['_graph_conflicts'])
            result = operation['candidate']
        else:
            operation['graph'] = [dict(path='.', candidate=result,
                                       tree=sync_git.text(candidate, 'rev-parse', f'{result}^{{tree}}').strip(),
                                       branch=operation['branch'], ours=operation['ours'])]
        _task_changes(candidate, operation['ours'], operation['theirs'], result)
        _validate_originals(candidate, operation['ours'], operation['theirs'], result)
        _validate_processing(candidate)
        if sync_git.text(candidate, 'status', '--porcelain=v1', '-z', '--untracked-files=all'):
            raise WikiError('candidate is not clean')
        report = sync_graph.verify_graph(sync_graph.CandidateGraph(Path(candidate), tuple(
            dict(path=item['path'], candidate=str(candidate) if item['path'] == '.' else str(Path(candidate) / item['path']),
                 commit=item['candidate'], tree=item['tree'], branch=item['branch'], ours=item['ours'])
            for item in operation['graph'])))
        if not report['ok']:
            raise WikiError('candidate structural check failed: ' + '; '.join(report['errors']))
        operation['candidate'] = result
        operation['tree'] = operation['graph'][0]['tree']
        operation['revision'] = _revision(operation)
        operation['status'] = 'prepared'
        operation['review'] = None
        operation['accepted'] = None
        operation['semantic_review'] = None
        operation.pop('_candidate_path', None)
        _save(runtime, operation)
        return dict(operation_id=ident, status='prepared', candidate=result,
                    candidate_worktree=str(candidate), revision=operation['revision'], check=report)


def inspect(wiki, state_dir, ident=None):
    base = operations.Runtime.__new__(operations.Runtime)
    base.base = operations.outside_git(Path(state_dir).expanduser().absolute())
    base.root = wiki.root
    base.namespace = operations.sha256(str(wiki.root).encode())[:24]
    base.path = base.base / 'roots' / base.namespace
    if ident:
        operation = _load(base, ident)
        fields = ('operation_id', 'kind', 'status', 'branch', 'ours', 'theirs', 'candidate', 'tree',
                  'revision', 'remote', 'target_branch', 'applied', 'published', 'semantic_review',
                  'publication_graph', 'published_graph', 'bootstrap_map_fingerprint', 'resolver_result')
        result = {key: operation[key] for key in fields if key in operation}
        if operation.get('resolver_proposal'):
            proposal = operation['resolver_proposal']
            result['resolver_proposal'] = {key: proposal[key] for key in
                                           ('revision', 'candidate_head', 'reports', 'written', 'recorded_at',
                                            'policy')
                                           if key in proposal}
        if operation.get('candidate'):
            result['candidate_worktree'] = str(_candidate(base, ident))
        return result
    folder = base.path / 'sync'
    if not folder.is_dir():
        return {'operations': []}
    values = []
    for path in sorted(folder.glob('*.json')):
        item = json.loads(path.read_text(encoding='utf-8'))
        if item.get('root') == str(wiki.root):
            values.append({key: item.get(key) for key in ('operation_id', 'status', 'ours', 'theirs', 'candidate')})
    return {'operations': values}


def close(wiki, state_dir, ident):
    """Release an unfinished local reservation while retaining its candidate and journal."""
    runtime = operations.Runtime(state_dir, wiki.root)
    with runtime.lock():
        operation = _load(runtime, ident)
        if operation.get('status') in ('applied', 'published'):
            raise WikiError('completed operations cannot be closed')
        if operation.get('status') == 'closed':
            return dict(operation_id=ident, status='closed', candidate_retained=bool(operation.get('candidate')))
        operation['status'] = 'closed'
        operation['closed_at'] = datetime.now(timezone.utc).isoformat()
        _save(runtime, operation)
        return dict(operation_id=ident, status='closed', candidate_retained=bool(operation.get('candidate')))


def manual_review(wiki, state_dir, ident):
    from . import sync_review
    runtime = operations.Runtime(state_dir, wiki.root)
    with runtime.lock():
        operation = _load(runtime, ident)
        if operation.get('status') != 'prepared' or not operation.get('candidate'):
            raise WikiError('only a sealed, structurally valid candidate can be manually reviewed')
        candidate = _candidate(runtime, ident)
        _assert_candidate(operation, candidate)
        if _revision(operation) != operation['revision']:
            raise WikiError('candidate changed; prepare and review the current revision again')
        operation['review'] = dict(kind='manual', revision=operation['revision'],
                                   candidate_revision=operation['revision'],
                                   git_revision=operation['revision'],
                                   acceptance_revision=operation['revision'],
                                   policy=sync_review.policy().__dict__,
                                   recorded_at=datetime.now(timezone.utc).isoformat())
        operation['status'] = 'reviewed'
        _save(runtime, operation)
        return dict(operation_id=ident, status='reviewed', revision=operation['revision'], review='manual')


def accept(wiki, state_dir, ident, revision, *, decision=None):
    from . import sync_review
    runtime = operations.Runtime(state_dir, wiki.root)
    with runtime.lock():
        operation = _load(runtime, ident)
        current_policy = sync_review.policy().__dict__
        if operation.get('status') == 'accepted' and operation.get('accepted', {}).get('revision') == revision:
            existing_decision = operation['accepted'].get('decision')
            if decision is None or decision == existing_decision:
                _validate_acceptance(runtime, operation)
                if operation['accepted'].get('policy') == current_policy:
                    return dict(operation_id=ident, status='accepted', revision=revision, reused=True)
                raise WikiError('semantic policy changed after acceptance')
            raise WikiError('acceptance decision is already bound to this revision')
        if operation.get('status') not in ('reviewed', 'accepted') or not operation.get('review'):
            raise WikiError('manual review is required before acceptance')
        if operation['review'].get('git_revision', operation['review'].get('revision')) != operation.get('revision'):
            raise WikiError('acceptance candidate revision does not match the reviewed candidate')
        expected_revision = sync_review.validate_stored_report(runtime, operation)
        if revision != expected_revision:
            raise WikiError('acceptance revision does not match the reviewed report')
        if operation['review'].get('policy') != current_policy:
            raise WikiError('semantic policy changed after review; review the current candidate again')
        semantic = operation.get('semantic_review')
        if current_policy.get('approval') == 'auto':
            report_path = (runtime.path / 'sync-reports' / ident /
                           f'{(semantic or {}).get("revision", "")}.json')
            reports = json.loads(report_path.read_text(encoding='utf-8')) if report_path.is_file() else []
            from .sync_resolver import acceptance_decision
            candidate = _candidate(runtime, ident)
            graph = sync_graph.verify_graph(sync_graph.CandidateGraph(Path(candidate), tuple(
                dict(path=item['path'], candidate=str(candidate) if item['path'] == '.' else str(Path(candidate) / item['path']),
                     commit=item['candidate'], tree=item['tree'], branch=item['branch'], ours=item['ours'])
                for item in operation.get('graph', [])))) if operation.get('graph') else None
            technical_ok = graph['ok'] if graph else check(Wiki(candidate))['ok']
            resolver_result = operation.get('resolver_result') or {}
            resolver_commit = resolver_result.get('commit')
            resolver_commit_valid = (not resolver_result or bool(resolver_commit and sync_git._ancestor(
                candidate, resolver_commit, operation.get('candidate', ''))))
            bundle = {'reports': reports, 'technical_checks_passed': technical_ok,
                      'candidate_current': sync_git.head(candidate) == operation.get('candidate'),
                      'resolver_commit_valid': resolver_commit_valid,
                      'fresh_review': bool(semantic and semantic.get('coverage') == 'all'
                                           and semantic.get('revision'))}
            if acceptance_decision(bundle, current_policy) != 'accepted':
                raise WikiError('automatic acceptance hard gates are not satisfied')
            decision = None
        elif operation['review'].get('kind') == 'semantic' and semantic and semantic.get('verdict') != 'clear':
            if not isinstance(decision, str) or not decision.strip():
                raise WikiError('a concrete owner decision is required for unresolved semantic findings')
        elif decision is not None and (not isinstance(decision, str) or not decision.strip()):
            raise WikiError('owner decision must be nonempty')
        candidate = _candidate(runtime, ident)
        _assert_candidate(operation, candidate)
        accepted = dict(revision=revision, candidate_revision=operation['revision'], policy=current_policy,
                        decision=decision.strip() if isinstance(decision, str) else None,
                        recorded_at=datetime.now(timezone.utc).isoformat())
        accepted['decision_binding'] = _decision_binding(accepted)
        operation['accepted'] = accepted
        operation['status'] = 'accepted'
        _save(runtime, operation)
        return dict(operation_id=ident, status='accepted', revision=revision)


def apply(wiki, state_dir, ident):
    runtime = operations.Runtime(state_dir, wiki.root)
    with runtime.lock():
        operation = _load(runtime, ident)
        if operation.get('status') == 'applied':
            return dict(operation_id=ident, status='applied', head=operation['applied'])
        if operation.get('status') != 'accepted' or not operation.get('accepted'):
            raise WikiError('operation has no valid owner acceptance')
        _validate_acceptance(runtime, operation)
        repo = _repo(wiki, operation['repo'])
        graph_operation = operation.get('graph') and len(operation['graph']) > 1
        if graph_operation:
            live = sync_git.RepoSnapshot(Path(repo), sync_git.text(repo, 'symbolic-ref', '--quiet', 'HEAD').strip(),
                                         sync_git.head(repo))
        else:
            live = sync_git.observe(repo)
        if operation.get('kind') == 'publish-only':
            _assert_candidate(operation, _candidate(runtime, ident))
            if live.branch != operation['branch'] or live.head != operation['ours'] or live.head != operation['candidate']:
                raise WikiError('publish-only local tips changed after review')
            return dict(operation_id=ident, status='not-applicable', head=live.head)
        if live.branch == operation['branch'] and live.head == operation['candidate'] and not graph_operation:
            candidate = _candidate(runtime, ident)
            _assert_candidate(operation, candidate)
            operation['status'] = 'applied'
            operation['applied'] = live.head
            _save(runtime, operation)
            return dict(operation_id=ident, status='applied', head=live.head, resumed=True)
        if (live.branch != operation['branch'] or
                (live.head != operation['ours'] and not (graph_operation and live.head == operation['candidate']))):
            raise WikiError('live branch changed after candidate review')
        candidate = _candidate(runtime, ident)
        _assert_candidate(operation, candidate)
        report = sync_graph.verify_graph(sync_graph.CandidateGraph(Path(candidate), tuple(
            dict(path=item['path'], candidate=str(candidate) if item['path'] == '.' else str(Path(candidate) / item['path']),
                 commit=item['candidate'], tree=item['tree'], branch=item['branch'], ours=item['ours'])
            for item in operation.get('graph', [])))) if operation.get('graph') else check(Wiki(candidate))
        if not report['ok']:
            raise WikiError('candidate structural check failed: ' + '; '.join(report['errors']))
        if operation.get('graph') and len(operation['graph']) > 1:
            operation['_candidate_root'] = str(candidate)
            result = sync_graph.apply_graph(operation, operation['revision'])
            operation.pop('_candidate_root', None)
            operation['status'] = 'applied'
            operation['applied'] = operation['candidate']
            _save(runtime, operation)
            return {**result, 'operation_id': ident}
        sync_git.text(repo, 'fetch', '--no-tags', '--no-recurse-submodules', '--', str(candidate),
                      'refs/heads/pisar-candidate')
        imported = sync_git.text(repo, 'rev-parse', '--verify', 'FETCH_HEAD^{commit}').strip()
        if imported != operation['candidate']:
            raise WikiError('candidate import did not resolve to the accepted commit')
        if not sync_git._ancestor(repo, operation['ours'], imported):
            raise WikiError('accepted candidate is not a fast-forward of the recorded live head')
        sync_git.text(repo, 'merge', '--ff-only', '--no-autostash', '--no-overwrite-ignore', '--no-edit', imported)
        if sync_git.head(repo) != imported or sync_git.text(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all'):
            raise WikiError('live repository did not finish cleanly at the accepted candidate')
        operation['status'] = 'applied'
        operation['applied'] = imported
        _save(runtime, operation)
        return dict(operation_id=ident, status='applied', head=imported)


def publish(wiki, state_dir, ident, *, dry_run=False, bootstrap_map=None):
    if dry_run:
        item = inspect(wiki, state_dir, ident)
        if item.get('status') not in ('applied', 'accepted'):
            raise WikiError('only an applied or accepted publish-only operation can be published')
        graph = sorted(item.get('publication_graph', []),
                       key=lambda target: (target['path'].count('/'), target['path'] != '.'), reverse=True)
        return dict(operation_id=ident, status='ready', remote=item['remote'], branch=item['target_branch'],
                    commit=item.get('applied') or item.get('candidate'), graph=graph,
                    bootstrap_map_fingerprint=item.get('bootstrap_map_fingerprint'), dry_run=True)
    runtime = operations.Runtime(state_dir, wiki.root)
    with runtime.lock():
        operation = _load(runtime, ident)
        if operation.get('status') == 'published':
            return dict(operation_id=ident, status='published', commit=operation['published'], already_reachable=True)
        publish_only = operation.get('kind') == 'publish-only'
        if operation.get('status') != ('accepted' if publish_only else 'applied'):
            raise WikiError('only an applied integration or accepted publish-only operation can be published')
        _validate_acceptance(runtime, operation)
        repo = _repo(wiki, operation['repo'])
        live = sync_git.observe(repo)
        if publish_only:
            _assert_candidate(operation, _candidate(runtime, ident))
            if live.branch != operation['branch'] or live.head != operation['ours'] or live.head != operation['candidate']:
                raise WikiError('publish-only local tips changed after review')
        elif live.branch != operation['branch'] or live.head != operation['applied']:
            raise WikiError('live branch changed after application; review the new outgoing state')
        if _remote_fingerprint(repo, operation['remote']) != operation['remote_fingerprint']:
            raise WikiError('sync remote changed after review')
        if _remote_fingerprint(repo, operation['remote'], push=True) != operation['remote_fingerprint']:
            raise WikiError('sync push destination differs from the reviewed remote')
        commit_to_publish = operation['candidate'] if publish_only else operation['applied']
        if operation.get('graph') and len(operation['graph']) > 1:
            candidate_root = _candidate(runtime, ident)
            graph = sync_graph.CandidateGraph(candidate_root, tuple(
                dict(path=item['path'], candidate=str(candidate_root if item['path'] == '.' else candidate_root / item['path']),
                     commit=item['candidate'], tree=item['tree'], branch=item['branch'], ours=item['ours'],
                     repo=repo if item['path'] == '.' else repo / item['path'])
                for item in operation['graph']))
            supplied_map = operation.get('bootstrap_map', {}) if bootstrap_map is None else bootstrap_map
            if _bootstrap_map_fingerprint(supplied_map) != operation.get('bootstrap_map_fingerprint',
                                                                         _bootstrap_map_fingerprint({})):
                raise WikiError('bootstrap map changed after review or must be supplied again')
            sync_graph.publish_children(operation, graph, lambda value: _save(runtime, value), supplied_map)
        tracking = _target_ref(repo, Target(operation['remote'], operation['target_branch']))
        current = sync_git.fetch_tip(repo, operation['remote'], operation['target_branch'])
        if sync_git._ancestor(repo, commit_to_publish, current):
            operation['published'] = commit_to_publish
            operation['status'] = 'published'
            _save(runtime, operation)
            return dict(operation_id=ident, status='published', commit=commit_to_publish, already_reachable=True)
        if not sync_git._ancestor(repo, current, commit_to_publish):
            raise WikiError('remote-advanced; integrate and review the new remote tip first')
        if current != operation['theirs']:
            raise WikiError('remote baseline changed after review')
        command = sync_git.command(repo, 'push', '--porcelain', '--no-follow-tags',
                                  '--recurse-submodules=check', operation['remote'],
                                  f"{commit_to_publish}:refs/heads/{operation['target_branch']}")
        result = subprocess.run(command, capture_output=True, env=gitops.environment())
        observed = sync_git.text(repo, 'ls-remote', '--refs', operation['remote'],
                                 f"refs/heads/{operation['target_branch']}", check=False).split()
        if result.returncode:
            if observed:
                try:
                    observed_tip = sync_git.fetch_tip(repo, operation['remote'], operation['target_branch'])
                except WikiError:
                    observed_tip = None
            else:
                observed_tip = None
            if observed_tip and sync_git._ancestor(repo, commit_to_publish, observed_tip):
                operation['published'] = commit_to_publish
                operation['status'] = 'published'
                _save(runtime, operation)
                return dict(operation_id=ident, status='published', commit=commit_to_publish, observed_after_error=True)
            if observed and not observed_tip:
                raise WikiError('publication-unknown: remote was visible but could not be fetched for ancestry check')
            raise WikiError('publication-unknown' if not observed else 'publication failed; remote did not accept candidate')
        if not observed or observed[0] != commit_to_publish:
            observed_tip = sync_git.fetch_tip(repo, operation['remote'], operation['target_branch']) if observed else None
            if not observed_tip or not sync_git._ancestor(repo, commit_to_publish, observed_tip):
                raise WikiError('publication-unknown: remote observation does not include candidate')
        operation['published'] = commit_to_publish
        operation['status'] = 'published'
        _save(runtime, operation)
        return dict(operation_id=ident, status='published', commit=commit_to_publish)


def publish_only(wiki, state_dir, relative='.', target=Target()):
    fetched = fetch(wiki, state_dir, relative, target, kind='publish-only')
    return prepare(wiki, state_dir, fetched['operation_id'])


def semantic_review(wiki, state_dir, ident, *, backend=None):
    from . import sync_review
    return sync_review.review_operation(wiki, state_dir, ident, backend=backend)


def resolve(wiki, state_dir, ident, *, backend=None, revision=None, decision=None, review_backend=None):
    """Propose a scoped candidate resolution or commit an explicitly approved proposal."""
    from . import sync_resolver
    return sync_resolver.resolve_operation(wiki, state_dir, ident, backend=backend, revision=revision,
                                           decision=decision, review_backend=review_backend)


def feedback(wiki, state_dir, ident, rating, category=None):
    from . import sync_review
    return sync_review.record_feedback(wiki, state_dir, ident, rating, category)


def feedback_summary(wiki, state_dir):
    from . import sync_review
    return sync_review.feedback_summary(wiki, state_dir)


def pull(wiki, state_dir, relative='.', target=Target(), *, merge=False, dry_run=False,
         review_backend=None):
    if dry_run:
        repo = _repo(wiki, relative)
        snapshot = sync_git.observe(repo)
        tracking = f'refs/remotes/{target.remote}/{target.branch}'
        remote_tip = sync_git.text(repo, 'rev-parse', '--verify', f'{tracking}^{{commit}}', check=False).strip()
        return dict(status='dry-run', branch=snapshot.branch, head=snapshot.head,
                    cached_remote_tip=remote_tip or None, remote_freshness='unknown')
    fetched = fetch(wiki, state_dir, relative, target)
    result = prepare(wiki, state_dir, fetched['operation_id'], merge=merge)
    if result.get('status') == 'prepared' and settings.semantic_policy().get('approval') == 'auto':
        from . import sync_review
        reviewed = semantic_review(wiki, state_dir, fetched['operation_id'], backend=review_backend)
        if reviewed.get('status') == 'reviewed':
            from .sync_resolver import acceptance_decision
            if acceptance_decision({'reports': reviewed.get('reports', []),
                                    'technical_checks_passed': True, 'candidate_current': True,
                                    'resolver_commit_valid': True, 'fresh_review': True},
                                   reviewed.get('policy', {})) != 'accepted':
                return {**result, 'operation_id': fetched['operation_id'], 'status': 'needs-decision',
                        'semantic_review': reviewed}
            accept(wiki, state_dir, fetched['operation_id'], reviewed['revision'])
            applied = apply(wiki, state_dir, fetched['operation_id'])
            return {**result, **applied, 'operation_id': fetched['operation_id'],
                    'status': 'applied-automatically', 'semantic_review': reviewed}
        if reviewed.get('status') == 'no-change':
            return {**result, 'operation_id': fetched['operation_id'], 'status': 'no-change'}
        return {**result, 'operation_id': fetched['operation_id'], 'status': 'needs-decision',
                'semantic_review': reviewed}
    return {**result, 'operation_id': fetched['operation_id'],
            'status': 'needs-approval' if result.get('status') == 'prepared' else result.get('status')}
