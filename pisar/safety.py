"""Path and metadata boundary checks shared by readers and writers."""
import hashlib
from pathlib import Path
import re


class WikiError(Exception):
    pass


def concrete_id(value, field='id'):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', value):
        raise WikiError(f'{field}: expected concrete lowercase kebab id')
    return value


def safe_path(root, relative):
    """Reject traversal, symlinks (including internal ones), Git internals."""
    if not isinstance(relative, str) or not relative or '\\' in relative or '\0' in relative:
        raise WikiError('invalid relative path')
    path = Path(relative)
    if path.is_absolute() or any(p in ('', '.', '..') for p in relative.split('/')):
        raise WikiError(f'unsafe path: {relative}')
    if any(p == '.git' for p in path.parts):
        raise WikiError('Git internals are not wiki paths')
    root = Path(root).resolve()
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise WikiError(f'symlink path refused: {relative}')
    if not current.resolve().is_relative_to(root):
        raise WikiError(f'path escapes root: {relative}')
    return current


def sha256(data):
    return hashlib.sha256(data).hexdigest()
