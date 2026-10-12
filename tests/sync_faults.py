"""Shared helpers of the sync regression tests: a remote fixture and in-process fault injection.

A fault is raised by `pisar.sync.checkpoint` when the named point is reached (optionally only the Nth time), so a
test can stop a run at any point, look at the repository, and run the same command again."""
import json
import os
import shutil
import subprocess
from unittest import mock

from pisar import sync
from .support import CHECKOUT, Fixture, clean_environ, commit_all, git, pisar_command


class Interrupted(Exception):
    pass


class Faults:
    """Context manager: raise Interrupted at checkpoint NAME, the NTH time it is reached."""

    def __init__(self, name=None, nth=1):
        self.name, self.nth, self.seen = name, nth, 0
        self.patch = None

    def _stop(self, point):
        if point == self.name:
            self.seen += 1
            if self.seen == self.nth:
                raise Interrupted(point)

    def __enter__(self):
        self.patch = mock.patch('pisar.sync.checkpoint', side_effect=self._stop)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()
        return False


def run_interrupted(function, *args, **kwargs):
    """Call FUNCTION; return True when it was interrupted at a fault."""
    try:
        function(*args, **kwargs)
    except Interrupted:
        return True
    return False


class RemoteFixture(Fixture):
    """A knowledge repository whose remote is a local bare repository, and a second clone that publishes.

    `new_world` starts again from the pristine repository: a new root, remote and publisher, so each scenario of a
    test is independent and nothing has to be undone."""

    def setUp(self):
        super().setUp()
        self.env_patch = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.base / 'noconfig')})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.pristine = self.base / 'pristine'
        shutil.copytree(self.root, self.pristine, symlinks=True)
        self.new_world('first')

    def new_world(self, name):
        self.root = self.base / f'root-{name}'
        shutil.copytree(self.pristine, self.root, symlinks=True)
        self.state = self.base / f'state-{name}'
        self.remote = self.base / f'remote-{name}.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(self.remote)], check=True)
        self.branch = git(self.root, 'symbolic-ref', '--short', 'HEAD')
        git(self.remote, 'symbolic-ref', 'HEAD', f'refs/heads/{self.branch}')
        git(self.root, 'remote', 'add', 'origin', str(self.remote))
        git(self.root, 'push', '-q', 'origin', self.branch)
        self.other = self.base / f'other-{name}'
        subprocess.run(['git', 'clone', '-q', '-b', self.branch, str(self.remote), str(self.other)], check=True,
                       env=clean_environ())
        git(self.other, 'config', 'user.email', 'synthetic@example.invalid')
        git(self.other, 'config', 'user.name', 'Synthetic fixture')
        return self.root

    def publish(self, change, message='remote change'):
        """Apply CHANGE(path) in the other clone, commit it and push: the remote moves."""
        git(self.other, 'pull', '-q', 'origin', self.branch)
        change(self.other)
        commit_all(self.other)
        git(self.other, 'commit', '-q', '--amend', '-m', message)
        git(self.other, 'push', '-q', '-f', 'origin', self.branch)
        return git(self.other, 'rev-parse', 'HEAD')

    def start(self, merge=False, relative=None):
        """A fetch, as the user would run it before a pull."""
        sync.do_fetch(self.root, self.state, relative)

    def snapshot(self, status=True):
        """Everything a refused or dry-run command must leave byte-identical: refs, HEAD, index, working files.

        STATUS=False leaves out `git status`, for repositories whose own attributes must not run in a test."""
        files = {}
        for path in self.root.rglob('*'):
            relative = path.relative_to(self.root)
            if path.is_file() and '.git' not in relative.parts:
                files[relative.as_posix()] = path.read_bytes()
        return dict(head=git(self.root, 'rev-parse', 'HEAD'),
                    refs=git(self.root, 'for-each-ref', '--format=%(refname) %(objectname)'),
                    index=git(self.root, 'ls-files', '--stage', '-z'),
                    status=git(self.root, 'status', '--porcelain=v1', '--untracked-files=all') if status else '',
                    files=files)

    def journals(self):
        return {p.name: p.read_bytes() for p in sorted((self.state / 'roots').rglob('sync-*.json'))}

    def pisar_tool(self, args):
        """Run `pisar` with ARGS the way the Codex server does (same entry point, same arguments)."""
        env = {**self.env, 'PYTHONPATH': str(CHECKOUT)}
        return subprocess.run([*pisar_command(), '--root', str(self.root), '--state-dir', str(self.state), *args],
                              capture_output=True, text=True, env=env, cwd=self.base)


def pull_json(fixture, *args, ok=True):
    return json.loads(fixture.cli('sync', 'pull', *args, ok=ok).stdout)
