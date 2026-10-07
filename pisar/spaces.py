"""Discover spaces from files; never persist a registry or read symlinks."""
from dataclasses import dataclass
import os
from pathlib import Path
import tomllib
from .safety import WikiError, concrete_id, safe_path


SKIP = {'.git', '.ruwana', '__pycache__'}


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

    @property
    def address(self):
        return f'{self.domain}/{self.id}'

    def record(self, root):
        return dict(id=self.id, domain=self.domain, address=self.address,
                    path=self.path.relative_to(root).as_posix(), kind=self.kind, status=self.status)


class Wiki:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise WikiError(f'wiki root is not a directory: {self.root}')
        self.errors = []
        self.domains = {}
        self.spaces = []
        for base in sorted(self.root.iterdir()):
            if base.name.startswith('.') or not base.is_dir():
                continue
            marker = base / '.domain.toml'
            name = marker.relative_to(self.root).as_posix()
            if base.is_symlink():
                if marker.exists():
                    self.errors.append(f'{base.name}: symlink domain refused')
                continue
            if marker.is_symlink():
                self.errors.append(f'{name}: symlink refused')
                continue
            if not marker.exists():
                continue  # Not a domain: docs, schemas, templates, ...
            try:
                if not marker.is_file():
                    raise WikiError('marker must be a regular file')
                self.domains[base.name] = load_domain(base)
            except (WikiError, ValueError, OSError) as error:
                self.errors.append(f'{name}: {error}')
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
                    self.spaces.append(Space(ident, path.parent, domain.id, meta['kind'], meta['status']))
                except (WikiError, ValueError, OSError) as error:
                    self.errors.append(f'{path.relative_to(self.root)}: {error}')
        seen = set()
        for s in self.spaces:
            if s.id in seen:
                self.errors.append(f'duplicate space id: {s.id}')
            seen.add(s.id)

    def require_valid(self):
        if self.errors:
            raise WikiError('; '.join(self.errors))

    def space(self, ident, scope='all'):
        self.require_valid()
        matches = [s for s in self.spaces if s.id == ident]
        if len(matches) != 1:
            raise WikiError(f'unknown or ambiguous space: {ident}')
        s = matches[0]
        if scope != 'all' and s.domain != scope:
            raise WikiError(f'scope violation: {ident}')
        return s

    def owner(self, path):
        matches = [s for s in self.spaces if path.is_relative_to(s.path)]
        return max(matches, key=lambda s: len(s.path.parts)) if matches else None

    def files(self, scope='all'):
        for domain in self.domains.values():
            if scope in ('all', domain.id):
                yield from walk_files(domain.path)

    def path(self, relative, scope='all'):
        path = safe_path(self.root, relative)
        parts = Path(relative).parts
        if parts[0] not in self.domains or (scope != 'all' and parts[0] != scope):
            raise WikiError('path outside requested domains')
        if not self.owner(path):
            raise WikiError('path is not in a discovered space')
        return path
