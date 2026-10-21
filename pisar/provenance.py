"""Portable processing provenance derived from committed wiki Git data."""
from dataclasses import dataclass
import re

from . import gitops
from .documents import metadata
from .safety import WikiError, sha256


_PENDING_MEETING = re.compile(
    rb'Pending: operation `[a-z0-9][a-z0-9-]*` is incomplete\. No task IDs claimed\.')


def _has_pending_meeting_status(blob):
    """Read the active generated status line, allowing harmless Markdown formatting edits."""
    lines = blob.replace(b'\r\n', b'\n').replace(b'\r', b'\n').split(b'\n')
    headings = [index for index, line in enumerate(lines) if line.strip() == b'## Ruwana references']
    if not headings:
        return False
    section = lines[headings[-1] + 1:]
    status = next((line.strip() for line in section if line.strip()), b'')
    return _PENDING_MEETING.fullmatch(status) is not None


@dataclass(frozen=True)
class ProcessedSource:
    space_address: str
    source_sha256: str
    source_path: str
    meeting_reference: str
    capture_reference: str | None


@dataclass(frozen=True)
class ProcessingInventory:
    processed: tuple[ProcessedSource, ...]
    incomplete: tuple[tuple[str, str, str], ...]


def _committed_blob(wiki, path, revision=None):
    space = wiki.owner(path)
    if space is None:
        return None
    repo = gitops.owner(path, wiki.root)
    relative = path.relative_to(repo).as_posix()
    tracked = gitops.run(repo, 'ls-files', '--error-unmatch', '-z', '--', relative, check=False)
    if not tracked:
        return None
    try:
        return gitops.blob(repo, f'{revision or "HEAD"}:{relative}')
    except WikiError:
        return None


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def committed_processing(wiki, *, revision=None):
    """Inventory one committed revision's capture descriptors and meeting source links.

    The working tree supplies paths/space ownership only. Every document and
    source byte used as evidence is read from its owning repository's HEAD or
    the supplied Git tree-ish.
    """
    wiki.require_valid()
    documents = []
    for path in wiki.files():
        if path.suffix.lower() != '.md' or wiki.owner(path) is None:
            continue
        blob = _committed_blob(wiki, path, revision)
        if blob is None:
            continue
        try:
            content = blob.decode('utf-8')
        except UnicodeDecodeError:
            continue
        meta = metadata(content)
        if meta is not None:
            documents.append((path, wiki.owner(path), meta))

    processed = []
    incomplete = []
    processed_meetings = set()
    meeting_originals = {}
    pending_meetings = set()
    for meeting_path, meeting_space, meeting_meta in documents:
        if meeting_meta.get('type') != 'meeting':
            continue
        meeting_ref = f'wiki:{meeting_space.address}:{meeting_meta.get("id", "")}'
        meeting_blob = _committed_blob(wiki, meeting_path, revision)
        if meeting_blob is not None and _has_pending_meeting_status(meeting_blob):
            pending_meetings.add((meeting_space.address, meeting_ref))
        sources = meeting_meta.get('sources', [])
        for source in sources if isinstance(sources, list) else ():
            if not isinstance(source, str) or not source.startswith('sources/meetings/'):
                continue
            original = _committed_blob(wiki, meeting_space.path / source.split('#', 1)[0], revision)
            if original is not None:
                meeting_originals.setdefault((meeting_space.address, meeting_ref), set()).add(sha256(original))
    for path, space, meta in documents:
        if meta.get('type') != 'source' or 'source_sha256' not in meta:
            continue
        digest = meta.get('source_sha256')
        if not _digest(digest):
            incomplete.append((space.address, str(digest), 'invalid committed source hash'))
            continue
        meeting_reference = meta.get('meeting_reference')
        capture_reference = f'wiki:{space.address}:{meta.get("id", "")}'
        if meta.get('ingest_status') != 'processed':
            incomplete.append((space.address, digest, 'committed capture processing is pending/incomplete'))
            continue
        if (not isinstance(meeting_reference, str)
                or not re.fullmatch(rf'wiki:{re.escape(space.address)}:[a-z0-9][a-z0-9-]*', meeting_reference)
                or not isinstance(meta.get('processed_by'), str) or not meta['processed_by']):
            incomplete.append((space.address, digest, 'committed processed capture lacks meeting/journal identity'))
            continue
        if digest not in meeting_originals.get((space.address, meeting_reference), set()):
            incomplete.append((space.address, digest,
                               'committed processed capture has no matching committed meeting source'))
            continue
        if (space.address, meeting_reference) in pending_meetings:
            incomplete.append((space.address, digest,
                               'committed capture points to pending meeting processing'))
            continue
        originals = [source for source in meta.get('sources', [])
                     if isinstance(source, str) and source.startswith('sources/captures/')]
        if len(originals) != 1:
            incomplete.append((space.address, digest, 'committed processed capture lacks one original source link'))
            continue
        original_path = space.path / originals[0]
        original = _committed_blob(wiki, original_path, revision)
        if original is None or sha256(original) != digest:
            incomplete.append((space.address, digest, 'committed original source is absent or has a different hash'))
            continue
        processed.append(ProcessedSource(space.address, digest, originals[0], meeting_reference,
                                         capture_reference))
        processed_meetings.add((space.address, meeting_reference))

    for path, space, meta in documents:
        if meta.get('type') != 'meeting':
            continue
        reference = f'wiki:{space.address}:{meta.get("id", "")}'
        if (space.address, reference) in processed_meetings:
            continue
        pending = (space.address, reference) in pending_meetings
        sources = meta.get('sources', [])
        for source in sources if isinstance(sources, list) else ():
            if not isinstance(source, str) or not source.startswith('sources/meetings/'):
                continue
            source_path = space.path / source.split('#', 1)[0]
            blob = _committed_blob(wiki, source_path, revision)
            if blob is None:
                incomplete.append((space.address, '', 'committed meeting source link is missing'))
                continue
            digest = sha256(blob)
            if pending:
                incomplete.append((space.address, digest, 'committed meeting save is pending/incomplete'))
            else:
                processed.append(ProcessedSource(space.address, digest, source, reference, None))
    return ProcessingInventory(tuple(processed), tuple(incomplete))


def find_processed(inventory, space_address: str, source_sha256: str) -> list[ProcessedSource]:
    """Find exact-byte processing evidence in one owning space only."""
    if not isinstance(space_address, str) or '/' not in space_address:
        raise WikiError('expected domain/space address for provenance lookup')
    if not _digest(source_sha256):
        raise WikiError('source_sha256 must be lowercase SHA-256')
    incomplete = [reason for scope, digest, reason in inventory.incomplete
                  if scope == space_address and digest in ('', source_sha256)]
    if incomplete:
        raise WikiError('committed processing is incomplete: ' + '; '.join(sorted(set(incomplete))))
    return sorted((item for item in inventory.processed
                   if item.space_address == space_address and item.source_sha256 == source_sha256),
                  key=lambda item: (item.meeting_reference, item.capture_reference or '', item.source_path))
