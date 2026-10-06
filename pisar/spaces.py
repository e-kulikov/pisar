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


@dataclass(frozen=True)
class Space:
    id: str
    path: Path
    scope: str
    kind: str
    status: str

    def record(self, root):
        return dict(id=self.id, path=self.path.relative_to(root).as_posix(),
                    scope=self.scope, kind=self.kind, status=self.status)


class Wiki:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise WikiError(f'wiki root is not a directory: {self.root}')
        self.errors = []
        self.spaces = []
        for scope in ('personal', 'work'):
            base = self.root / scope
            if base.is_symlink():
                self.errors.append(f'{scope}: symlink namespace refused')
                continue
            for path in walk_files(base):
                if path.name != '.wiki.toml':
                    continue
                try:
                    meta = tomllib.loads(path.read_text(encoding='utf-8'))
                    if type(meta.get('schema_version')) is not int or meta['schema_version'] != 1:
                        raise WikiError('schema_version must be 1')
                    ident = concrete_id(meta.get('id'))
                    if meta.get('kind') not in ('project', 'area', 'resource'):
                        raise WikiError('kind must be project/area/resource')
                    if meta.get('status') not in ('active', 'archived'):
                        raise WikiError('status must be active/archived')
                    self.spaces.append(Space(ident, path.parent, scope, meta['kind'], meta['status']))
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
        if scope != 'all' and s.scope != scope:
            raise WikiError(f'scope violation: {ident}')
        return s

    def owner(self, path):
        matches = [s for s in self.spaces if path.is_relative_to(s.path)]
        return max(matches, key=lambda s: len(s.path.parts)) if matches else None

    def files(self, scope='all'):
        for domain in ('personal', 'work'):
            if scope not in ('all', domain) or (self.root / domain).is_symlink():
                continue
            yield from walk_files(self.root / domain)

    def path(self, relative, scope='all'):
        path = safe_path(self.root, relative)
        parts = Path(relative).parts
        if parts[0] not in ('personal', 'work') or (scope != 'all' and parts[0] != scope):
            raise WikiError('path outside requested personal/work scope')
        if not self.owner(path):
            raise WikiError('path is not in a discovered space')
        return path
