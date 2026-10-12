"""Independent cleanup failures must not strand other owned sync resources."""
import os
from pathlib import Path
import unittest
from unittest import mock

from pisar import config, sync
from .sync_faults import RemoteFixture, Faults, run_interrupted
from .support import git, commit_all, document
from .test_sync_index import git_add


class CleanupRunnerTests(unittest.TestCase):
    def test_cleanup_all_runs_every_action_and_raises_the_first_error(self):
        calls = []
        first, second = PermissionError('first'), OSError('second')

        def action(name, error=None):
            calls.append(name)
            if error is not None:
                raise error
        with self.assertRaises(PermissionError) as caught:
            sync._cleanup_all(lambda: action('close', first), lambda: action('scratch', second),
                              lambda: action('lock'))
        self.assertIs(caught.exception, first)
        self.assertEqual(calls, ['close', 'scratch', 'lock'])
        self.assertIsNone(sync._cleanup_all())
        calls.clear()
        self.assertIsNone(sync._cleanup_all(lambda: action('one'), lambda: action('two')))
        self.assertEqual(calls, ['one', 'two'])

    def test_cleanup_all_continues_after_baseexception(self):
        for error in (KeyboardInterrupt(), SystemExit(19)):
            with self.subTest(error=type(error).__name__):
                calls = []

                def fail():
                    calls.append('close')
                    raise error
                with self.assertRaises(type(error)) as caught:
                    sync._cleanup_all(fail, lambda: calls.append('lock'))
                self.assertIs(caught.exception, error)
                self.assertEqual(calls, ['close', 'lock'])


class IndexCleanupTests(RemoteFixture):
    def setUp(self):
        super().setUp()
        self.new_world('cleanup-index')
        (self.root / 'a.txt').write_text('old\n')
        (self.root / 'b.txt').write_text('other\n')
        commit_all(self.root)
        git(self.root, 'push', '-q', 'origin', self.branch)
        git(self.other, 'pull', '-q', 'origin', self.branch)
        self.publish(lambda repo: (repo / 'a.txt').write_text('remote\n'))
        self.start()
        self.index = Path(git(self.root, 'rev-parse', '--path-format=absolute', '--git-path', 'index').strip())
        self.lock = self.index.with_name('index.lock')
        self.runtime = sync.Runtime(self.state, self.root)
        self.ident = 'sync-cleanup-test'
        self.old = sync._index_entry(self.root, 'a.txt')
        self.new = ('100644', sync.hash_blob(self.root, b'remote\n'))

    def publish_entry(self):
        sync.publish_entry(self.root, self.runtime, self.ident, 'a.txt', self.old, self.new)

    def test_scratch_permission_failure_releases_owned_lock_and_retry_finishes(self):
        before = self.index.read_bytes()
        directories = []
        real_unlink = Path.unlink

        def interrupt(point):
            if point == 'index-compared':
                journal = sync.open_journal(self.runtime, '.')
                directory = sync.work_dir(self.runtime, journal['operation_id']).parent
                directories.append(directory)
                directory.chmod(0o500)
                raise RuntimeError('body interrupted')

        def unlink(path, *args, **kwargs):
            if path.name == 'index.private' and path.parent.stat().st_mode & 0o777 == 0o500:
                raise PermissionError('scratch denied')
            return real_unlink(path, *args, **kwargs)
        try:
            with mock.patch('pisar.sync.checkpoint', side_effect=interrupt), mock.patch.object(Path, 'unlink', unlink):
                with self.assertRaises(PermissionError):
                    sync.do_pull(self.root, self.state)
        finally:
            for directory in directories:
                directory.chmod(0o700)
        self.assertEqual(self.index.read_bytes(), before)
        self.assertFalse(self.lock.exists())
        self.assertEqual(git_add(self.root, 'b.txt').returncode, 0)
        self.assertIsNotNone(sync.open_journal(self.runtime, '.'))
        self.assertEqual(sync.do_pull(self.root, self.state)['status'], 'fast-forward')
        self.assertEqual(sync.git_bytes(self.root, 'show', ':a.txt'), b'remote\n')

    def close_fault(self, scratch_error=None):
        before = self.index.read_bytes()
        real_open, real_close, real_unlink = os.open, os.close, Path.unlink
        owned, closed, attempted = [], [], []
        error = OSError('close failed')
        scratch = sync.work_dir(self.runtime, self.ident).parent / 'index.private'

        def acquire(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if Path(path) == self.lock:
                owned.append(fd)
            return fd

        def close(fd):
            if fd in owned and fd not in closed:
                real_close(fd)
                closed.append(fd)
                raise error
            return real_close(fd)

        def unlink(path, *args, **kwargs):
            if path == scratch and path.exists() and scratch_error is not None:
                attempted.append(path)
                raise scratch_error
            return real_unlink(path, *args, **kwargs)

        def interrupt(point):
            if point == 'index-compared':
                raise RuntimeError('body interrupted')
        try:
            with mock.patch('pisar.sync.os.open', side_effect=acquire), \
                    mock.patch('pisar.sync.os.close', side_effect=close), \
                    mock.patch('pisar.sync.checkpoint', side_effect=interrupt), \
                    mock.patch.object(Path, 'unlink', unlink):
                with self.assertRaises(OSError) as caught:
                    self.publish_entry()
            self.assertIs(caught.exception, error)
            self.assertEqual(self.index.read_bytes(), before)
            if scratch_error is None:
                self.assertFalse(scratch.exists())
            else:
                self.assertEqual(attempted, [scratch])
            self.assertFalse(self.lock.exists())
            self.assertEqual(git_add(self.root, 'b.txt').returncode, 0)
        finally:
            real_unlink(scratch, missing_ok=True)

    def test_close_failure_does_not_skip_scratch_or_lock_cleanup(self):
        self.close_fault()

    def test_first_cleanup_error_wins_even_when_scratch_cleanup_also_fails(self):
        self.close_fault(PermissionError('scratch failed'))

    def test_scratch_setup_failure_never_leaves_owned_lock(self):
        before = self.index.read_bytes()
        error = PermissionError('setup failed')
        real_scratch, real_open, real_close = sync.scratch_path, os.open, os.close
        owned, closed = [], []

        def scratch(runtime, ident, name):
            if name == 'index.private':
                raise error
            return real_scratch(runtime, ident, name)

        def acquire(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if Path(path) == self.lock:
                owned.append(fd)
            return fd

        def close(fd):
            if fd in owned:
                closed.append(fd)
            return real_close(fd)
        try:
            with mock.patch('pisar.sync.scratch_path', side_effect=scratch), \
                    mock.patch('pisar.sync.os.open', side_effect=acquire), \
                    mock.patch('pisar.sync.os.close', side_effect=close):
                with self.assertRaises(PermissionError) as caught:
                    self.publish_entry()
            self.assertIs(caught.exception, error)
            self.assertFalse(self.lock.exists())
            self.assertEqual(self.index.read_bytes(), before)
            self.assertEqual(git_add(self.root, 'b.txt').returncode, 0)
            self.assertEqual(closed, owned)
        finally:
            for fd in owned:
                if fd not in closed:
                    real_close(fd)


class RecoveryCleanupTests(RemoteFixture):
    def setUp(self):
        super().setUp()
        for patch in (
                mock.patch('pisar.settings.commit_message', return_value={**config.COMMIT_MESSAGE_DEFAULTS,
                                                                         'enabled': False}),
                mock.patch('pisar.commit_message.current', return_value={
                    'what': 'Merge fixture changes', 'why': 'Keep both sides', 'context': None})):
            patch.start()
            self.addCleanup(patch.stop)

    def make_conflict(self):
        self.publish(lambda repo: (repo / 'work/team/alpha/call.md').write_text(
            document(related=('work/beta',), body='Remote wording.')))
        (self.root / 'work/team/alpha/call.md').write_text(
            document(related=('work/beta',), body='Local wording.'))
        commit_all(self.root)
        self.start()
        result = sync.do_pull(self.root, self.state, merge=True)
        self.assertEqual(result['status'], 'conflicts')
        runtime = sync.Runtime(self.state, self.root)
        ident = result['sync_id']
        return runtime, ident, sync.load(runtime, ident), Path(result['work'])

    def terminal_conflict(self, status='complete'):
        runtime, ident, journal, work = self.make_conflict()
        journal.update(status=status, phase=status)
        sync.store(runtime, journal)
        return runtime, ident, journal, work

    def assert_main_only(self):
        self.assertEqual(git(self.root, 'worktree', 'list', '--porcelain').count('worktree '), 1)

    def test_worktree_remove_exception_still_removes_files_and_prunes(self):
        runtime, ident, journal, work = self.make_conflict()
        before = self.snapshot()
        real_git, prunes = sync.git, []
        error = OSError('remove failed')

        def command(repo, *args, **kwargs):
            if args[:2] == ('worktree', 'remove'):
                raise error
            if args[:2] == ('worktree', 'prune'):
                prunes.append(repo)
            return real_git(repo, *args, **kwargs)
        with mock.patch('pisar.sync.git', side_effect=command):
            with self.assertRaises(OSError) as caught:
                sync.discard_worktree(self.root, work)
        self.assertIs(caught.exception, error)
        self.assertFalse(work.exists())
        self.assertEqual(prunes, [self.root])
        self.assert_main_only()
        self.assertEqual(self.snapshot(), before)

    def test_worktree_rmtree_failure_still_attempts_prune_and_is_reported(self):
        runtime, ident, journal, work = self.make_conflict()
        real_git, real_rmtree, prunes = sync.git, sync.shutil.rmtree, []
        error = PermissionError('rmtree failed')

        def command(repo, *args, **kwargs):
            if args[:2] == ('worktree', 'remove'):
                return ''
            if args[:2] == ('worktree', 'prune'):
                prunes.append(repo)
            return real_git(repo, *args, **kwargs)

        def remove(path, *args, **kwargs):
            if Path(path) == work:
                raise error
            return real_rmtree(path, *args, **kwargs)
        with mock.patch('pisar.sync.git', side_effect=command), \
                mock.patch('pisar.sync.shutil.rmtree', side_effect=remove):
            with self.assertRaises(PermissionError) as caught:
                sync.discard_worktree(self.root, work)
        try:
            self.assertIs(caught.exception, error)
            self.assertEqual(prunes, [self.root])
            self.assertTrue(work.exists())
        finally:
            sync.discard_worktree(self.root, work)

    def test_discard_failure_does_not_skip_operation_scratch_cleanup(self):
        runtime, ident, journal, work = self.terminal_conflict()
        before = self.snapshot()
        error = OSError('discard failed')
        with mock.patch('pisar.sync.discard_worktree', side_effect=error), \
                mock.patch('pisar.sync.remove_scratch', wraps=sync.remove_scratch) as remove:
            with self.assertRaises(OSError) as caught:
                sync.discard_worktree_of(runtime, self.root, journal)
        self.assertIs(caught.exception, error)
        remove.assert_called_once_with(runtime, ident)
        self.assertFalse(work.parent.exists())
        self.assertTrue(sync.load(runtime, ident)['worktree'])
        sync.finish_leftovers(runtime, self.root, '.')
        self.assertFalse(sync.load(runtime, ident)['worktree'])
        self.assert_main_only()
        self.assertEqual(self.snapshot(), before)

    def test_worktree_flag_store_failure_does_not_skip_scratch_cleanup(self):
        runtime, ident, journal, work = self.terminal_conflict()
        sentinel = work.parent / 'cleanup-sentinel'
        sentinel.write_text('temporary')
        real_store = sync.store
        error = OSError('store failed')

        def store(rt, value):
            if value['operation_id'] == ident and value.get('worktree') is False:
                raise error
            return real_store(rt, value)
        with mock.patch('pisar.sync.store', side_effect=store), \
                mock.patch('pisar.sync.remove_scratch', wraps=sync.remove_scratch) as remove:
            with self.assertRaises(OSError) as caught:
                sync.discard_worktree_of(runtime, self.root, journal)
        self.assertIs(caught.exception, error)
        remove.assert_called_once_with(runtime, ident)
        self.assertFalse(sentinel.exists())
        self.assertTrue(sync.load(runtime, ident)['worktree'])
        sync.finish_leftovers(runtime, self.root, '.')
        self.assertFalse(sync.load(runtime, ident)['worktree'])
        self.assert_main_only()

    def test_leftovers_attempt_every_terminal_journal_after_an_error(self):
        runtime = sync.Runtime(self.state, self.root)
        directories = {}
        for ident, status in (('sync-a', 'complete'), ('sync-b', 'aborted'), ('sync-open', 'open')):
            sync.store(runtime, dict(format=sync.FORMAT, operation_id=ident, repo='.', status=status,
                                     phase=status, worktree=False))
            sentinel = sync.scratch_path(runtime, ident, 'sentinel')
            sentinel.write_text('temporary')
            directories[ident] = sentinel.parent
        real_remove = sync.remove_scratch
        error = OSError('first journal failed')

        def remove(rt, ident):
            if ident == 'sync-a':
                raise error
            return real_remove(rt, ident)
        with mock.patch('pisar.sync.remove_scratch', side_effect=remove):
            with self.assertRaises(OSError) as caught:
                sync.finish_leftovers(runtime, self.root, '.')
        self.assertIs(caught.exception, error)
        self.assertFalse(directories['sync-b'].exists())
        self.assertTrue((directories['sync-open'] / 'sentinel').exists())
        sync.finish_leftovers(runtime, self.root, '.')
        self.assertFalse(directories['sync-a'].exists())
        self.assertFalse(directories['sync-b'].exists())
        self.assertTrue((directories['sync-open'] / 'sentinel').exists())

    def test_aborted_leftovers_retry_worktree_administration(self):
        runtime, ident, journal, work = self.terminal_conflict('aborted')
        before = self.snapshot()
        sync.finish_leftovers(runtime, self.root, '.')
        self.assertFalse(sync.load(runtime, ident)['worktree'])
        self.assertFalse(work.parent.exists())
        self.assert_main_only()
        self.assertEqual(self.snapshot(), before)

    def test_abort_records_terminal_intent_before_cleanup_error(self):
        for fault in ('discard', 'scratch'):
            with self.subTest(fault=fault):
                self.new_world(f'abort-{fault}')
                runtime, ident, journal, work = self.make_conflict()
                before = self.snapshot()
                events = []
                real_store, real_discard, real_remove = sync.store, sync.discard_worktree, sync.remove_scratch
                error = OSError(f'{fault} failed')

                def store(rt, value):
                    events.append(('store', value['status']))
                    return real_store(rt, value)

                def discard(repo, path):
                    events.append(('cleanup', 'discard'))
                    if fault == 'discard':
                        raise error
                    return real_discard(repo, path)

                def remove(rt, op):
                    events.append(('cleanup', 'scratch'))
                    if fault == 'scratch':
                        raise error
                    return real_remove(rt, op)
                with mock.patch('pisar.sync.store', side_effect=store), \
                        mock.patch('pisar.sync.discard_worktree', side_effect=discard), \
                        mock.patch('pisar.sync.remove_scratch', side_effect=remove):
                    with self.assertRaises(OSError) as caught:
                        sync.do_abort(self.root, self.state, ident)
                self.assertIs(caught.exception, error)
                self.assertEqual(sync.load(runtime, ident)['status'], 'aborted')
                self.assertEqual(events[0], ('store', 'aborted'))
                self.assertIn(('cleanup', 'discard'), events)
                self.assertIn(('cleanup', 'scratch'), events)
                self.assertIsNone(sync.open_journal(runtime, '.'))
                self.assertEqual(self.snapshot(), before)
                sync.finish_leftovers(runtime, self.root, '.')
                self.assertFalse(work.parent.exists())
                self.assertFalse(sync.load(runtime, ident)['worktree'])
                self.assert_main_only()

    def test_resolve_cleanup_error_keeps_completed_merge_and_cleans_scratch(self):
        runtime, ident, journal, work = self.make_conflict()
        resolved = document(related=('work/beta',), body='Both kept.')
        (work / 'work/team/alpha/call.md').write_text(resolved)
        real_store = sync.store
        error = OSError('terminal store failed')

        def store(rt, value):
            if value['status'] == 'complete' and value.get('worktree') is False:
                raise error
            return real_store(rt, value)
        with mock.patch('pisar.sync.store', side_effect=store), \
                mock.patch('pisar.sync.remove_scratch', wraps=sync.remove_scratch) as remove:
            with self.assertRaises(OSError) as caught:
                sync.do_resolve(self.root, self.state, ident)
        self.assertIs(caught.exception, error)
        self.assertEqual(remove.call_count, 1)
        actual_runtime, actual_ident = remove.call_args.args
        self.assertEqual(actual_ident, ident)
        self.assertEqual(actual_runtime.root, runtime.root)
        self.assertEqual(actual_runtime.path, runtime.path)
        self.assertFalse(work.parent.exists())
        persisted = sync.load(runtime, ident)
        self.assertEqual(persisted['status'], 'complete')
        self.assertTrue(persisted['worktree'])
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(len(git(self.root, 'rev-list', '--parents', '-n', '1', 'HEAD').split()), 3)
        self.assertEqual((self.root / 'work/team/alpha/call.md').read_text(), resolved)
        head = git(self.root, 'rev-parse', 'HEAD')
        count = git(self.root, 'rev-list', '--count', '--merges', 'HEAD')
        self.assertEqual(sync.do_pull(self.root, self.state)['status'], 'up-to-date')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.root, 'rev-list', '--count', '--merges', 'HEAD'), count)
        self.assertFalse(sync.load(runtime, ident)['worktree'])
        self.assert_main_only()
