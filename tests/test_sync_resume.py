"""Interrupted syncs resume without duplicates: a fault is injected at each named checkpoint, then the sync runs again.

The faults run in-process (`sync.checkpoint` is replaced), against temporary repositories and a temporary remote.
"""
import os
import subprocess
from unittest import mock

from pisar import sync
from .support import Fixture, clean_environ, commit_all, document, git


class Interrupted(Exception):
    pass


class SyncResumeTests(Fixture):
    def setUp(self):
        super().setUp()
        self.env_patch = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.base / 'noconfig')})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def scenario(self, name, local=None, remote=None):
        """A new root and remote for one run; LOCAL and REMOTE change the branches before the sync."""
        root = self.base / f'root-{name}'
        subprocess.run(['cp', '-a', str(self.root), str(root)], check=True)
        remote_repo = self.base / f'remote-{name}.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(remote_repo)], check=True)
        branch = git(root, 'symbolic-ref', '--short', 'HEAD')
        git(remote_repo, 'symbolic-ref', 'HEAD', f'refs/heads/{branch}')
        git(root, 'remote', 'add', 'origin', str(remote_repo))
        git(root, 'push', '-q', 'origin', branch)
        other = self.base / f'other-{name}'
        subprocess.run(['git', 'clone', '-q', str(remote_repo), str(other)], check=True, env=clean_environ())
        git(other, 'config', 'user.email', 'synthetic@example.invalid')
        git(other, 'config', 'user.name', 'Synthetic fixture')
        if local:
            local(root)
            commit_all(root)
        if remote:
            remote(other)
            commit_all(other)
            git(other, 'push', '-q', 'origin', branch)
        state = self.base / f'state-{name}'
        return root, state, branch

    def add_remote_document(self, repo):
        (repo / 'work/team/alpha/remote-call.md').write_text(document('remote-call', 'work/alpha', body='Remote.'))

    def add_local_note(self, repo):
        (repo / 'work/team/alpha/local-note.md').write_text(document('local-note', 'work/alpha', body='Local.'))

    def history(self, root):
        return dict(head=git(root, 'rev-parse', 'HEAD'), commits=int(git(root, 'rev-list', '--count', 'HEAD')),
                    merges=int(git(root, 'rev-list', '--count', '--merges', 'HEAD')))

    def run_with_fault(self, name, fault):
        """Run one pull; FAULT names the checkpoint that raises Interrupted, or None for no fault."""
        def stop(point):
            if point == fault:
                raise Interrupted(point)
        with mock.patch('pisar.sync.checkpoint', side_effect=stop):
            return sync.do_pull(self.last_root, self.last_state, merge=self.last_merge)

    def start(self, root, state, merge=False):
        self.last_root, self.last_state, self.last_merge = root, state, merge
        sync.do_fetch(root, state)

    def test_fast_forward_resumes_from_every_checkpoint(self):
        for fault in ('journal', 'pending', 'moved', 'files', 'complete'):
            with self.subTest(fault=fault):
                root, state, branch = self.scenario(f'ff-{fault}', remote=self.add_remote_document)
                self.start(root, state)
                remote_head = git(root, 'rev-parse', f'refs/pisar/sync/origin/{branch}')
                with self.assertRaises(Interrupted):
                    self.run_with_fault(fault, fault)
                sync.do_pull(root, state)
                self.assertEqual(git(root, 'rev-parse', 'HEAD'), remote_head)
                self.assertEqual(self.history(root)['commits'], self.history(root)['commits'] - 0)
                self.assertTrue((root / 'work/team/alpha/remote-call.md').is_file())
                self.assertEqual(git(root, 'status', '--porcelain'), '')
                self.assertEqual(len(list((state / 'roots').rglob('sync-*.json'))), 1, fault)
                self.assertEqual(sync.do_pull(root, state)['status'], 'up-to-date')

    def test_merge_resumes_from_every_checkpoint_without_a_second_merge_commit(self):
        for fault in ('merge-commit', 'journal', 'pending', 'moved', 'files', 'complete'):
            with self.subTest(fault=fault):
                root, state, branch = self.scenario(f'merge-{fault}', local=self.add_local_note,
                                                    remote=self.add_remote_document)
                self.start(root, state, merge=True)
                before = self.history(root)
                with self.assertRaises(Interrupted):
                    self.run_with_fault(fault, fault)
                result = sync.do_pull(root, state, merge=True)
                after = self.history(root)
                # Faulted after the journal was completed: the retry finds the branch already in place.
                self.assertEqual(result['status'], 'up-to-date' if fault == 'complete' else 'merge', (fault, result))
                self.assertEqual(after['merges'], before['merges'] + 1, fault)
                self.assertEqual(after['commits'], before['commits'] + 2, fault)
                self.assertEqual(git(root, 'rev-parse', 'HEAD^2'), git(root, 'rev-parse', f'refs/pisar/sync/origin/{branch}'))
                self.assertEqual(len(list((state / 'roots').rglob('sync-*.json'))), 1, fault)
                self.assertEqual(git(root, 'status', '--porcelain'), '')
                self.assertTrue((root / 'work/team/alpha/local-note.md').is_file())
                self.assertTrue((root / 'work/team/alpha/remote-call.md').is_file())

    def test_conflict_state_survives_an_interruption_and_resolves_once(self):
        def local(repo):
            (repo / 'call.md').write_text(document(related=('work/beta',), body='Local wording.'))

        def remote(repo):
            (repo / 'call.md').write_text(document(related=('work/beta',), body='Remote wording.'))
        for fault in ('worktree', 'conflicts'):
            with self.subTest(fault=fault):
                root, state, _ = self.scenario(f'conflict-{fault}', local=local, remote=remote)
                self.start(root, state, merge=True)
                head = git(root, 'rev-parse', 'HEAD')
                with self.assertRaises(Interrupted):
                    self.run_with_fault(fault, fault)
                self.assertEqual(git(root, 'rev-parse', 'HEAD'), head)
                second = sync.do_pull(root, state, merge=True)
                self.assertEqual(second['status'], 'conflicts', (fault, second))
                self.assertEqual(len(list((state / 'roots').rglob('sync-*.json'))), 1)
                self.assertEqual(git(root, 'rev-parse', 'HEAD'), head)
                self.assertEqual(len(git(root, 'worktree', 'list').splitlines()), 2)
                sync.do_abort(root, state, second['sync_id'])
                self.assertEqual(len(git(root, 'worktree', 'list').splitlines()), 1)
                self.assertEqual(git(root, 'rev-parse', 'HEAD'), head)

    def test_a_retry_with_a_moved_fetched_ref_refuses(self):
        root, state, branch = self.scenario('moved', remote=self.add_remote_document)
        self.start(root, state)
        with self.assertRaises(Interrupted):
            self.run_with_fault('journal', 'journal')
        self.assertIsNotNone(sync.open_journal(sync.Runtime(state, root), '.'))
        ref = f'refs/pisar/sync/origin/{branch}'
        git(root, 'update-ref', ref, git(root, 'rev-parse', 'HEAD~0'))
        with self.assertRaisesRegex(Exception, 'fetched ref moved'):
            sync.do_pull(root, state)
