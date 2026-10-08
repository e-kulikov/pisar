"""External-only proposal snapshots; explicit per-item routing and acceptance."""
import json
from pathlib import Path
from .operations import (JOURNAL_FORMAT, Runtime, legacy_error, atomic_bytes, capture, fingerprint, outside_git,
                         read_plan, save, source_bytes, validate_plan, verify_capture, write_json)
from .safety import WikiError, concrete_id, safe_path, sha256


def batch_dir(runtime, batch):
    concrete_id(batch, 'batch_id')
    path = safe_path(runtime.base, f'triage/{batch}')
    outside_git(path)
    return path


def load(runtime, batch):
    directory = batch_dir(runtime, batch)
    path = safe_path(directory, 'manifest.json')
    if not path.is_file():
        raise WikiError(f'unknown triage batch: {batch}')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('format') != JOURNAL_FORMAT:
        raise legacy_error(f'triage batch {batch}', path, path.parent)
    if manifest.get('root') != str(runtime.root) or manifest.get('batch_id') != batch:
        raise WikiError('triage root namespace/batch mismatch')
    return directory, manifest


def persist(directory, manifest):
    write_json(safe_path(directory, 'manifest.json'), manifest)
    lines = [f'# Triage {manifest["batch_id"]}', '',
             'Proposals remain external until explicit accept. No semantic routing is performed.', '']
    for item in manifest['items']:
        lines.append(f'- `{item["id"]}`: {item["status"]}; destination `{item.get("space_id") or "unknown"}`; original `{item["source"]}`')
        if item.get('error'):
            lines.append(f'  Error: {item["error"]}')
    atomic_bytes(safe_path(directory, 'report.md'), ('\n'.join(lines) + '\n').encode())


def public(directory, manifest):
    return dict(batch_id=manifest['batch_id'], root=manifest['root'], directory=str(directory),
                status=manifest['status'], items=manifest['items'])


def prepare(wiki, directory, batch, plan_path=None, inbox=None):
    runtime = Runtime(directory, wiki.root)
    dest = batch_dir(runtime, batch)
    with runtime.lock():
        if plan_path:
            routing = read_plan(plan_path)
            items = routing.get('items')
            inbox_path = None
        else:
            inbox_path = outside_git(inbox or runtime.base / 'inbox')
            if inbox_path.is_relative_to(wiki.root):
                raise WikiError('global inbox must be outside wiki root')
            inbox_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            items = []
            for source in sorted(inbox_path.iterdir()):
                if source.is_symlink():
                    raise WikiError('global inbox symlink refused')
                if not source.is_file():
                    raise WikiError('global inbox accepts immediate regular files only')
                _, data, digest = source_bytes(source)
                ident = 'item-' + sha256((str(source) + digest).encode())[:16]
                items.append(dict(id=ident, source=str(source), sha256=digest, space_id=None))
            routing = dict(schema_version=1, items=items, inbox=str(inbox_path))
        if not isinstance(items, list):
            raise WikiError('routing items must be array')
        fp = fingerprint(routing)
        if dest.exists():
            _, prior = load(runtime, batch)
            if prior['fingerprint'] != fp:
                raise WikiError('batch id reused with changed inputs; prepare a new batch')
            if prior['status'] == 'prepared':
                return public(dest, prior)
        entries, snapshots, drafts = [], {}, {}
        ids = set()
        sources, hashes = set(), set()
        for item in items:
            if not isinstance(item, dict):
                raise WikiError('routing item must be object')
            ident = concrete_id(item.get('id'), 'item.id')
            if ident in ids:
                raise WikiError('duplicate triage item id')
            ids.add(ident)
            source = outside_git(item.get('source', ''))
            if not isinstance(item.get('sha256'), str):
                raise WikiError('item.sha256 required')
            source, data, digest = source_bytes(source, item['sha256'])
            if source in sources or digest in hashes:
                raise WikiError('duplicate source identity/content in batch; keep one canonical item')
            sources.add(source)
            hashes.add(digest)
            space_id = item.get('space_id')
            if space_id is not None:
                wiki.space(space_id)
            proposed = item.get('plan')
            if proposed is not None:
                validate_plan(wiki, proposed)
                if proposed['space_id'] != space_id or Path(proposed['source']['path']).resolve() != source or proposed['source']['sha256'] != digest:
                    raise WikiError('draft plan source/target must match routing proposal')
                if 'capture_id' in proposed:
                    raise WikiError('triage assigns capture_id after acceptance')
            draft = dict(space_id=space_id, plan=proposed)
            draft_bytes = (json.dumps(draft, ensure_ascii=False, indent=2) + '\n').encode()
            entry = dict(id=ident, source=str(source), sha256=digest, space_id=space_id,
                         status='pending', draft_sha256=sha256(draft_bytes),
                         archive_inbox=bool(inbox_path and source.parent == inbox_path),
                         processed_path=str(safe_path(runtime.base, f'processed/{batch}/{ident}/original')))
            entries.append(entry)
            snapshots[ident] = data
            drafts[ident] = draft_bytes
        manifest = dict(schema_version=1, format=JOURNAL_FORMAT, root=str(wiki.root), batch_id=batch,
                        fingerprint=fp, status='preparing', items=entries)
        if not dest.exists():
            dest.mkdir(parents=True, mode=0o700)
        persist(dest, manifest)
        for entry in entries:
            ident = entry['id']
            atomic_bytes(safe_path(dest, f'originals/{ident}'), snapshots[ident])
            atomic_bytes(safe_path(dest, f'drafts/{ident}.json'), drafts[ident])
        manifest['status'] = 'prepared'
        persist(dest, manifest)
        return public(dest, manifest)


def report(wiki, directory, batch):
    runtime = Runtime(directory, wiki.root)
    with runtime.lock():
        dest, manifest = load(runtime, batch)
        return public(dest, manifest)


def entry_for(manifest, ident):
    concrete_id(ident, 'item_id')
    matches = [e for e in manifest['items'] if e['id'] == ident]
    if len(matches) != 1:
        raise WikiError('unknown/ambiguous triage item')
    return matches[0]


def checked_draft(dest, entry):
    path = safe_path(dest, f'drafts/{entry["id"]}.json')
    data = path.read_bytes()
    if sha256(data) != entry['draft_sha256']:
        raise WikiError('triage draft changed since prepare/reroute')
    draft = json.loads(data)
    if draft.get('space_id') != entry['space_id']:
        raise WikiError('triage draft/manifest destination conflict')
    snapshot = safe_path(dest, f'originals/{entry["id"]}')
    if not snapshot.is_file() or sha256(snapshot.read_bytes()) != entry['sha256']:
        raise WikiError('triage source snapshot hash conflict')
    return draft


def reroute(wiki, directory, batch, ident, space_id):
    wiki.space(space_id)
    runtime = Runtime(directory, wiki.root)
    with runtime.lock():
        dest, manifest = load(runtime, batch)
        entry = entry_for(manifest, ident)
        if entry['status'] not in ('pending', 'deferred'):
            raise WikiError(f'cannot reroute {entry["status"]} item; retry accept with its original inputs or diagnose/roll back partial work')
        draft = checked_draft(dest, entry)
        if entry['space_id'] != space_id and draft.get('plan'):
            # A new owner requires a new semantic plan; do not silently transfer derived pages/tasks.
            draft['plan'] = None
            entry['note'] = 'Reroute discarded old meeting proposal; accepted source will remain pending for fresh ingest.'
        draft['space_id'] = space_id
        data = (json.dumps(draft, ensure_ascii=False, indent=2) + '\n').encode()
        atomic_bytes(safe_path(dest, f'drafts/{ident}.json'), data)
        entry.update(space_id=space_id, status='pending', draft_sha256=sha256(data))
        persist(dest, manifest)
        return dict(batch_id=batch, item=entry)


def defer(wiki, directory, batch, ident):
    runtime = Runtime(directory, wiki.root)
    with runtime.lock():
        dest, manifest = load(runtime, batch)
        entry = entry_for(manifest, ident)
        if entry['status'] not in ('pending', 'deferred'):
            raise WikiError(f'cannot defer {entry["status"]} item; retry accept with its original inputs or diagnose/roll back partial work')
        entry['status'] = 'deferred'
        persist(dest, manifest)
        return dict(batch_id=batch, item=entry)


def accept(wiki, directory, batch, ident, binary='ruwana'):
    runtime = Runtime(directory, wiki.root)
    with runtime.lock():
        dest, manifest = load(runtime, batch)
        if manifest['status'] != 'prepared':
            raise WikiError('batch preparation incomplete; rerun prepare')
        entry = entry_for(manifest, ident)
        processed = outside_git(entry['processed_path'])
        expected_processed = safe_path(runtime.base, f'processed/{batch}/{ident}/original')
        if processed != expected_processed:
            raise WikiError('processed namespace path conflict')
        if entry['status'] == 'complete':
            source_bytes(processed, entry['sha256'])
            verify_capture(wiki, runtime, wiki.space(entry['space_id']), ident, entry['source'], entry['sha256'])
            return dict(batch_id=batch, item=entry)
        if entry['status'] == 'deferred':
            raise WikiError('deferred item requires explicit reroute before accept')
        if entry.get('space_id') is None:
            raise WikiError('destination unknown; reroute before accept')
        target = wiki.space(entry['space_id'])
        draft = checked_draft(dest, entry)
        source = outside_git(entry['source'])
        # A crash during verified archiving can leave only processed bytes.
        current = source if source.exists() else processed
        _, data, digest = source_bytes(current, entry['sha256'])
        entry['status'] = 'applying'
        persist(dest, manifest)
        try:
            if source.exists():
                capture(wiki, directory, target.address, ident, source, digest)
            elif entry.get('archive_inbox'):
                verify_capture(wiki, runtime, target, ident, source, digest)
            else:
                raise WikiError('external original disappeared; restore it before retry')
            if draft.get('plan'):
                plan = draft['plan']
                plan['capture_id'] = ident
                plan['source'] = dict(path=str(safe_path(target.path, f'sources/captures/{ident}/original')), sha256=digest)
                apply_path = safe_path(dest, f'accepted/{ident}.json')
                write_json(apply_path, plan)
                save(wiki, directory, apply_path, binary)
            # Capture/save verify commits and task identities before the inbox original is archived.
            if processed.exists():
                source_bytes(processed, digest)
            else:
                atomic_bytes(processed, data)
                source_bytes(processed, digest)
            if entry.get('archive_inbox') and source.exists():
                source_bytes(source, digest)
                source.unlink()
            entry['status'] = 'complete'
            entry.pop('error', None)
            persist(dest, manifest)
            return dict(batch_id=batch, item=entry)
        except (WikiError, OSError, ValueError) as error:
            entry['status'] = 'incomplete'
            entry['error'] = str(error)
            persist(dest, manifest)
            raise WikiError(f'{batch}/{ident}: incomplete: {error}') from None
