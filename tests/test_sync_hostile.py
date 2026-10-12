"""Hostile remote content: names, links and malformed trees from a remote are refused before anything is written, and
nothing lands outside the repository or the sync's own work tree. The trees are built by tests/sync_forge.py in the
remote itself (objects only, so the remote can be published exactly as a hostile remote would)."""
import json
import subprocess

from . import sync_forge
from .support import CHECKOUT, Fixture, document, git, pisar_command


class HostileRemoteTests(Fixture):
    def setUp(self):
        super().setUp()
        self.remote = self.base / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(self.remote)], check=True)
        self.branch = git(self.root, 'symbolic-ref', '--short', 'HEAD')
        git(self.root, 'remote', 'add', 'origin', str(self.remote))
        git(self.root, 'push', '-q', 'origin', self.branch)
        self.base_head = git(self.root, 'rev-parse', 'HEAD')

    def publish(self, commit_id):
        sync_forge.set_branch(self.remote, self.branch, commit_id)

    def start_from_base(self, local=None):
        """The remote and the local root both at the base commit; LOCAL edits the root (uncommitted changes, if any,
        are discarded by the caller)."""
        sync_forge.set_branch(self.remote, self.branch, self.base_head)
        git(self.root, 'reset', '-q', '--hard', self.base_head)
        if local is not None:
            local()
            git(self.root, 'add', '--all')
            git(self.root, 'commit', '-q', '-m', 'local change')

    def snapshot(self):
        return dict(head=git(self.root, 'rev-parse', 'HEAD'), status=git(self.root, 'status', '--porcelain'),
                    refs=git(self.root, 'for-each-ref', '--format=%(refname) %(objectname)'),
                    files={p.relative_to(self.root).as_posix(): p.read_bytes()
                           for p in self.root.rglob('*') if p.is_file() and '.git' not in p.relative_to(self.root).parts})

    def refused(self, *args):
        """Refused either by fetch (Git's own object check: malformed trees and `.git` names) or by the pull (the
        pisar checks). Either way nothing in the repository, the working tree or the refs changes."""
        before = self.snapshot()
        env = {**self.env, 'PYTHONPATH': str(CHECKOUT)}
        fetched = subprocess.run([*pisar_command(), '--root', str(self.root), '--state-dir', str(self.state),
                                  'sync', 'fetch'], capture_output=True, text=True, env=env, cwd=self.base)
        if fetched.returncode:
            self.assertTrue(fetched.stderr.startswith('pisar: '), fetched.stderr)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(list(self.base.rglob('escaped.txt')), [])
            return None
        before = self.snapshot()
        result = json.loads(self.cli('sync', 'pull', *args, ok=False).stdout)
        self.assertEqual(result['status'], 'invalid', result)
        self.assertTrue(result['problems'], result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.base.rglob('escaped.txt')), [])  # Nothing was written anywhere under the base.
        self.assertEqual(len(git(self.root, 'worktree', 'list').splitlines()), 1)
        return result

    HOSTILE = [
        ('dotdot-nested', {'x/../../escaped.txt': b'escape\n'}),
        ('dotgit-nested', {'x/.git/config': b'[core]\n'}),
        ('dotgit-upper', {'x/.GIT/config': b'[core]\n'}),
        ('backslash', {'x\\y.md': b'text\n'}),
    ]

    def test_hostile_names_in_a_fast_forward_are_refused_and_nothing_is_written(self):
        for label, files in self.HOSTILE:
            with self.subTest(label=label):
                self.start_from_base()
                self.publish(sync_forge.commit(self.remote, self.base_head, files=files))
                self.refused()

    def test_hostile_names_in_a_conflicted_merge_are_refused_and_nothing_is_written_outside_the_work_tree(self):
        for label, files in self.HOSTILE:
            with self.subTest(label=label):
                self.start_from_base(local=lambda: (self.root / 'work/team/alpha/call.md').write_text(
                    document(body='Local.')))
                remote_files = {**files, 'work/team/alpha/call.md': document(body='Remote.').encode()}
                self.publish(sync_forge.commit(self.remote, self.base_head, files=remote_files))
                self.refused('--merge')

    def test_a_symbolic_link_from_the_remote_is_refused_in_a_fast_forward_and_in_a_conflicted_merge(self):
        link = {'work/team/alpha/link.md': '/etc/passwd'}
        self.start_from_base()
        self.publish(sync_forge.commit(self.remote, self.base_head, links=link))
        self.refused()
        self.start_from_base(local=lambda: (self.root / 'work/team/alpha/call.md').write_text(document(body='L.')))
        self.publish(sync_forge.commit(self.remote, self.base_head, links=link,
                                       files={'work/team/alpha/call.md': document(body='R.').encode()}))
        self.refused('--merge')

    def test_a_malformed_tree_is_refused_by_fetch_and_the_private_ref_is_not_written(self):
        self.start_from_base()
        blob = sync_forge._blob(self.remote, b'dup\n')
        raw = b'100644 dup.md\0' + bytes.fromhex(blob) + b'100644 dup.md\0' + bytes.fromhex(blob)
        tree = subprocess.run(['git', '-C', str(self.remote), 'hash-object', '-t', 'tree', '-w', '--literally',
                               '--stdin'], input=raw, capture_output=True, check=True).stdout.decode().strip()
        self.publish(sync_forge._git(self.remote, 'commit-tree', tree, '-p', self.base_head, '-m', 'duplicates')
                     .decode().strip())
        p = self.cli('sync', 'fetch', ok=False)
        self.assertTrue(p.stderr.startswith('pisar: '), p.stderr)
        self.assertEqual(git(self.root, 'for-each-ref', 'refs/pisar'), '')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), self.base_head)
