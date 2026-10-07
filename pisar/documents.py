"""TOML front matter, stable references, validation of real documents."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import tomllib
from .safety import WikiError, concrete_id, safe_path, sha256


TYPES = {'source', 'meeting', 'overview', 'decision', 'note', 'recipe', 'movie', 'series', 'book'}
REF = re.compile(r'wiki:([a-z0-9][a-z0-9-]*):([a-z0-9][a-z0-9-]*)(?:#[\w:.-]+)?')
DATE = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)')


def metadata(content):
    lines = content.splitlines()
    if not lines or lines[0] != '+++':
        return None
    try:
        end = lines.index('+++', 1)
    except ValueError:
        raise WikiError('front matter lacks standalone +++ closing line') from None
    try:
        return tomllib.loads('\n'.join(lines[1:end]))
    except tomllib.TOMLDecodeError as error:
        raise WikiError(f'invalid TOML front matter: {error}') from None


def validate_meta(meta, owner, wiki):
    if type(meta.get('schema_version')) is not int or meta['schema_version'] != 1:
        raise WikiError('schema_version must be 1')
    concrete_id(meta.get('id'))
    if meta.get('type') not in TYPES:
        raise WikiError('unsupported document type')
    if not isinstance(meta.get('title'), str) or not meta['title'].strip():
        raise WikiError('title must be nonempty string')
    if '{{' in repr(meta) or '}}' in repr(meta):
        raise WikiError('unresolved template placeholder')
    ids = meta.get('space_ids')
    if not isinstance(ids, list) or not ids or ids[0] != owner.id:
        raise WikiError('space_ids must start with actual owner')
    for ident in ids:
        concrete_id(ident, 'space_ids')
        related = wiki.space(ident)
        if related.domain != owner.domain:
            raise WikiError('cross-domain related space')
    if len(set(ids)) != len(ids):
        raise WikiError('duplicate space_ids')
    if not isinstance(meta.get('sources'), list) or not all(isinstance(s, str) and s for s in meta['sources']):
        raise WikiError('sources must be array of nonempty strings')
    for field in ('occurred_at', 'imported_at', 'updated_at'):
        if field in meta:
            value = meta[field]
            if not isinstance(value, str) or not DATE.fullmatch(value):
                raise WikiError(f'{field}: expected quoted RFC3339 with offset')
            try:
                datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                raise WikiError(f'{field}: invalid timestamp') from None


@dataclass
class Document:
    path: Path
    owner: object
    meta: dict
    content: str

    @property
    def reference(self):
        return f'wiki:{self.owner.id}:{self.meta["id"]}'

    def record(self, root):
        return dict(reference=self.reference, path=self.path.relative_to(root).as_posix(),
                    metadata=self.meta, sha256=sha256(self.path.read_bytes()))


def scan(wiki, scope='all'):
    docs, errors = [], []
    seen = set()
    for path in wiki.files(scope):
        owner = wiki.owner(path)
        if path.suffix.lower() != '.md' or owner is None:
            continue
        # Sources are immutable opaque originals, even if they look like TOML.
        if path.relative_to(owner.path).parts[0] == 'sources':
            continue
        try:
            content = path.read_text(encoding='utf-8')
            meta = metadata(content)
            if meta is None:
                continue
            validate_meta(meta, owner, wiki)
            doc = Document(path, owner, meta, content)
            if doc.reference in seen:
                raise WikiError(f'duplicate document id: {doc.reference}')
            seen.add(doc.reference)
            docs.append(doc)
        except (WikiError, ValueError, OSError) as error:
            errors.append(f'{path.relative_to(wiki.root)}: {error}')
    return docs, errors


def resolve(wiki, reference, scope='all'):
    match = REF.fullmatch(reference)
    if not match:
        raise WikiError('expected wiki:<space-id>:<document-id>')
    owner = wiki.space(match[1], scope)
    docs, errors = scan(wiki, owner.domain)
    if errors:
        raise WikiError('; '.join(errors))
    matches = [d for d in docs if d.owner.id == owner.id and d.meta['id'] == match[2]]
    if len(matches) != 1:
        raise WikiError(f'unknown or ambiguous reference: {reference}')
    return matches[0]


def check_references(wiki, doc, docs, available=()):
    errors = []
    by_ref = {d.reference: d for d in docs}
    refs = set(m.group(0).split('#')[0] for m in REF.finditer(doc.content))
    for ref in refs:
        target = by_ref.get(ref)
        if target is None:
            errors.append(f'{doc.reference}: unresolved reference {ref}')
        elif target.owner.domain != doc.owner.domain:
            errors.append(f'{doc.reference}: cross-domain reference {ref}')
    for source in doc.meta['sources']:
        if source.startswith('wiki:'):
            if not REF.fullmatch(source):
                errors.append(f'{doc.reference}: invalid source reference {source}')
            continue
        if '://' in source or source.startswith('urn:'):
            continue  # Identifiers/URLs are provenance, not verified local evidence.
        relative = source.split('#', 1)[0]
        try:
            path = safe_path(doc.owner.path, relative)
            if not path.is_file() and path not in available:
                raise WikiError(f'missing source: {source}')
            other = wiki.owner(path)
            if other is not None and other != doc.owner:
                raise WikiError('source crosses space ownership')
        except WikiError as error:
            errors.append(f'{doc.reference}: {error}')
    return errors


def check(wiki, scope='all'):
    docs, errors = scan(wiki, scope)
    errors = [*wiki.errors, *errors]
    # Read other domain only when requested; domain-crossing references remain unresolved errors.
    for doc in docs:
        errors.extend(check_references(wiki, doc, docs))
    import os
    for domain in wiki.domains.values():
        if scope not in ('all', domain.id):
            continue
        for parent, dirs, files in os.walk(domain.path, followlinks=False):
            dirs[:] = [d for d in dirs if d not in ('.git', '.ruwana')]
            for name in [*dirs, *files]:
                if (Path(parent) / name).is_symlink():
                    errors.append(f'{Path(parent, name).relative_to(wiki.root)}: symlink refused')
    return dict(ok=not errors, errors=errors, documents=len(docs), spaces=len(wiki.spaces),
                limitations=['External source identifiers are not verified; no semantic analysis.'])
