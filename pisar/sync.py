"""Delivery-I single-repository Git sync lifecycle."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import uuid

from . import commit_message, gitops, operations, settings, sync_git
from .documents import check
from .safety import WikiError, concrete_id, sha256
from .spaces import Wiki


@dataclass(frozen=True)
class Target:
    remote: str = 'origin'
    branch: str = 'main'


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
        raise WikiError('delivery I supports only the complete, submodule-free wiki root')
    if gitops.run(wiki.root, 'rev-parse', '--show-toplevel').strip() != str(wiki.root):
        raise WikiError('sync wiki must be the top of its Git repository')
    links = gitops.run(wiki.root, 'ls-files', '--stage', '-z')
    if any(record.startswith('160000 ') for record in links.split('\0') if record):
        raise WikiError('delivery I does not support submodule-bearing wiki roots')
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


def _task_changes(candidate, ours, theirs, result):
    changed = set()
    for before, after in ((ours, result), (theirs, result)):
        raw = sync_git.run(candidate, 'diff', '--name-only', '-z', before, after)
        changed.update(p.decode('utf-8', errors='surrogateescape') for p in raw.split(b'\0') if p)
    for base in (ours, theirs):
        commits = sync_git.text(candidate, 'rev-list', '--reverse', f'{base}..{result}').splitlines()
        for commit in commits:
            raw = sync_git.run(candidate, 'diff-tree', '--root', '--no-commit-id', '--name-only', '-r', '-z',
                               '-m', commit)
            changed.update(p.decode('utf-8', errors='surrogateescape') for p in raw.split(b'\0') if p)
    if any('.ruwana' in Path(name).parts for name in changed):
        raise WikiError('task-validation-unavailable')


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


def _revision(operation):
    data = {key: operation[key] for key in ('root', 'repo', 'branch', 'remote', 'target_branch',
                                             'remote_fingerprint', 'kind', 'ours', 'theirs', 'candidate', 'tree')
            if key in operation}
    return operations.fingerprint(data)


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
    runtime = operations.Runtime(state_dir, wiki.root)
    with runtime.lock():
        snapshot = sync_git.observe(repo)
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
                          and item.get('remote_fingerprint') == remote_fp and item.get('theirs') == theirs), None)
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
                         created_at=datetime.now(timezone.utc).isoformat())
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
            if operation.get('status') not in ('prepared', 'reviewed', 'accepted'):
                raise WikiError('candidate correction is not allowed in this operation state')
            if sync_git.text(candidate, 'status', '--porcelain=v1', '-z', '--untracked-files=all'):
                raise WikiError('corrected candidate must be Git-clean before revalidation')
        elif candidate.exists():
            # A conflict handoff is never overwritten. A user-created merge commit can be sealed on retry.
            merge_head = sync_git.text(candidate, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}', check=False).strip()
            if merge_head:
                unmerged = sync_git.run(candidate, 'ls-files', '--unmerged', '-z')
                if unmerged:
                    return dict(operation_id=ident, status='conflict', candidate=str(candidate),
                                unmerged=unmerged.decode('utf-8', errors='surrogateescape').split('\0'))
                result = sync_git.commit_merge(candidate, message=_candidate_message(wiki, runtime, candidate, ident))
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
                return dict(operation_id=ident, status='conflict', candidate=str(candidate),
                            unmerged=outcome['unmerged'].split('\0'))
            result = outcome['candidate']
        if not sync_git._ancestor(candidate, operation['ours'], result) or not sync_git._ancestor(
                candidate, operation['theirs'], result):
            raise WikiError('candidate no longer preserves the recorded O/T histories')
        if any(record.startswith(b'160000 ') for record in sync_git.run(candidate, 'ls-tree', '-r', '-z', result).split(b'\0') if record):
            raise WikiError('delivery I does not support submodule-bearing candidate trees')
        _task_changes(candidate, operation['ours'], operation['theirs'], result)
        _validate_originals(candidate, operation['ours'], operation['theirs'], result)
        if sync_git.text(candidate, 'status', '--porcelain=v1', '-z', '--untracked-files=all'):
            raise WikiError('candidate is not clean')
        report = check(Wiki(candidate))
        if not report['ok']:
            raise WikiError('candidate structural check failed: ' + '; '.join(report['errors']))
        operation['candidate'] = result
        operation['tree'] = sync_git.text(candidate, 'rev-parse', f'{result}^{{tree}}').strip()
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
                  'revision', 'remote', 'target_branch', 'applied', 'published', 'semantic_review')
        result = {key: operation[key] for key in fields if key in operation}
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
        if operation['review'].get('kind') == 'semantic' and semantic and semantic.get('verdict') != 'clear':
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
        live = sync_git.observe(repo)
        if operation.get('kind') == 'publish-only':
            _assert_candidate(operation, _candidate(runtime, ident))
            if live.branch != operation['branch'] or live.head != operation['ours'] or live.head != operation['candidate']:
                raise WikiError('publish-only local tips changed after review')
            return dict(operation_id=ident, status='not-applicable', head=live.head)
        if live.branch == operation['branch'] and live.head == operation['candidate']:
            candidate = _candidate(runtime, ident)
            _assert_candidate(operation, candidate)
            operation['status'] = 'applied'
            operation['applied'] = live.head
            _save(runtime, operation)
            return dict(operation_id=ident, status='applied', head=live.head, resumed=True)
        if live.branch != operation['branch'] or live.head != operation['ours']:
            raise WikiError('live branch changed after candidate review')
        candidate = _candidate(runtime, ident)
        _assert_candidate(operation, candidate)
        report = check(Wiki(candidate))
        if not report['ok']:
            raise WikiError('candidate structural check failed: ' + '; '.join(report['errors']))
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


def publish(wiki, state_dir, ident, *, dry_run=False):
    if dry_run:
        item = inspect(wiki, state_dir, ident)
        if item.get('status') not in ('applied', 'accepted'):
            raise WikiError('only an applied or accepted publish-only operation can be published')
        return dict(operation_id=ident, status='ready', remote=item['remote'], branch=item['target_branch'],
                    commit=item.get('applied') or item.get('candidate'), dry_run=True)
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


def feedback(wiki, state_dir, ident, rating, category=None):
    from . import sync_review
    return sync_review.record_feedback(wiki, state_dir, ident, rating, category)


def pull(wiki, state_dir, relative='.', target=Target(), *, merge=False, dry_run=False):
    if dry_run:
        repo = _repo(wiki, relative)
        snapshot = sync_git.observe(repo)
        tracking = f'refs/remotes/{target.remote}/{target.branch}'
        remote_tip = sync_git.text(repo, 'rev-parse', '--verify', f'{tracking}^{{commit}}', check=False).strip()
        return dict(status='dry-run', branch=snapshot.branch, head=snapshot.head,
                    cached_remote_tip=remote_tip or None, remote_freshness='unknown')
    fetched = fetch(wiki, state_dir, relative, target)
    result = prepare(wiki, state_dir, fetched['operation_id'], merge=merge)
    return {**result, 'operation_id': fetched['operation_id'],
            'status': 'needs-approval' if result.get('status') == 'prepared' else result.get('status')}
