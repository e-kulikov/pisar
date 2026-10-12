"""The real index is published only under Git's own index lock, and only after its current entry is compared with the
journaled state while the lock is held (review 20c). An ordinary Git writer is either seen by that comparison (it
finished first) or sees the sync's lock and fails cleanly (it starts later). A foreign lock is never removed."""
import os
import stat
import subprocess
import time
from pathlib import Path

from pisar import sync
from .sync_faults import RemoteFixture
from .support import commit_all, document, git


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

    def test_an_abrupt_exit_after_taking_the_lock_leaves_it_and_the_retry_refuses_without_removing_it(self):
        # A killed process runs no cleanup: simulated with os._exit in a forked child right after the lock is taken.
        pid = os.fork()
        if pid == 0:
            try:
                from unittest import mock

                def die(point):
                    if point == 'index-locked':
                        os._exit(17)
                with mock.patch('pisar.sync.checkpoint', side_effect=die):
                    sync.do_pull(self.root, self.state)
            finally:
                os._exit(99)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 17)
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


class LockLifecycleTests(RemoteFixture):
    """Every exit path after the lock is created releases it, except an abrupt exit (review 20d, 1)."""

    def setUp(self):
        super().setUp()
        self.new_world('lifecycle')
        (self.root / 'a.txt').write_text('old\n')
        (self.root / 'b.txt').write_text('other\n')
        commit_all(self.root)
        git(self.root, 'push', '-q', 'origin', self.branch)
        git(self.other, 'pull', '-q', 'origin', self.branch)
        self.publish(lambda repo: (repo / 'a.txt').write_text('remote\n'))
        self.start()
        self.index = Path(git(self.root, 'rev-parse', '--path-format=absolute', '--git-path', 'index').strip())
        self.lock = self.index.with_name('index.lock')

    def assert_released(self, repo_index_lock):
        self.assertFalse(repo_index_lock.exists(), 'an index.lock was left behind')

    def test_a_writer_that_finished_with_the_target_bytes_leaves_no_lock_and_git_keeps_working(self):
        from unittest import mock

        def git_add_remote_bytes(point):
            if point == 'file-written':
                self.assertEqual(git_add(self.root, 'a.txt').returncode, 0)
        with mock.patch('pisar.sync.checkpoint', side_effect=git_add_remote_bytes):
            self.assertEqual(sync.do_pull(self.root, self.state)['status'], 'fast-forward')
        self.assert_released(self.lock)
        self.assertEqual(git_add(self.root, 'b.txt').returncode, 0)

    def test_a_child_pull_interrupted_at_pinned_and_retried_leaves_no_lock_in_the_parent(self):
        from .sync_faults import Faults, run_interrupted
        child = self.add_submodule()
        origin = self.base / 'module-origin'
        (origin / 'child-new.md').write_text(document('child-new', 'work/module', body='Child.'))
        commit_all(origin)
        sync.do_fetch(self.root, self.state, relative='work/module')
        with Faults('pinned'):
            self.assertTrue(run_interrupted(sync.do_pull, self.root, self.state, relative='work/module'))
        self.assertEqual(sync.do_pull(self.root, self.state, relative='work/module')['status'], 'fast-forward')
        parent_lock = Path(git(self.root, 'rev-parse', '--path-format=absolute', '--git-path', 'index.lock').strip())
        self.assert_released(parent_lock)
        self.assertEqual(git_add(self.root, 'b.txt').returncode, 0)
        del child

    def test_a_keyboard_interrupt_at_index_locked_or_index_compared_leaves_no_lock(self):
        from unittest import mock
        for point in ('index-locked', 'index-compared'):
            with self.subTest(point=point):
                self.new_world(f'kbd-{point}')
                (self.root / 'a.txt').write_text('old\n')
                (self.root / 'b.txt').write_text('other\n')
                commit_all(self.root)
                git(self.root, 'push', '-q', 'origin', self.branch)
                git(self.other, 'pull', '-q', 'origin', self.branch)
                self.publish(lambda repo: (repo / 'a.txt').write_text('remote\n'))
                self.start()

                def interrupt(name):
                    if name == point:
                        raise KeyboardInterrupt()
                with mock.patch('pisar.sync.checkpoint', side_effect=interrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        sync.do_pull(self.root, self.state)
                self.assert_released(self.lock)
                self.assertEqual(git_add(self.root, 'b.txt').returncode, 0)
                self.assertEqual(sync.do_pull(self.root, self.state)['status'], 'fast-forward')

    def test_a_lock_created_by_someone_else_is_refused_and_never_removed_on_any_path(self):
        self.lock.write_text('FOREIGN LOCK\n')
        with self.assertRaisesRegex(Exception, 'index.lock'):
            sync.do_pull(self.root, self.state)
        self.assertEqual(self.lock.read_text(), 'FOREIGN LOCK\n')
        self.lock.unlink()


class RacyTimestampTests(RemoteFixture):
    """The published index keeps Git's racy-file information (review 20d, 2): an unrelated same-size edit with the
    index's own second is still seen by status, diff and staging after a sync."""

    def setUp(self):
        super().setUp()
        self.new_world('racy')
        (self.root / 'a.txt').write_text('old\n')
        (self.root / 'b.txt').write_text('other\n')
        commit_all(self.root)
        git(self.root, 'push', '-q', 'origin', self.branch)
        git(self.other, 'pull', '-q', 'origin', self.branch)
        self.publish(lambda repo: (repo / 'a.txt').write_text('remote\n'))
        git(self.root, 'config', 'core.trustctime', 'false')
        git(self.root, 'config', 'core.checkStat', 'minimal')
        self.start()

    def test_a_same_size_unrelated_edit_in_the_index_second_is_still_reported_after_the_sync(self):
        from unittest import mock

        def edit_b(point):
            if point != 'file-written' or getattr(edit_b, 'done', False):
                return
            edit_b.done = True
            b = self.root / 'b.txt'
            stamp = int(time.time())
            os.utime(b, (stamp, stamp))
            git(self.root, 'add', 'b.txt')
            b.write_text('USER!\n')
            os.utime(b, (stamp, stamp))
            time.sleep(1.1)
        with mock.patch('pisar.sync.checkpoint', side_effect=edit_b):
            self.assertEqual(sync.do_pull(self.root, self.state)['status'], 'fast-forward')
        self.assertIn('b.txt', git(self.root, 'diff', '--name-only').split())
        self.assertIn('b.txt', git(self.root, 'status', '--porcelain'))
        git(self.root, 'add', '-u', '--', 'b.txt')
        self.assertEqual(subprocess.run(['git', '-C', str(self.root), 'show', ':b.txt'], capture_output=True,
                                        check=True).stdout, b'USER!\n')

    def test_an_ordinary_sync_leaves_a_clean_status(self):
        self.assertEqual(sync.do_pull(self.root, self.state)['status'], 'fast-forward')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(git(self.root, 'diff', '--name-only'), '')


class SharedPermissionTests(RemoteFixture):
    """The published index keeps its access mode, honouring core.sharedRepository (review 20d, 3)."""

    def setUp(self):
        super().setUp()
        self.new_world('shared')
        (self.root / 'a.txt').write_text('old\n')
        commit_all(self.root)
        git(self.root, 'push', '-q', 'origin', self.branch)
        git(self.other, 'pull', '-q', 'origin', self.branch)
        self.publish(lambda repo: (repo / 'a.txt').write_text('remote\n'))
        self.index = Path(git(self.root, 'rev-parse', '--path-format=absolute', '--git-path', 'index').strip())
        git(self.root, 'config', 'core.sharedRepository', 'group')
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)

    def test_the_index_mode_survives_a_sync_under_a_restrictive_umask(self):
        git(self.root, 'add', 'a.txt')
        git(self.root, 'reset', '-q', '--', 'a.txt')
        before = stat.S_IMODE(self.index.stat().st_mode)
        self.start()
        self.assertEqual(sync.do_pull(self.root, self.state)['status'], 'fast-forward')
        self.assertEqual(stat.S_IMODE(self.index.stat().st_mode), before)
        self.assertEqual(before & 0o070, 0o070 & before, 'group access expected')
