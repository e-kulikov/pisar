"""External journals, cooperative writer locks, recoverable file/Git operations."""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
from . import gitops, settings
from .documents import DATE, check_references, metadata, scan, validate_meta
from .safety import WikiError, concrete_id, safe_path, sha256


_HELD_LOCKS = ContextVar('pisar_writer_locks', default=())


def external_path(path):
    path = Path(path).absolute()
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise WikiError(f'external symlink refused: {part}')
    return path.resolve()


def outside_git(path):
    path = external_path(path)
    for parent in [path, *path.parents]:
        marker = parent / '.git'
        if marker.is_file() or (marker / 'HEAD').is_file() or ((parent / 'HEAD').is_file() and (parent / 'objects').is_dir()):
            raise WikiError(f'runtime must be outside all Git repositories: {path}')
    return path


def atomic_bytes(path, content):
    external_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.pisar-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


class Runtime:
    def __init__(self, directory, root):
        self.base = outside_git(settings.state_dir(directory))
        self.root = Path(root).resolve()
        if self.base.is_relative_to(self.root):
            raise WikiError('runtime cannot be inside wiki root')
        if self.root.is_relative_to(self.base):
            raise WikiError(f'wiki root cannot be inside runtime: {self.base}')
        self.namespace = sha256(str(self.root).encode())[:24]
        self.path = self.base / 'roots' / self.namespace
        outside_git(self.path)
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path, 0o700)

    @contextmanager
    def lock(self):
        path = safe_path(self.path, 'writer.lock')
        if str(path) in _HELD_LOCKS.get():
            yield
            return
        with path.open('a+b') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WikiError('pisar writer lock is busy; retry after current operation') from None
            try:
                token = _HELD_LOCKS.set((*_HELD_LOCKS.get(), str(path)))
                yield
            finally:
                _HELD_LOCKS.reset(token)
                fcntl.flock(stream, fcntl.LOCK_UN)

    def journal_path(self, ident):
        concrete_id(ident, 'operation_id')
        return safe_path(self.path, f'operations/{ident}.json')

    def load(self, ident):
        path = self.journal_path(ident)
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('root') != str(self.root) or value.get('operation_id') != ident:
            raise WikiError('journal root/operation namespace mismatch')
        return value

    def store(self, journal):
        outside_git(self.path)
        write_json(self.journal_path(journal['operation_id']), journal)


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())


def source_bytes(path, expected=None):
    path = external_path(path)
    if not path.is_file():
        raise WikiError(f'source is not a file: {path}')
    data = path.read_bytes()
    digest = sha256(data)
    if expected is not None and digest != expected:
        raise WikiError(f'source hash conflict: {path}')
    return path, data, digest


def check_source_domain(wiki, space, path):
    path = external_path(path)
    if path.is_relative_to(wiki.root):
        relative = path.relative_to(wiki.root)
        if not relative.parts or relative.parts[0] != space.domain:
            raise WikiError(f'source domain violation: in-root source must be in {space.domain}/')
    return path


def artifact(wiki, space, relative, data, base_sha256=None):
    path = safe_path(space.path, relative)
    if wiki.owner(path) != space:
        raise WikiError('destination crosses space ownership')
    if '.ruwana' in Path(relative).parts:
        raise WikiError('tasks must be written through ruwana')
    before = sha256(path.read_bytes()) if path.is_file() else None
    if path.exists() and not path.is_file():
        raise WikiError('destination is not a file')
    if base_sha256 is None and path.exists():
        raise WikiError(f'destination already exists: {path}')
    if base_sha256 is not None and before != base_sha256:
        raise WikiError(f'destination base hash conflict: {path}')
    repo = gitops.owner(path, wiki.root)
    ignored = gitops.run(repo, 'check-ignore', '--', path.relative_to(repo).as_posix(), check=False)
    if ignored.strip():
        raise WikiError(f'destination is Git-ignored: {path}')
    return dict(path=path.relative_to(wiki.root).as_posix(), before=before,
                after=sha256(data), data=base64.b64encode(data).decode(),
                repo=str(repo))


def groups(wiki, artifacts):
    repos, links = {}, {}
    for item in artifacts:
        path = safe_path(wiki.root, item['path'])
        repo = gitops.owner(path, wiki.root)
        if str(repo) != item['repo']:
            raise WikiError('Git ownership changed since preparation')
        repos.setdefault(repo, set()).add(path.relative_to(repo).as_posix())
        for parent, link, child in gitops.ancestors(repo, wiki.root):
            repos.setdefault(parent, set()).add(link)
            links[(parent, link)] = child
    return repos, links


def validate_artifacts(wiki, journal):
    for item in journal['artifacts']:
        path = safe_path(wiki.root, item['path'])
        if not wiki.owner(path):
            raise WikiError('destination space disappeared')
        current = sha256(path.read_bytes()) if path.is_file() else None
        if current not in (item['before'], item['after']):
            raise WikiError(f'destination changed during operation: {item["path"]}')
        if sha256(base64.b64decode(item['data'])) != item['after']:
            raise WikiError('journal artifact hash mismatch')


def start(wiki, runtime, ident, payload, artifacts, adopted=(), extra_repos=()):
    journal = dict(schema_version=1, root=str(wiki.root), operation_id=ident,
                   fingerprint=fingerprint(payload), status='incomplete',
                   artifacts=artifacts, commits=[], tasks={}, heads={})
    repos, _ = groups(wiki, artifacts)
    for repo in extra_repos:
        repos.setdefault(repo, set())
    for repo in repos:
        allowed = {p.relative_to(repo).as_posix() for p in adopted if p.is_relative_to(repo)}
        gitops.clean_except(repo, allowed)
        for status, relative in gitops.dirty(repo):
            if relative in allowed and status != '??':
                raise WikiError('only an untracked intended inbox source may be adopted')
        journal['heads'][str(repo)] = gitops.run(repo, 'rev-parse', 'HEAD').strip()
    runtime.store(journal)
    return journal


def existing(runtime, ident, payload):
    journal = runtime.load(ident)
    if journal and journal['fingerprint'] != fingerprint(payload):
        raise WikiError('operation id reused with changed inputs; conflict')
    return journal


def owned_artifacts(wiki, journal):
    items = list(journal['artifacts'])
    for task in journal['tasks'].values():
        path = safe_path(wiki.root, task['path'])
        if not path.is_file() or sha256(path.read_bytes()) != task['sha256']:
            raise WikiError(f'ruwana task changed during operation: {task["path"]}')
        items.append(dict(path=task['path'], after=task['sha256'],
                          repo=str(gitops.owner(path, wiki.root))))
    return items


def apply_files(wiki, runtime, journal):
    validate_artifacts(wiki, journal)
    owned = owned_artifacts(wiki, journal)
    repos, links = groups(wiki, owned)
    by_path = {item['path']: item for item in owned}
    for repo, allowed in repos.items():
        current_head = gitops.run(repo, 'rev-parse', 'HEAD').strip()
        previous_head = journal['heads'].get(str(repo))
        if previous_head is None:
            raise WikiError('repository was not part of prepared operation')
        if current_head != previous_head:
            # A crash can occur after Git commits and before the journal checkpoint.
            parent_head = gitops.run(repo, 'rev-parse', 'HEAD^', check=False).strip()
            subject = gitops.run(repo, 'log', '-1', '--format=%s').strip()
            changed = set(gitops.run(repo, 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD').splitlines())
            if parent_head != previous_head or subject != f'wiki: {journal["operation_id"]}' or not changed <= allowed:
                raise WikiError(f'Git HEAD changed outside operation: {repo}')
            journal['heads'][str(repo)] = current_head
            runtime.store(journal)
        gitops.clean_except(repo, allowed)
        for _, relative in gitops.dirty(repo):
            if (repo, relative) in links:
                continue
            item = by_path[(repo / relative).relative_to(wiki.root).as_posix()]
            path = safe_path(wiki.root, item['path'])
            if not path.is_file() or sha256(path.read_bytes()) != item['after']:
                raise WikiError(f'dirty operation path was edited: {relative}')
    for item in journal['artifacts']:
        path = safe_path(wiki.root, item['path'])
        data = base64.b64decode(item['data'])
        if not path.is_file() or sha256(path.read_bytes()) != item['after']:
            atomic_bytes(path, data)
    # Owners first, then parent gitlinks; each checkpoint survives partial completion.
    for repo in sorted(repos, key=lambda p: len(p.parts), reverse=True):
        gitops.clean_except(repo, repos[repo])
        commit = gitops.commit(repo, repos[repo], journal['operation_id'])
        if commit:
            journal['commits'].append(dict(repo=str(repo), commit=commit))
        journal['heads'][str(repo)] = gitops.run(repo, 'rev-parse', 'HEAD').strip()
        runtime.store(journal)


def result(journal):
    return {key: journal[key] for key in ('operation_id', 'status', 'commits', 'tasks')}


def capture_operation(space, ident):
    # Dash-joined kebab ids are ambiguous (acme/alpha-beta vs acme-alpha/beta);
    # a hash of the exact triple keeps distinct captures apart.
    digest = sha256(json.dumps([space.domain, space.id, ident]).encode())[:12]
    return f'capture-{space.domain}-{space.id}-{ident}-{digest}'


def verify_capture(wiki, runtime, space, ident, source, digest):
    operation = capture_operation(space, ident)
    payload = dict(space=space.address, id=ident, source=str(external_path(source)), sha256=digest)
    journal = existing(runtime, operation, payload)
    if journal is None or journal['status'] != 'complete':
        raise WikiError('capture is not durably complete')
    raw = safe_path(space.path, f'sources/captures/{ident}/original')
    descriptor = safe_path(space.path, f'inbox/{ident}.md')
    if not raw.is_file() or sha256(raw.read_bytes()) != digest:
        raise WikiError('captured original changed or is missing')
    meta = metadata(descriptor.read_text(encoding='utf-8'))
    if not meta or meta.get('id') != ident or meta.get('source_sha256') != digest:
        raise WikiError('capture descriptor identity/hash conflict')
    validate_meta(meta, space, wiki)
    return journal


def capture(wiki, directory, space_address, ident, source, expected=None):
    space = wiki.space(space_address)
    concrete_id(ident)
    source = check_source_domain(wiki, space, source)
    source, data, digest = source_bytes(source, expected)
    operation = capture_operation(space, ident)
    payload = dict(space=space.address, id=ident, source=str(source), sha256=digest)
    runtime = Runtime(directory, wiki.root)
    with runtime.lock():
        journal = existing(runtime, operation, payload)
        if journal is not None and journal['status'] == 'complete':
            # A subsequent save legitimately updates the queue descriptor to processed.
            return result(verify_capture(wiki, runtime, space, ident, source, digest))
        if journal is None:
            docs, errors = scan(wiki)
            if errors:
                raise WikiError('; '.join(errors))
            if any(d.reference == f'wiki:{space.address}:{ident}' for d in docs):
                raise WikiError('duplicate document id')
            relative = f'sources/captures/{ident}/original'
            now = datetime.now(timezone.utc).isoformat()
            text = ('+++\nschema_version = 1\n'
                    f'id = {json.dumps(ident)}\ntype = "source"\ntitle = {json.dumps(ident)}\n'
                    f'space_ids = {json.dumps([space.address])}\nsources = {json.dumps([relative])}\n'
                    f'imported_at = {json.dumps(now)}\nsource_sha256 = {json.dumps(digest)}\n'
                    'ingest_status = "pending"\n'
                    f'original_name = {json.dumps(source.name)}\n+++\n\n'
                    '# Captured source\n\nOriginal bytes preserved; semantic processing is a separate operation.\n')
            artifacts = [artifact(wiki, space, relative, data),
                         artifact(wiki, space, f'inbox/{ident}.md', text.encode())]
            adopted = []
            if source.is_relative_to(space.path / 'inbox') and wiki.owner(source) == space:
                repo = gitops.owner(source, wiki.root)
                source_relative = source.relative_to(repo).as_posix()
                if any(status == '??' and name == source_relative for status, name in gitops.dirty(repo)):
                    artifacts.append(artifact(wiki, space, source.relative_to(space.path).as_posix(), data, digest))
                    adopted.append(source)
            journal = start(wiki, runtime, operation, payload, artifacts, adopted)
            journal['source_path'] = str(source)
            runtime.store(journal)
        try:
            source_bytes(source, digest)
            apply_files(wiki, runtime, journal)
            journal['status'] = 'complete'
            journal.pop('error', None)
            runtime.store(journal)
            return result(journal)
        except (WikiError, OSError) as error:
            journal['status'] = 'incomplete'
            journal['error'] = str(error)
            runtime.store(journal)
            raise


def read_plan(path):
    path = outside_git(path)
    if not path.is_file():
        raise WikiError('plan must be an external JSON file')
    plan = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(plan, dict) or type(plan.get('schema_version')) is not int or plan['schema_version'] != 1:
        raise WikiError('plan schema_version must be 1')
    return plan


def validate_plan(wiki, plan):
    concrete_id(plan.get('operation_id'), 'operation_id')
    owner = wiki.space(plan.get('space_id'))
    if 'capture_id' in plan:
        concrete_id(plan['capture_id'], 'capture_id')
    meeting = plan.get('meeting')
    if not isinstance(meeting, dict):
        raise WikiError('meeting must be an object')
    concrete_id(meeting.get('id'), 'meeting.id')
    if meeting['id'] == plan.get('capture_id'):
        raise WikiError('meeting.id and capture_id must differ (distinct meeting/source documents)')
    if not isinstance(meeting.get('title'), str) or not meeting['title'].strip():
        raise WikiError('meeting.title must be nonempty')
    body = meeting.get('body')
    if not isinstance(body, str) or not body.strip() or '{{' in body or '}}' in body:
        raise WikiError('meeting.body must be filled Markdown')
    if '## Ruwana references' in body:
        raise WikiError('Ruwana references section is generated from verified tasks')
    related = meeting.get('related_space_ids', [])
    if not isinstance(related, list):
        raise WikiError('related_space_ids must be array')
    for ident in related:
        if wiki.space(ident).domain != owner.domain:
            raise WikiError('cross-domain meeting relation')
    if owner.address in related or len(set(related)) != len(related):
        raise WikiError('duplicate owner/related space ids')
    if 'occurred_at' in meeting:
        value = meeting['occurred_at']
        if not isinstance(value, str) or not DATE.fullmatch(value):
            raise WikiError('occurred_at must be quoted RFC3339 with offset')
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    source = plan.get('source')
    if not isinstance(source, dict) or not isinstance(source.get('path'), str) or not Path(source['path']).is_absolute():
        raise WikiError('source.path must be absolute explicit file')
    if not isinstance(source.get('sha256'), str) or len(source['sha256']) != 64:
        raise WikiError('source.sha256 required')
    check_source_domain(wiki, owner, source['path'])
    tasks = plan.get('tasks', [])
    pages = plan.get('pages', [])
    if not isinstance(tasks, list) or not isinstance(pages, list):
        raise WikiError('tasks/pages must be arrays')
    ids = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise WikiError('task must be object')
        ident = concrete_id(task.get('id'), 'task.id')
        if ident in ids:
            raise WikiError('duplicate task id')
        ids.add(ident)
        target = wiki.space(task.get('space_id'))
        if target.domain != owner.domain:
            raise WikiError('task target must be a space in the meeting domain')
        if target.address not in [owner.address, *related]:
            raise WikiError('task target must be owner or related space')
        if task.get('agreed') is not True:
            raise WikiError('task requires agreed=true; proposals must not create tasks')
        if not isinstance(task.get('title'), str) or not task['title'].strip():
            raise WikiError('task title required')
        if 'description' in task and not isinstance(task['description'], str):
            raise WikiError('task description must be string')
        if 'due' in task:
            if not isinstance(task['due'], str) or not DATE.fullmatch(task['due']):
                raise WikiError('task due must be explicit RFC3339 with offset')
            datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
    for page in pages:
        if not isinstance(page, dict):
            raise WikiError('page must be object')
        target = wiki.space(page.get('space_id'))
        if target.domain != owner.domain:
            raise WikiError('cross-domain derived page')
        path = safe_path(target.path, page.get('path'))
        if path.suffix != '.md' or wiki.owner(path) != target:
            raise WikiError('page must be Markdown within selected owner')
        if path.relative_to(target.path).parts[0] in ('sources', 'inbox', 'meetings', '.ruwana'):
            raise WikiError('derived pages cannot rewrite sources/meetings/tasks')
        content = page.get('content')
        if not isinstance(content, str):
            raise WikiError('page content must be Markdown')
        meta = metadata(content)
        if meta is None:
            raise WikiError('page front matter required')
        validate_meta(meta, target, wiki)
        if meta['type'] in ('meeting', 'source'):
            raise WikiError('source/meeting cannot be a derived page')
    return owner


def meeting_text(plan, imported_at, tasks=None):
    meeting = plan['meeting']
    relative = f'sources/meetings/{meeting["id"]}/transcript.md'
    values = dict(schema_version=1, id=meeting['id'], type='meeting', title=meeting['title'],
                  imported_at=imported_at,
                  space_ids=[plan['space_id'], *meeting.get('related_space_ids', [])], sources=[relative])
    if 'occurred_at' in meeting:
        values['occurred_at'] = meeting['occurred_at']
    header = '\n'.join(f'{k} = {json.dumps(v, ensure_ascii=False)}' for k, v in values.items())
    text = '+++\n' + header + '\n+++\n\n' + meeting['body'].rstrip() + '\n\n## Ruwana references\n\n'
    if tasks is None:
        text += f'Pending: operation `{plan["operation_id"]}` is incomplete. No task IDs claimed.\n'
    elif not tasks:
        text += 'No agreed tasks to create.\n'
    else:
        for key, task in tasks.items():
            text += f'- `{task["project"]}` task `{task["id"]}` — `{task["source"]}`\n'
    return text + '\nCurrent task status is authoritative only in ruwana.\n'


def verify_completed_save(wiki, journal, plan, binary, reference):
    """Verify current files/tasks without changing any completed journal or data."""
    from .ruwana import Ruwana
    current_tasks = {}
    tasks = plan.get('tasks', [])
    if tasks:
        adapter = Ruwana(wiki, binary)
        for task in tasks:
            found = adapter.find(wiki.space(task['space_id']), task, reference)
            recorded = journal['tasks'].get(task['id'])
            if not found or not recorded or found['id'] != recorded['id']:
                raise WikiError('completed ruwana task is missing or its identity changed')
            current_tasks[task['id']] = found
    verified = {**journal, 'tasks': current_tasks}
    owned = owned_artifacts(wiki, verified)
    repos, _ = groups(wiki, owned)
    for repo in repos:
        gitops.clean_except(repo)
    for item in journal['artifacts']:
        path = safe_path(wiki.root, item['path'])
        if not path.is_file() or sha256(path.read_bytes()) != item['after']:
            raise WikiError('completed operation artifact is missing/changed; preserve later edits')
    return result(verified)


def save(wiki, directory, plan_path, binary='ruwana'):
    from .ruwana import Ruwana
    plan = read_plan(plan_path)
    owner = validate_plan(wiki, plan)
    source, data, digest = source_bytes(plan['source']['path'], plan['source']['sha256'])
    runtime = Runtime(directory, wiki.root)
    operation = plan['operation_id']
    reference = f'wiki:{owner.address}:{plan["meeting"]["id"]}'
    with runtime.lock():
        journal = existing(runtime, operation, plan)
        if journal is not None and journal['status'] == 'complete':
            try:
                return verify_completed_save(wiki, journal, plan, binary, reference)
            except (WikiError, OSError, ValueError) as error:
                raise WikiError(f'{operation}: completed history retained; current verification failed: {error}') from None
        fresh = journal is None
        if fresh:
            docs, errors = scan(wiki)
            if errors:
                raise WikiError('; '.join(errors))
            if any(d.reference == reference for d in docs):
                raise WikiError('duplicate meeting id')
            now = datetime.now(timezone.utc).isoformat()
            summary = meeting_text(plan, now)
            artifacts = [artifact(wiki, owner, f'sources/meetings/{plan["meeting"]["id"]}/transcript.md', data),
                         artifact(wiki, owner, f'meetings/{plan["meeting"]["id"]}.md', summary.encode())]
            for page in plan.get('pages', []):
                target = wiki.space(page['space_id'])
                artifacts.append(artifact(wiki, target, page['path'], page['content'].encode(), page.get('base_sha256')))
            if 'capture_id' in plan:
                capture_id = plan['capture_id']
                descriptor = safe_path(owner.path, f'inbox/{capture_id}.md')
                raw = safe_path(owner.path, f'sources/captures/{capture_id}/original')
                meta = metadata(descriptor.read_text(encoding='utf-8'))
                if source != raw or not meta or meta.get('source_sha256') != digest or meta.get('ingest_status') != 'pending':
                    raise WikiError('capture_id must identify a pending descriptor with the selected captured original')
                validate_meta(meta, owner, wiki)
                captured = descriptor.read_bytes()
                artifacts.append(artifact(wiki, owner, descriptor.relative_to(owner.path).as_posix(), captured, sha256(captured)))
            paths = [a['path'] for a in artifacts]
            if len(set(paths)) != len(paths):
                raise WikiError('duplicate destination paths')
            # Validate future metadata and source links against the combined existing/proposed tree.
            from .documents import Document
            proposed = []
            for item in artifacts[1:]:
                path = safe_path(wiki.root, item['path'])
                content = base64.b64decode(item['data']).decode()
                meta = metadata(content)
                target = wiki.owner(path)
                validate_meta(meta, target, wiki)
                proposed.append(Document(path, target, meta, content))
            replaced = set(paths)
            refs = [d.reference for d in [*[d for d in docs if d.path.relative_to(wiki.root).as_posix() not in replaced], *proposed]]
            if len(set(refs)) != len(refs):
                raise WikiError('duplicate document id in proposed plan')
            future = [d for d in docs if d.path.relative_to(wiki.root).as_posix() not in replaced] + proposed
            available = {safe_path(wiki.root, a['path']) for a in artifacts}
            reference_errors = [error for doc in future for error in check_references(wiki, doc, future, available)]
            if reference_errors:
                raise WikiError('; '.join(reference_errors))
            extra_repos = set()
            for task in plan.get('tasks', []):
                target = wiki.space(task['space_id'])
                repo = gitops.owner(target.path, wiki.root)
                extra_repos.add(repo)
                gitops.clean_except(repo)
                for parent, _, _ in gitops.ancestors(repo, wiki.root):
                    extra_repos.add(parent)
                    gitops.clean_except(parent)
                safe_path(target.path, '.ruwana')
            journal = start(wiki, runtime, operation, plan, artifacts, extra_repos=extra_repos)
            journal['imported_at'] = now
            runtime.store(journal)
        try:
            source_bytes(source, digest)
            tasks = plan.get('tasks', [])
            adapter = None
            if fresh:
                apply_files(wiki, runtime, journal)
            if tasks:
                adapter = Ruwana(wiki, binary)
                # Recover tasks created before a process died prior to journal checkpoint.
                for task in tasks:
                    found = adapter.find(wiki.space(task['space_id']), task, reference)
                    if found:
                        old = journal['tasks'].get(task['id'])
                        if old and old['id'] != found['id']:
                            raise WikiError('ruwana identity changed since checkpoint')
                        journal['tasks'][task['id']] = found
                    elif task['id'] in journal['tasks']:
                        raise WikiError('recorded ruwana task is missing; refusing to recreate')
                runtime.store(journal)
            if not fresh:
                apply_files(wiki, runtime, journal)
            for task in tasks:
                if task['id'] not in journal['tasks']:
                    journal['tasks'][task['id']] = adapter.ensure(wiki.space(task['space_id']), task, reference)
                    runtime.store(journal)
            apply_files(wiki, runtime, journal)
            content = meeting_text(plan, journal['imported_at'], journal['tasks']).encode()
            item = journal['artifacts'][1]
            if item['after'] != sha256(content):
                item['before'] = item['after']
                item['after'] = sha256(content)
                item['data'] = base64.b64encode(content).decode()
                runtime.store(journal)
                apply_files(wiki, runtime, journal)
            if 'capture_id' in plan:
                capture_path = f'{owner.path.relative_to(wiki.root).as_posix()}/inbox/{plan["capture_id"]}.md'
                item = next(a for a in journal['artifacts'] if a['path'] == capture_path)
                text = base64.b64decode(item['data']).decode()
                if 'ingest_status = "pending"' in text:
                    text = text.replace('ingest_status = "pending"',
                                        'ingest_status = "processed"\n' +
                                        f'processed_by = {json.dumps(operation)}\n' +
                                        f'meeting_reference = {json.dumps(reference)}', 1)
                    item['before'] = item['after']
                    item['after'] = sha256(text.encode())
                    item['data'] = base64.b64encode(text.encode()).decode()
                    runtime.store(journal)
                    apply_files(wiki, runtime, journal)
            journal['status'] = 'complete'
            journal.pop('error', None)
            runtime.store(journal)
            return result(journal)
        except (WikiError, OSError, ValueError) as error:
            journal['status'] = 'incomplete'
            journal['error'] = str(error)
            runtime.store(journal)
            raise WikiError(f'{operation}: incomplete: {error}') from None
