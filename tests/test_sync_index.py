"""The real index is published only under Git's own index lock, and only after its current entry is compared with the
journaled state while the lock is held (review 20c). An ordinary Git writer is either seen by that comparison (it
finished first) or sees the sync's lock and fails cleanly (it starts later). A foreign lock is never removed."""
import os
import subprocess
from pathlib import Path

from pisar import sync
from .sync_faults import RemoteFixture
from .support import commit_all, git


class SimulatedCrash(BaseException):
    """A process killed at a point: unlike an Exception, it does not run the cleanup of the sync."""


def git_add(repo, path):
    return subprocess.run(['git', '-C', str(repo), 'add', '--', path], capture_output=True, text=True)


class IndexRaceTests(RemoteFixture):
    def setUp(self):
        super().setUp()
        self.new_world('race')
        (self.root / 'a.txt').write_text('old\n')
        (self.root / 'b.txt').write_text('other\n')
        commit_all(self.root)
        git(self.root, 'push', '-q', 'origin', self.branch)
        git(self.other, 'pull', '-q', 'origin', self.branch)
        self.publish(lambda repo: (repo / 'a.txt').write_text('remote\n'))
        self.start()
        self.index = Path(git(self.root, 'rev-parse', '--path-format=absolute', '--git-path', 'index').strip())
        self.lock = self.index.with_name('index.lock')

    def staged(self, path):
        """The exact staged bytes of PATH (git show strips the final newline, so it is not used)."""
        return subprocess.run(['git', '-C', str(self.root), 'show', f':{path}'], capture_output=True,
                              check=True).stdout.decode()

    def test_a_concurrent_git_add_of_the_same_path_is_refused_and_kept(self):
        # Review 20c: the reviewer's reproduction. The user stages their own content after the install of the file.
        def user_stages(point):
            if point == 'file-written':
                (self.root / 'a.txt').write_text('USER STAGED\n')
                self.assertEqual(git_add(self.root, 'a.txt').returncode, 0)
                (self.root / 'a.txt').write_text('remote\n')
        from unittest import mock
        with mock.patch('pisar.sync.checkpoint', side_effect=lambda p: user_stages(p)):
            with self.assertRaisesRegex(Exception, 'a.txt'):
                sync.do_pull(self.root, self.state)
        self.assertEqual(self.staged('a.txt'), 'USER STAGED\n')
        self.assertFalse(self.lock.exists())
        self.assertIn('status": "open', (self.state / 'roots').rglob('sync-*.json').__next__().read_text())
        # The user resolves it (unstages their change): the retry completes.
        git(self.root, 'reset', '-q', '--', 'a.txt')
        self.assertEqual(sync.do_pull(self.root, self.state)['status'], 'fast-forward')
        self.assertEqual(self.staged('a.txt'), 'remote\n')

    def test_a_writer_that_finished_before_the_lock_is_detected_and_not_overwritten(self):
        # The user stages the very bytes the sync is installing: the comparison sees them and nothing is replaced.
        from unittest import mock

        def user_stages_the_same_bytes(point):
            if point == 'file-written':
                git_add(self.root, 'a.txt')
        with mock.patch('pisar.sync.checkpoint', side_effect=user_stages_the_same_bytes):
            self.assertEqual(sync.do_pull(self.root, self.state)['status'], 'fast-forward')
        self.assertEqual(self.staged('a.txt'), 'remote\n')

    def test_a_writer_racing_the_publication_fails_cleanly_while_sync_holds_the_lock(self):
        # Between the comparison and the replacement, an ordinary `git add` of another path must find the lock.
        from unittest import mock
        seen = []

        def racing_writer(point):
            if point == 'index-compared':
                result = git_add(self.root, 'b.txt')
                seen.append((result.returncode, self.lock.exists()))
        with mock.patch('pisar.sync.checkpoint', side_effect=racing_writer):
            self.assertEqual(sync.do_pull(self.root, self.state)['status'], 'fast-forward')
        self.assertEqual(seen, [(128, True)])  # refused by Git's own lock; the sync's lock was present
        self.assertFalse(self.lock.exists())

    def test_a_foreign_index_lock_is_refused_and_never_removed(self):
        self.lock.write_text('someone else\n')
        with self.assertRaisesRegex(Exception, 'index.lock'):
            sync.do_pull(self.root, self.state)
        self.assertEqual(self.lock.read_text(), 'someone else\n')
        self.assertEqual(self.staged('a.txt'), 'old\n')

    def test_a_crash_after_taking_the_lock_leaves_it_and_the_retry_refuses_without_removing_it(self):
        from unittest import mock

        def crash(point):
            if point == 'index-locked':
                raise SimulatedCrash(point)
        with mock.patch('pisar.sync.checkpoint', side_effect=crash):
            with self.assertRaises(SimulatedCrash):
                sync.do_pull(self.root, self.state)
        self.assertTrue(self.lock.exists())
        with self.assertRaisesRegex(Exception, 'index.lock'):
            sync.do_pull(self.root, self.state)
        self.assertTrue(self.lock.exists(), 'the retry removed a lock it did not create')

    def test_unrelated_staged_entries_survive_the_publication_byte_for_byte(self):
        from unittest import mock
        before = git(self.root, 'ls-files', '--stage', '-z', '--', 'b.txt')

        def user_stages_another_path(point):
            if point == 'file-written':
                (self.root / 'b.txt').write_text('user b\n')
                git_add(self.root, 'b.txt')
        with mock.patch('pisar.sync.checkpoint', side_effect=user_stages_another_path):
            self.assertEqual(sync.do_pull(self.root, self.state)['status'], 'fast-forward')
        self.assertNotEqual(git(self.root, 'ls-files', '--stage', '-z', '--', 'b.txt'), before)
        self.assertEqual(self.staged('b.txt'), 'user b\n')
        self.assertEqual(self.staged('a.txt'), 'remote\n')

    def test_additions_deletions_mode_changes_leave_stage_zero_entries_equal_to_head(self):
        def change(repo):
            (repo / 'added.txt').write_text('added\n')
            (repo / 'b.txt').unlink()
            (repo / 'a.txt').chmod(0o755)
        self.publish(change)
        self.start()
        self.assertEqual(sync.do_pull(self.root, self.state)['status'], 'fast-forward')
        listing = git(self.root, 'ls-files', '--stage', '-z').split('\0')
        self.assertTrue(all(row.split()[2] == '0' for row in listing if row), 'unmerged entry')
        self.assertEqual(git(self.root, 'diff', '--cached', '--name-only'), '')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertIn('100755 ', git(self.root, 'ls-files', '--stage', '--', 'a.txt'))
        self.assertFalse((self.root / 'b.txt').exists())
