"""Public CLI adapter; all task state remains authoritative in ruwana."""
from datetime import datetime, time
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from .safety import WikiError, safe_path, sha256


# Rust str::trim uses Unicode White_Space; Python's default strip also removes
# U+001C..U+001F, which ruwana preserves. Match the public CLI's text contract.
RUWANA_WHITESPACE = '\t\n\v\f\r \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000'


def task_content(task):
    """Trim title edges; blank descriptions are absent, other bytes stay exact."""
    title = task['title'].strip(RUWANA_WHITESPACE)
    description = task.get('description')
    if description is not None and not description.strip(RUWANA_WHITESPACE):
        description = None
    return title, description


def due_end_of_day(value):
    """Plan date in its supplied offset; ruwana stores that day at local 23:59:59."""
    day = datetime.fromisoformat(value.replace('Z', '+00:00')).date()
    return datetime.combine(day, time(23, 59, 59)).astimezone()


class Ruwana:
    def __init__(self, wiki, binary):
        self.wiki = wiki
        found = shutil.which(str(binary))
        if found is None:
            raise WikiError(f'ruwana unavailable: {binary}; operation incomplete, retry with --ruwana or PISAR_RUWANA_BIN')
        # which() keeps relative flags and relative PATH entries relative; make them
        # absolute from the invocation directory before ruwana runs inside the root.
        # abspath, not resolve: symlinked shims dispatch on their own name.
        self.binary = os.path.abspath(found)

    def run(self, *args):
        try:
            # Compatibility exception: ruwana's public interface still reads the
            # legacy WIKI_ROOT. It is set only for this child; pisar never reads it.
            p = subprocess.run([self.binary, *args], cwd=self.wiki.root,
                               env={**os.environ, 'WIKI_ROOT': str(self.wiki.root)},
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WikiError(f'ruwana failed: {error}') from None
        if p.returncode or p.stderr.strip():
            raise WikiError(f'ruwana failed/warned (exit {p.returncode}): {p.stderr.strip()}')
        return p.stdout.strip()

    def project(self, space):
        directory = safe_path(space.path, '.ruwana')
        if directory.exists():
            for entry in directory.iterdir():
                if entry.is_symlink():
                    raise WikiError('ruwana task symlink refused')
        return space.path.relative_to(self.wiki.root).as_posix()

    def find(self, space, task, reference):
        project = self.project(space)
        marker = f'{reference}#task-{task["id"]}'
        records = json.loads(self.run('list', '--project', project, '--all', '--format', 'json'))
        if not isinstance(records, list):
            raise WikiError('ruwana list returned invalid JSON shape')
        matches = [r for r in records if marker in r.get('source', [])]
        if len(matches) > 1:
            raise WikiError(f'duplicate ruwana task source identity: {marker}')
        if not matches:
            return None
        ident = matches[0].get('id')
        if not isinstance(ident, str) or not re.fullmatch('[a-z0-9]+', ident):
            raise WikiError('ruwana returned invalid task id')
        record = json.loads(self.run('show', ident, '--project', project, '--format', 'json'))
        if record.get('id') != ident or record.get('project') != project or marker not in record.get('source', []):
            raise WikiError('ruwana show identity conflict')
        title, description = task_content(task)
        if record.get('title') != title or record.get('description') != description:
            raise WikiError(f'ruwana task content conflict: {marker}')
        due = record.get('due')
        expected = task.get('due')
        if bool(due) != bool(expected):
            raise WikiError(f'ruwana task due conflict: {marker}')
        if due:
            actual = datetime.fromisoformat(due.replace('Z', '+00:00'))
            normalized = due_end_of_day(expected)
            if actual.replace(tzinfo=None) != normalized.replace(tzinfo=None) or actual.utcoffset() != normalized.utcoffset():
                raise WikiError(f'ruwana task due conflict: {marker}')
        path = safe_path(space.path, f'.ruwana/{ident}.toml')
        returned = Path(record.get('file_path', ''))
        if not returned.is_absolute():
            returned = self.wiki.root / returned
        if returned.resolve() != path or not path.is_file():
            raise WikiError('ruwana returned unexpected/missing task file')
        return dict(id=ident, space_id=space.id, project=project, source=marker,
                    path=path.relative_to(self.wiki.root).as_posix(), sha256=sha256(path.read_bytes()))

    def ensure(self, space, task, reference):
        found = self.find(space, task, reference)
        if found:
            return found
        args = ['add', f'--project={self.project(space)}',
                f'--source={reference}', f'--source={reference}#task-{task["id"]}']
        title, description = task_content(task)
        if description is not None:
            args.append('--description=' + description)
        if 'due' in task:
            args.append('--due=' + due_end_of_day(task['due']).date().isoformat())
        args.extend(['--', title])
        ident = self.run(*args)
        found = self.find(space, task, reference)
        if not found or ident != found['id']:
            raise WikiError('ruwana add did not produce a verified task; operation incomplete')
        return found
