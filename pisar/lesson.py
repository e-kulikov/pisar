"""Lessons: a reviewed, explicitly accepted note carried from one domain to a space.

A batch lives outside Git and outside the wiki, in `<state>/lessons/<batch>/`:
`draft.md` (edited by the agent), `meta.json`, immutable `revisions/rN.md`,
`rN.report.json` (guard findings), `rN.review.json` (reviewer verdict) and
`journal.json`. The guard and the reviewer only REPORT. Accepting writes exactly
the reviewed revision; nothing is ever stripped or rewritten.
"""
from datetime import datetime, timezone
import difflib
import json
import re
import shutil
import unicodedata
import uuid
from . import gitops, guard
from .documents import Document, check_references, metadata, scan, validate_meta
from .operations import (Runtime, apply_files, remove_leftovers, artifact, atomic_bytes, existing, external_path,
                         outside_git, result as operation_result, start as start_operation, write_json)
from .safety import WikiError, address, concrete_id, safe_path, sha256

TIERS = ('severe', 'ask', 'warn')
VERDICTS = ('clear', 'concerns', 'block')
REVIEW_KEYS = {'schema_version', 'revision', 'sha256', 'verdict', 'findings', 'reviewer'}
REVIEW_REQUIRED = ('tier', 'category', 'excerpt', 'comment')
REVIEW_FINDING_KEYS = {*REVIEW_REQUIRED, 'suggestion'}


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, what):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise WikiError(f'{what} is not readable JSON: {error}') from None


class Batch:
    """One lesson workspace, resolved and validated for a wiki and a state directory."""

    def __init__(self, wiki, state, ident):
        self.wiki = wiki
        self.runtime = Runtime(state, wiki.root)
        self.ident = concrete_id(ident, 'batch')
        self.dir = safe_path(self.runtime.base, f'lessons/{ident}')
        outside_git(self.dir)
        self.archive = safe_path(self.runtime.path, f'lessons/{ident}.json')

    def file(self, name):
        return safe_path(self.dir, name)

    def exists(self):
        return self.dir.exists() or self.archive.exists()

    def load(self):
        path = self.file('meta.json')
        if not path.is_file():
            raise WikiError(f'unknown lesson batch: {self.ident}')
        meta = read_json(path, 'meta.json')
        if meta.get('root') != str(self.wiki.root) or meta.get('batch') != self.ident:
            raise WikiError('lesson batch belongs to another wiki root')
        return meta

    def save(self, meta):
        write_json(self.file('meta.json'), meta)

    def journal(self):
        path = self.file('journal.json')
        return read_json(path, 'journal.json') if path.is_file() else dict(schema_version=1, batch=self.ident, events=[])

    def record(self, meta, event, **fields):
        journal = self.journal()
        journal['events'].append(dict(at=now(), event=event, **fields))
        write_json(self.file('journal.json'), journal)
        self.sync_archive(meta, journal)
        return journal

    def sync_archive(self, meta, journal=None):
        # The retained copy survives discarding the workspace. Unchanged content is never rewritten.
        value = {**(journal or self.journal()), 'root': meta['root'], 'from': meta['from'],
                 'to': meta['to'], 'title': meta['title']}
        wanted = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        try:
            if self.archive.is_file() and self.archive.read_bytes() == wanted:
                return
        except OSError:
            pass
        write_json(self.archive, value)

    def current(self, meta):
        if not meta['revisions']:
            raise WikiError('no revision yet; run `pisar lesson check` first')
        rev = meta['revisions'][-1]
        data = self.file(f'revisions/{rev["revision"]}.md').read_bytes()
        if sha256(data) != rev['sha256']:
            raise WikiError(f'revision {rev["revision"]} changed on disk; revisions are immutable')
        return rev, data

    def draft(self):
        path = self.file('draft.md')
        if not path.is_file():
            raise WikiError('draft.md is missing')
        return path, path.read_bytes()

    def require_open(self, meta):
        if meta['status'] != 'open':
            raise WikiError(f'lesson batch {self.ident} is {meta["status"]}; start a new batch')

    def accepting(self):
        """The operation record once the acceptance has passed its commit point, else None."""
        journal = self.runtime.load(self.ident)
        return journal if journal is not None and acceptance_started(self.wiki, journal) else None

    def require_not_accepting(self):
        """Once an acceptance has started its revision is fixed: only `accept` may continue."""
        journal = self.accepting()
        if journal is not None:
            raise WikiError(f'the acceptance of {self.ident} has started; finish it by rerunning the same '
                            f'`pisar lesson accept --batch {self.ident}` (no new revision or review is possible)'
                            + self.conflict_hint(journal))

    def conflict_hint(self, journal):
        paths = conflicts(self.wiki, journal)
        return '. ' + conflict_message(self.ident, paths, journal.get('decision', {}).get('revision', '?')) if paths else ''


def acceptance_started(wiki, journal):
    """True once this acceptance changed any repository: the COMMIT POINT.

    Before it nothing outside the lesson workspace has changed, so the operation record is stale
    and the acceptance counts as not started. Started means: a commit was journaled, a repository
    HEAD moved to this operation's own commit, or the note file written by it is on disk."""
    if journal['status'] == 'complete' or journal['commits']:
        return True
    for repo, head in journal['heads'].items():
        if gitops.run(repo, 'rev-parse', 'HEAD').strip() != head and \
                gitops.run(repo, 'log', '-1', '--format=%s').strip() == f'wiki: {journal["operation_id"]}':
            return True
    for item in journal['artifacts']:
        path = safe_path(wiki.root, item['path'])
        # Anything but the recorded pre-operation state counts: our write, or a later edit of it.
        if path.is_file() and sha256(path.read_bytes()) != item['before']:
            return True
    return False


def conflicts(wiki, journal):
    """Artifact paths holding neither the pre-operation state nor the journaled bytes."""
    found = []
    for item in journal['artifacts']:
        path = safe_path(wiki.root, item['path'])
        if path.is_file() and sha256(path.read_bytes()) not in (item['before'], item['after']):
            found.append(item['path'])
    return found


def conflict_message(ident, paths, revision):
    where = ', '.join(paths)
    return (f'{where} was edited after pisar wrote it (or differs from the reviewed text). Pisar never commits '
            f'such edits and never overwrites them. Remove {where} (or restore it to the reviewed text) and '
            f'rerun `pisar lesson accept --batch {ident}`; the reviewed revision {revision} is kept')


def warnings(wiki, meta):
    origin, target = meta['from']['domain'], meta['to'].split('/')[0]
    if origin == target:
        return []
    title = lambda ident: wiki.domains[ident].title if ident in wiki.domains else ident
    return [f'CROSS-DOMAIN: this lesson carries text from domain {origin} ({title(origin)}) into domain '
            f'{target} ({title(target)}). Domains are confidentiality boundaries and between companies '
            'this may breach confidentiality; every decision to proceed must be the user\'s own.']


def start(wiki, state, source, to, title):
    if not isinstance(title, str) or not title.strip():
        raise WikiError('--title must be nonempty')
    if '/' in source:
        origin = wiki.space(address(source, '--from'), {source.split('/')[0]})
        domain, origin_address = origin.domain, origin.address
    else:
        domain, origin_address = concrete_id(source, '--from'), None
        if domain not in wiki.domains:
            raise WikiError(f'unknown domain: {domain}')
        wiki.require_valid({domain})
    target = wiki.space(address(to, '--to'), {to.split('/')[0]})
    if target.status != 'active':
        raise WikiError(f'target space {target.address} is archived')
    batch = Batch(wiki, state, 'lesson-' + uuid.uuid4().hex[:10])
    with batch.runtime.lock():
        if batch.exists():
            raise WikiError(f'lesson batch id collision: {batch.ident}; retry')
        batch.dir.mkdir(parents=True, mode=0o700)
        batch.file('revisions').mkdir(mode=0o700)
        atomic_bytes(batch.file('draft.md'), b'')
        meta = dict(schema_version=1, batch=batch.ident, root=str(wiki.root), status='open',
                    title=title.strip(), created_at=now(), revisions=[],
                    **{'from': dict(domain=domain, address=origin_address)}, to=target.address)
        batch.save(meta)
        batch.record(meta, 'start', **{'from': meta['from']}, to=meta['to'], title=meta['title'])
    return dict(batch=batch.ident, directory=str(batch.dir), draft=str(batch.file('draft.md')),
                title=meta['title'], to=meta['to'], warnings=warnings(wiki, meta), **{'from': meta['from']})


def diff(previous, text, old, new):
    return ''.join(difflib.unified_diff(previous.splitlines(True), text.splitlines(True), old, new))


def previous_text(batch, meta):
    if len(meta['revisions']) < 2:
        return None
    return batch.file(f'revisions/{meta["revisions"][-2]["revision"]}.md').read_text(encoding='utf-8')


def check(wiki, state, ident):
    batch = Batch(wiki, state, ident)
    with batch.runtime.lock():
        meta = batch.load()
        batch.require_open(meta)
        batch.require_not_accepting()
        path, data = batch.draft()
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            raise WikiError('draft.md must be UTF-8') from None
        if not text.strip():
            raise WikiError('draft.md is empty; write the lesson first')
        digest = sha256(data)
        unchanged = bool(meta['revisions']) and meta['revisions'][-1]['sha256'] == digest
        if unchanged:
            revision = meta['revisions'][-1]['revision']
            report = read_json(batch.file(f'{revision}.report.json'), f'{revision}.report.json')
        else:
            revision = f'r{len(meta["revisions"]) + 1}'
            atomic_bytes(batch.file(f'revisions/{revision}.md'), data)
            found = guard.check(wiki.root, text, meta['from']['domain'], meta['to'].split('/')[0])
            report = dict(schema_version=1, revision=revision, sha256=digest, created_at=now(), **found)
            write_json(batch.file(f'{revision}.report.json'), report)
            meta['revisions'].append(dict(revision=revision, sha256=digest))
            batch.save(meta)
            batch.record(meta, 'check', revision=revision, sha256=digest, findings=len(report['findings']))
        before = previous_text(batch, meta) if len(meta['revisions']) > 1 else None
    previous = f'r{len(meta["revisions"]) - 1}'
    return dict(batch=ident, revision=revision, sha256=digest, draft=str(path), unchanged=unchanged,
                findings=report['findings'], limits=report['limits'], warnings=warnings(wiki, meta),
                diff=diff(before, text, previous, revision) if before is not None else '')


def validate_review(review, current):
    if not isinstance(review, dict):
        raise WikiError('review must be a JSON object')
    unknown = sorted(set(review) - REVIEW_KEYS)
    missing = sorted(REVIEW_KEYS - set(review))
    if unknown or missing:
        raise WikiError(f'review keys: unknown {unknown}, missing {missing}')
    if type(review['schema_version']) is not int or review['schema_version'] != 1:
        raise WikiError('review schema_version must be 1')
    if review['verdict'] not in VERDICTS:
        raise WikiError(f'review verdict must be one of {", ".join(VERDICTS)}')
    reviewer = review['reviewer']
    if not isinstance(reviewer, dict) or not isinstance(reviewer.get('model'), str) or not reviewer['model'].strip():
        raise WikiError('review reviewer.model must be a nonempty string')
    findings = review['findings']
    if not isinstance(findings, list):
        raise WikiError('review findings must be an array')
    for item in findings:
        if not isinstance(item, dict) or not set(REVIEW_REQUIRED) <= set(item) or set(item) - REVIEW_FINDING_KEYS:
            raise WikiError(f'review finding needs {", ".join(REVIEW_REQUIRED)} (and optional suggestion)')
        if item['tier'] not in TIERS:
            raise WikiError(f'review finding tier must be one of {", ".join(TIERS)}')
        if not all(isinstance(value, str) for value in item.values()):
            raise WikiError('review finding fields must be strings')
    if review['revision'] != current['revision'] or review['sha256'] != current['sha256']:
        raise WikiError(f'stale review: it covers {review["revision"]} {str(review["sha256"])[:12]}, but the '
                        f'current revision is {current["revision"]} {current["sha256"][:12]}; '
                        'review the current revision')


def review(wiki, state, ident, file):
    batch = Batch(wiki, state, ident)
    with batch.runtime.lock():
        meta = batch.load()
        batch.require_open(meta)
        batch.require_not_accepting()
        path = external_path(file)
        if not path.is_file():
            raise WikiError(f'review file not found: {path}')
        value = read_json(path, 'review file')
        current, _ = batch.current(meta)
        validate_review(value, current)
        write_json(batch.file(f'{current["revision"]}.review.json'), value)
        batch.record(meta, 'review', revision=current['revision'], verdict=value['verdict'],
                     findings=len(value['findings']), reviewer=value['reviewer'])
    return dict(batch=ident, revision=current['revision'], verdict=value['verdict'],
                findings=len(value['findings']))


def show(wiki, state, ident):
    batch = Batch(wiki, state, ident)
    meta = batch.load()
    out = [f'Lesson {ident} ({meta["status"]})', f'Title: {meta["title"]}',
           f'From: {meta["from"]["address"] or meta["from"]["domain"]}', f'Destination: {meta["to"]}']
    out += ['', *warnings(wiki, meta)]
    if not meta['revisions']:
        return '\n'.join([*out, '', 'No revision yet: write the draft, then run `pisar lesson check`.']) + '\n'
    rev, data = batch.current(meta)
    revision = rev['revision']
    report = read_json(batch.file(f'{revision}.report.json'), 'report')
    review_path = batch.file(f'{revision}.review.json')
    reviewed = read_json(review_path, 'review') if review_path.is_file() else None
    started = batch.accepting() if meta['status'] == 'open' else None
    out += ['', f'revision: {revision}', f'sha256: {rev["sha256"]}']
    if started is not None:
        out.append(f'Acceptance in progress: rerun `pisar lesson accept --batch {ident}`; it resumes the persisted '
                   f'revision {revision} and draft.md is preserved, never written.' + batch.conflict_hint(started))
    elif meta['status'] == 'open' and batch.draft()[1] != data:
        out.append('The draft has unchecked changes: run `pisar lesson check`.')
    out += ['', '--- text ---', data.decode('utf-8').rstrip('\n'), '--- end ---', '', 'Findings by tier:']
    for tier in TIERS:
        for f in (f for f in report['findings'] if f['tier'] == tier):
            out.append(f'  [{tier}] {f["id"]} line {f["line"]} {f["category"]}: {f["excerpt"]}')
    if not report['findings']:
        out.append('  none')
    out.append(f'  ({report["limits"]})')
    suggestions = sorted({f['hint'] for f in report['findings']})
    if reviewed:
        suggestions += [f'{f["excerpt"]}: {f["suggestion"]}' for f in reviewed['findings'] if f.get('suggestion')]
    out += ['', 'Suggested generalisations:', *(f'  - {s}' for s in suggestions or ['none'])]
    out += ['', 'Review:']
    if reviewed:
        out.append(f'  verdict {reviewed["verdict"]} by {reviewed["reviewer"]["model"]} (advisory)')
        out += [f'  [{f["tier"]}] {f["category"]}: {f["excerpt"]} - {f["comment"]}' for f in reviewed['findings']]
    elif started is not None:
        out.append(f'  as decided when the acceptance started: verdict {started["decision"].get("verdict") or "none"}')
    else:
        out.append(f'  no review of {revision}: attach one with `pisar lesson review`')
    out += ['', 'Open decisions:']
    if started is not None:
        out.append('  - none to take: the acceptance already started and its decisions are recorded')
    if not reviewed and meta['status'] == 'open' and started is None:
        out.append('  - no review of the current revision (or accept with --skip-review)')
    kept = {i for e in batch.journal()['events'] if e['event'] == 'accept' for i in e['keeps']}
    open_ids = [f['id'] for f in report['findings'] if f['id'] not in kept]
    if open_ids and meta['status'] == 'open' and started is None:
        out.append(f'  - edit the draft or keep each finding: --keep {",".join(open_ids)}')
    if out[-1] == 'Open decisions:':
        out.append('  none')
    before = previous_text(batch, meta)
    if before is not None:
        changes = diff(before, data.decode('utf-8'), f'r{len(meta["revisions"]) - 1}', revision)
        out += ['', 'Diff against the previous revision:', changes.rstrip('\n') or '  (none)']
    return '\n'.join(out) + '\n'


def slug(title, fallback):
    ascii_title = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode()
    value = re.sub(r'[^a-z0-9]+', '-', ascii_title.lower()).strip('-')[:60].strip('-')
    return value or fallback


def note_text(meta, doc_id, body, mention):
    header = '\n'.join(f'{k} = {json.dumps(v, ensure_ascii=False)}' for k, v in dict(
        schema_version=1, id=doc_id, type='note', title=meta['title'], space_ids=[meta['to']], sources=[]).items())
    origin = f'Origin: {mention}\n\n' if mention else ''
    return f'+++\n{header}\n+++\n\n{origin}'.encode() + body


def decide(batch, meta, rev, data, keeps, skip_review):
    """Gate a FIRST acceptance on the workspace files; returns the review verdict (or None)."""
    if meta['status'] != 'open':
        raise WikiError(f'lesson batch {batch.ident} is {meta["status"]}')
    if batch.draft()[1] != data:
        raise WikiError(f'draft.md differs from {rev["revision"]}; run `pisar lesson check` first')
    verdict = None
    review_path = batch.file(f'{rev["revision"]}.review.json')
    if review_path.is_file():
        reviewed = read_json(review_path, 'review')
        if reviewed.get('sha256') != rev['sha256']:
            raise WikiError('stale review; review the current revision')
        verdict = reviewed['verdict']
    elif not skip_review:
        raise WikiError(f'no review of the current revision {rev["revision"]}: attach one with '
                        '`pisar lesson review`, or pass --skip-review (recorded)')
    report = read_json(batch.file(f'{rev["revision"]}.report.json'), 'report')
    ids = {f['id'] for f in report['findings']}
    unknown = sorted(set(keeps) - ids)
    if unknown:
        raise WikiError(f'--keep ids not found in {rev["revision"]}: {", ".join(unknown)}')
    open_findings = [f for f in report['findings'] if f['id'] not in keeps]
    if open_findings:
        lines = [f'{f["id"]} [{f["tier"]}] line {f["line"]} {f["category"]}: {f["excerpt"]}' for f in open_findings]
        raise WikiError('findings without a decision in the current revision (edit the draft and '
                        'run check, or list them in --keep): ' + '; '.join(lines))
    return verdict


def accept(wiki, state, ident, keep=(), mention_origin=False, skip_review=False):
    """Write the reviewed revision. Once an operation journal exists (resume or completed retry)
    the outcome is a pure function of meta.json, the operation journal (which persists the decision
    taken at the start) and the committed repository state: draft.md, revisions, reports and reviews
    are never read again."""
    batch = Batch(wiki, state, ident)
    keeps = sorted({part.strip() for value in keep for part in value.split(',') if part.strip()})
    with batch.runtime.lock():
        meta = batch.load()
        target = wiki.space(meta['to'], {meta['to'].split('/')[0]})
        if target.status != 'active':
            raise WikiError(f'target space {target.address} is archived')
        if not meta['revisions']:
            raise WikiError('no revision yet; run `pisar lesson check` first')
        rev = meta['revisions'][-1]
        doc_id = slug(meta['title'], ident)
        operation = concrete_id(ident, 'operation_id')
        payload = dict(batch=ident, revision=rev['revision'], sha256=rev['sha256'], to=target.address,
                       id=doc_id, keeps=keeps, mention_origin=bool(mention_origin), skip_review=bool(skip_review))
        stale = batch.runtime.load(operation)
        if stale is not None and not acceptance_started(wiki, stale):
            # Before the commit point nothing outside the workspace changed: start over, and keep a trace.
            batch.record(meta, 'restart', reason='stale operation record without any repository change',
                         previous_status=stale['status'], error=stale.get('error'))
            remove_leftovers(wiki, stale)
            stale = None
        journal = existing(batch.runtime, operation, payload) if stale is not None else None
        resumed = journal is not None
        if journal is not None and journal['status'] == 'complete':
            item = journal['artifacts'][0]
            path = safe_path(wiki.root, item['path'])
            if not path.is_file() or sha256(path.read_bytes()) != item['after']:
                raise WikiError('accepted note changed or is missing; later edits are preserved')
        if journal is None:
            _, data = batch.current(meta)
            verdict = decide(batch, meta, rev, data, keeps, skip_review)
            title = wiki.domains[meta['from']['domain']].title if mention_origin else None
            relative = f'notes/{doc_id}.md'
            text = note_text(meta, doc_id, data, title)
            docs, errors = scan(wiki, {target.domain})
            if errors:
                raise WikiError('; '.join(errors))
            if any(d.reference == f'wiki:{target.address}:{doc_id}' for d in docs):
                raise WikiError(f'duplicate document id: wiki:{target.address}:{doc_id}')
            proposed = metadata(text.decode('utf-8'))
            validate_meta(proposed, target, wiki)
            artifacts = [artifact(wiki, target, relative, text)]
            # The body is checked too: the same cross-domain and reference rules as `pisar check`.
            future = Document(safe_path(wiki.root, artifacts[0]['path']), target, proposed, text.decode('utf-8'))
            problems = check_references(wiki, future, [*docs, future])
            if problems:
                raise WikiError('the text would not pass `pisar check` (nothing was changed; edit the draft and '
                                'run `pisar lesson check` again): ' + '; '.join(problems))
            decision = dict(revision=rev['revision'], keeps=keeps, skip_review=bool(skip_review),
                            mention_origin=bool(mention_origin), verdict=verdict)
            journal = start_operation(wiki, batch.runtime, operation, payload, artifacts,
                                      extra=dict(decision=decision))
        decision = journal.get('decision', {})
        if resumed and journal['status'] != 'complete':
            clash = conflicts(wiki, journal)
            if clash:
                raise WikiError(conflict_message(ident, clash, rev['revision']))
        try:
            if journal['status'] != 'complete':
                apply_files(wiki, batch.runtime, journal)
                journal['status'] = 'complete'
                journal.pop('error', None)
                batch.runtime.store(journal)
        except (WikiError, OSError) as error:
            journal['status'] = 'incomplete'
            journal['error'] = str(error)
            batch.runtime.store(journal)
            raise
        item = journal['artifacts'][0]
        verdict = decision.get('verdict')
        # Finalization is idempotent: a retry repairs a missing event, status or retained journal.
        if any(e['event'] == 'accept' for e in batch.journal()['events']):
            try:
                batch.sync_archive(meta)
            except OSError:
                pass  # Archive upkeep never fails an acceptance that is already finalized.
        else:
            batch.record(meta, 'accept', revision=rev['revision'], sha256=rev['sha256'], keeps=keeps,
                         skip_review=bool(skip_review), mention_origin=bool(mention_origin), verdict=verdict,
                         path=item['path'], operation_id=operation, commits=journal['commits'])
        if meta['status'] != 'accepted':
            meta['status'] = 'accepted'
            batch.save(meta)
        notes = []
        if resumed:
            notes.append(f'resumed from the persisted revision {rev["revision"]}: draft.md was not read, is '
                         'ignored and left untouched')
        return dict(batch=ident, revision=rev['revision'], path=item['path'], notes=notes,
                    reference=f'wiki:{target.address}:{doc_id}', keeps=keeps, skip_review=bool(skip_review),
                    mention_origin=bool(mention_origin), verdict=verdict, warnings=warnings(wiki, meta),
                    **operation_result(journal))


def discard(wiki, state, ident):
    batch = Batch(wiki, state, ident)
    with batch.runtime.lock():
        meta = batch.load()
        pending = batch.accepting()
        if pending is None and (record := batch.runtime.load(ident)) is not None:
            remove_leftovers(wiki, record)  # A stale record: only its own half-written file goes.
        recorded = any(e['event'] == 'accept' for e in batch.journal()['events'])
        if pending is not None and (pending['status'] != 'complete' or meta['status'] != 'accepted' or not recorded):
            raise WikiError(f'the acceptance of {ident} is not finalized; finish it by rerunning the same '
                            f'`pisar lesson accept --batch {ident}` (the workspace is needed to resume), '
                            'then discard' + batch.conflict_hint(pending))
        batch.record(meta, 'discard', status=meta['status'])
        shutil.rmtree(batch.dir)
    return dict(batch=ident, discarded=True, journal=str(batch.archive))
