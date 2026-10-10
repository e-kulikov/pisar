"""Discover spaces from files; never persist a registry or read symlinks."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tomllib
from .safety import WikiError, address, concrete_id, safe_path


SKIP = {'.git', '.ruwana', '__pycache__'}
# Document types pisar itself writes (capture sources, meeting records). Every other type is declared by its space.
SYSTEM_TYPES = frozenset({'source', 'meeting'})
# Front matter every document has; a space cannot declare a field with one of these names.
BASE_FIELDS = frozenset({'schema_version', 'id', 'type', 'title', 'space_ids', 'sources',
                         'occurred_at', 'imported_at', 'updated_at'})
FIELD_TYPES = ('string', 'integer', 'boolean', 'date', 'list')
TYPE_NAME = re.compile(r'[a-z][a-z0-9-]*')


def walk_files(root):
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if name != '.git' and not path.is_symlink():
                yield path


LAYOUT = dict(project='10-projects', area='20-areas', resource='30-resources',
              archive='40-archives', inbox='inbox')
MARKER_KEYS = {'schema_version', 'id', 'title', 'layout', 'ids', 'sensitive'}


@dataclass(frozen=True)
class Domain:
    id: str
    path: Path
    title: str
    layout: dict
    min_segments: int
    aliases: tuple
    terms: tuple

    def record(self, root):
        return dict(id=self.id, title=self.title, path=self.path.relative_to(root).as_posix(),
                    layout=dict(self.layout))


def _table(meta, key, allowed):
    value = meta.get(key, {})
    if not isinstance(value, dict):
        raise WikiError(f'[{key}] must be a table')
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise WikiError(f'unknown key in [{key}]: {", ".join(unknown)}')
    return value


def load_domain(path):
    """Validate a .domain.toml marker; path is the domain directory."""
    meta = tomllib.loads((path / '.domain.toml').read_text(encoding='utf-8'))
    unknown = sorted(set(meta) - MARKER_KEYS)
    if unknown:
        raise WikiError(f'unknown key: {", ".join(unknown)}')
    if type(meta.get('schema_version')) is not int or meta['schema_version'] != 1:
        raise WikiError('schema_version must be 1')
    ident = concrete_id(meta.get('id'))
    if ident != path.name:
        raise WikiError(f'id {ident} must equal the directory name {path.name}')
    title = meta.get('title')
    if not isinstance(title, str) or not title.strip():
        raise WikiError('title must be nonempty string')
    layout = {**LAYOUT, **_table(meta, 'layout', LAYOUT)}
    for kind, folder in layout.items():
        if (not isinstance(folder, str) or not folder or '\\' in folder or folder.startswith('/')
                or any(p in ('', '.', '..', '.git') for p in folder.split('/'))):
            raise WikiError(f'layout.{kind}: expected a relative folder inside the domain')
    if len(set(layout.values())) != len(layout):
        raise WikiError('layout folders must be distinct')
    segments = _table(meta, 'ids', ('min_segments',)).get('min_segments', 1)
    if type(segments) is not int or segments < 1:
        raise WikiError('ids.min_segments must be a positive integer')
    sensitive = _table(meta, 'sensitive', ('aliases', 'terms'))
    lists = {}
    for key in ('aliases', 'terms'):
        value = sensitive.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise WikiError(f'sensitive.{key} must be an array of nonempty strings')
        lists[key] = tuple(value)
    return Domain(ident, path, title, layout, segments, lists['aliases'], lists['terms'])


@dataclass(frozen=True)
class Space:
    id: str
    path: Path
    domain: str
    kind: str
    status: str
    types: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def address(self):
        return f'{self.domain}/{self.id}'

    def record(self, root):
        return dict(id=self.id, domain=self.domain, address=self.address,
                    path=self.path.relative_to(root).as_posix(), kind=self.kind, status=self.status)


@dataclass(frozen=True)
class Field:
    name: str
    type: str = 'string'
    required: bool = False
    values: tuple = None  # None: any value of the type; () allows nothing


@dataclass(frozen=True)
class RecordType:
    name: str
    description: str
    template: str = None  # None: no body skeleton; '' is an explicit empty one
    fields: tuple = ()

    def record(self):
        return dict(name=self.name, description=self.description, template=self.template,
                    fields=[dict(name=f.name, type=f.type, required=f.required,
                                 values=None if f.values is None else list(f.values))
                            for f in self.fields])


def _values(path, kind, values):
    if values is None:
        return None
    if kind == 'boolean':
        raise WikiError(f'{path}.values: boolean fields take no values')
    if not isinstance(values, list):
        raise WikiError(f'{path}.values: expected an array')
    if not all(isinstance(v, str) or type(v) is int for v in values):
        raise WikiError(f'{path}.values: expected strings or integers')
    if kind in ('string', 'date') and not all(isinstance(v, str) for v in values):
        raise WikiError(f'{path}.values: {kind} fields take string values')
    if kind == 'integer' and not all(type(v) is int for v in values):
        raise WikiError(f'{path}.values: integer fields take integer values')
    return tuple(values)


def parse_type(name, raw, key='types'):
    """Validate one declaration; KEY is the TOML path shown in errors. Shared by loading and `space type add`."""
    where = f'{key}.{name}'
    if not isinstance(name, str) or not TYPE_NAME.fullmatch(name):
        raise WikiError(f'{where}: name must match [a-z][a-z0-9-]*')
    if name in SYSTEM_TYPES:
        raise WikiError(f'{where}: reserved system type')
    if not isinstance(raw, dict):
        raise WikiError(f'{where}: expected a table')
    unknown = sorted(set(raw) - {'description', 'template', 'fields'})
    if unknown:
        raise WikiError(f'unknown key in {where}: {", ".join(unknown)}')
    description = raw.get('description')
    if description is None:
        raise WikiError(f'{where}.description: required')
    if not isinstance(description, str) or not description.strip():
        raise WikiError(f'{where}.description: nonempty string required')
    template = raw.get('template')
    if template is not None and not isinstance(template, str):
        raise WikiError(f'{where}.template: expected a string')
    fields = raw.get('fields', {})
    if not isinstance(fields, dict):
        raise WikiError(f'{where}.fields: expected a table')
    parsed = []
    for fname, spec in fields.items():
        path = f'{where}.fields.{fname}'
        if not isinstance(fname, str) or not fname.strip():
            raise WikiError(f'{where}.fields: field name must be nonempty')
        if fname != fname.strip():
            raise WikiError(f'{where}.fields.{fname}: field name must not have surrounding whitespace')
        if fname in BASE_FIELDS:
            raise WikiError(f'{path}: reserved field name')
        if not isinstance(spec, dict):
            raise WikiError(f'{path}: expected a table')
        unknown = sorted(set(spec) - {'type', 'required', 'values'})
        if unknown:
            raise WikiError(f'unknown key in {path}: {", ".join(unknown)}')
        kind = spec.get('type', 'string')
        if kind not in FIELD_TYPES:
            raise WikiError(f'{path}.type: unsupported field type {kind!r}; use {"/".join(FIELD_TYPES)}')
        required = spec.get('required', False)
        if type(required) is not bool:
            raise WikiError(f'{path}.required: expected true or false')
        values = _values(path, kind, spec.get('values'))
        parsed.append(Field(fname, kind, required, values))
    return RecordType(name, description, template, tuple(sorted(parsed, key=lambda f: f.name)))


def parse_types(meta):
    """The `[types.*]` declarations of a parsed .wiki.toml; absent means none, malformed fails closed."""
    raw = meta.get('types', {})
    if not isinstance(raw, dict):
        raise WikiError('types must be a table')
    return {name: parse_type(name, spec) for name, spec in raw.items()}


class Wiki:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise WikiError(f'wiki root is not a directory: {self.root}')
        # Problems are kept per top-level directory so a selection can skip them.
        self.problems = {}
        self.marked = set()  # Every directory carrying a marker, valid or not.
        self.domains = {}
        self.spaces = []
        for base in sorted(self.root.iterdir()):
            if base.name.startswith('.') or not base.is_dir():
                continue
            marker = base / '.domain.toml'
            name = marker.relative_to(self.root).as_posix()
            if not (marker.exists() or marker.is_symlink()):
                continue  # Not a domain: docs, schemas, templates, ...
            self.marked.add(base.name)
            if base.is_symlink():
                self.report(base.name, f'{base.name}: symlink domain refused')
                continue
            if marker.is_symlink():
                self.report(base.name, f'{name}: symlink refused')
                continue
            try:
                if not marker.is_file():
                    raise WikiError('marker must be a regular file')
                self.domains[base.name] = load_domain(base)
            except (WikiError, ValueError, OSError) as error:
                self.report(base.name, f'{name}: {error}')
        for domain in self.domains.values():
            for path in walk_files(domain.path):
                if path.name != '.wiki.toml':
                    continue
                try:
                    if path.parent == domain.path:
                        raise WikiError('a domain directory cannot itself be a space')
                    meta = tomllib.loads(path.read_text(encoding='utf-8'))
                    if type(meta.get('schema_version')) is not int or meta['schema_version'] != 1:
                        raise WikiError('schema_version must be 1')
                    ident = concrete_id(meta.get('id'))
                    if meta.get('kind') not in ('project', 'area', 'resource'):
                        raise WikiError('kind must be project/area/resource')
                    if meta.get('status') not in ('active', 'archived'):
                        raise WikiError('status must be active/archived')
                    declared = parse_types(meta)
                    self.spaces.append(Space(ident, path.parent, domain.id, meta['kind'], meta['status'], declared))
                except (WikiError, ValueError, OSError) as error:
                    self.report(domain.id, f'{path.relative_to(self.root)}: {error}')
        seen = set()
        for s in self.spaces:
            if s.address in seen:
                self.report(s.domain, f'duplicate space id: {s.address}')
            seen.add(s.address)

    def report(self, name, message):
        self.problems.setdefault(name, []).append(message)

    def errors_in(self, domains=None):
        """Marker and space problems of the selected domains (all when None)."""
        return [m for name, messages in sorted(self.problems.items())
                if domains is None or name in domains for m in messages]

    @property
    def errors(self):
        return self.errors_in()

    def require_valid(self, domains=None):
        errors = self.errors_in(domains)
        if errors:
            raise WikiError('; '.join(errors))

    def select(self, include=None, exclude=None):
        """Domain ids chosen by comma-separated, repeatable --include/--exclude.

        Neither means all domains; exclude applies after include."""
        def ids(values):
            chosen = [part.strip() for value in values for part in value.split(',')]
            for ident in chosen:
                if ident not in self.marked:
                    raise WikiError(f'unknown domain: {ident}')
            return set(chosen)
        selected = ids(include) if include else set(self.marked)
        return frozenset(selected - ids(exclude or ()))

    def space(self, value, domains=None):
        """Resolve a `domain/id` address; ids are unique within their domain."""
        self.require_valid(domains)
        address(value)
        matches = [s for s in self.spaces if s.address == value]
        if len(matches) != 1:
            raise WikiError(f'unknown space: {value}')
        s = matches[0]
        if domains is not None and s.domain not in domains:
            raise WikiError(f'space {value} is outside the selected domains')
        return s

    def owner(self, path):
        matches = [s for s in self.spaces if path.is_relative_to(s.path)]
        return max(matches, key=lambda s: len(s.path.parts)) if matches else None

    def selected(self, domains=None):
        return [d for d in self.domains.values() if domains is None or d.id in domains]

    def files(self, domains=None):
        for domain in self.selected(domains):
            yield from walk_files(domain.path)

    def path(self, relative, domains=None):
        path = safe_path(self.root, relative)
        parts = Path(relative).parts
        if parts[0] not in self.domains or (domains is not None and parts[0] not in domains):
            raise WikiError('path outside the selected domains')
        if not self.owner(path):
            raise WikiError('path is not in a discovered space')
        return path
