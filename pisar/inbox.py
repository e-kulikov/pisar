"""Passive descriptor-level reading and listing for the global inbox."""

import errno
import os
from pathlib import Path
import stat
import sys

from .safety import WikiError

TRUNCATION_LIMIT = 65536
BUFFER_LIMIT = TRUNCATION_LIMIT + 1
TRUNCATION_MARKER = '\n[pisar: inbox item truncated; limit 65536 bytes]\n'


def validate_id(name: str) -> str:
    """Validate that an inbox filename contains no slashes, traversal, or control chars."""
    if not isinstance(name, str) or not name or not name.strip():
        raise WikiError('inbox item id cannot be empty or whitespace')
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise WikiError('inbox item id cannot contain control characters')
    if '/' in name or '\\' in name or '\x00' in name:
        raise WikiError('inbox item id cannot contain path separators or NUL bytes')
    if name.startswith('.'):
        raise WikiError('inbox item id cannot start with a dot')
    if '..' in name:
        raise WikiError('inbox item id cannot contain parent directory traversal')
    return name


def list_items(state_dir: Path | str) -> list[str]:
    """Return sorted filenames of regular files in <state-dir>/inbox/."""
    inbox_dir = Path(state_dir) / 'inbox'
    dir_flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        dfd = os.open(inbox_dir, dir_flags)
    except (FileNotFoundError, OSError):
        return []
    items = []
    try:
        with os.scandir(dfd) as entries:
            for entry in entries:
                name = entry.name
                try:
                    validate_id(name)
                except WikiError:
                    continue
                if entry.is_symlink():
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode):
                        items.append(name)
                except OSError:
                    continue
    except OSError:
        return []
    finally:
        os.close(dfd)
    return sorted(items)


def read_item(state_dir: Path | str, ident: str) -> str:
    """Read up to 64 KiB of UTF-8 content from <state-dir>/inbox/ID."""
    ident = validate_id(ident)
    inbox_dir = Path(state_dir) / 'inbox'
    dir_flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        dfd = os.open(inbox_dir, dir_flags)
    except FileNotFoundError:
        raise WikiError(f'inbox directory {inbox_dir} does not exist') from None
    except OSError as exc:
        if exc.errno == errno.ELOOP or inbox_dir.is_symlink():
            raise WikiError(f'inbox directory {inbox_dir} must not be a symlink') from None
        if exc.errno == errno.ENOTDIR:
            raise WikiError(f'inbox directory {inbox_dir} is not a directory') from None
        raise WikiError(f'cannot access inbox directory {inbox_dir}: {exc}') from None

    try:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        try:
            fd = os.open(ident, flags, dir_fd=dfd)
        except FileNotFoundError:
            raise WikiError(f'inbox item {ident} not found') from None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise WikiError(f'inbox item {ident} is not a regular file') from None
            raise WikiError(f'cannot open inbox item {ident}: {exc}') from None

        try:
            try:
                st = os.fstat(fd)
            except OSError as exc:
                raise WikiError(f'cannot inspect inbox item {ident}: {exc}') from None
            if not stat.S_ISREG(st.st_mode):
                raise WikiError(f'inbox item {ident} is not a regular file')

            chunks = []
            total = 0
            while total < BUFFER_LIMIT:
                try:
                    chunk = os.read(fd, BUFFER_LIMIT - total)
                except OSError as exc:
                    raise WikiError(f'cannot read inbox item {ident}: {exc}') from None
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)

    raw = b''.join(chunks)
    if len(raw) <= TRUNCATION_LIMIT:
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise WikiError(f'inbox item {ident} is not valid UTF-8: {exc}') from None

    clipped = raw[:TRUNCATION_LIMIT]
    try:
        text = clipped.decode('utf-8')
    except UnicodeDecodeError as exc:
        if exc.reason in ('unexpected end of data', 'incomplete input'):
            try:
                text = clipped[:exc.start].decode('utf-8')
            except UnicodeDecodeError as inner_exc:
                raise WikiError(f'inbox item {ident} is not valid UTF-8: {inner_exc}') from None
        else:
            raise WikiError(f'inbox item {ident} is not valid UTF-8: {exc}') from None

    return text + TRUNCATION_MARKER


def read(state_dir: Path | str, ident: str | None = None) -> int:
    """Print listing or item content from the global inbox to stdout."""
    if ident is None:
        items = list_items(state_dir)
        if items:
            sys.stdout.write('\n'.join(items) + '\n')
    else:
        text = read_item(state_dir, ident)
        sys.stdout.write(text)
    return 0
