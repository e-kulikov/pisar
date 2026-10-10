"""Recovery of an interrupted commit is decided by the commit ids pisar journaled, never by its text."""
import hashlib
import json
import unittest
from unittest import mock

from pisar import gitops, operations
from pisar.safety import WikiError
from pisar.spaces import Wiki
from .support import Fixture, git


class CommitRecovery(Fixture):
    def plan(self, operation):
        source = self.external()
        path = self.base / f'{operation}.json'
        path.write_text(json.dumps(dict(
            schema_version=1, operation_id=operation, space_id='work/alpha',
            source=dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
            meeting=dict(id=f'{operation}-meeting', title='Launch', body='Agreed to launch.'),
            tasks=[], pages=[])))
        return path

    def journal(self, operation):
        return operations.Runtime(self.state, self.root).load(operation)

    def interrupt(self, plan, window, edit=None):
        """Run save and interrupt it once: before the commit, after commit-tree, or after update-ref.

        EDIT replaces the message given to commit-tree, so the commit has another subject."""
        real_run, real_commit = gitops.run, gitops.commit
        armed = [True]

        def run(repo, *args, **kwargs):
            if args and args[0] == 'commit-tree' and edit is not None:
                args = (*args[:-1], edit)
            if args and args[0] == 'update-ref' and window == 'before-ref' and armed[0]:
                armed[0] = False
                raise WikiError('interrupted between commit-tree and update-ref')
            return real_run(repo, *args, **kwargs)

        def commit(repo, *args, **kwargs):
            if window == 'before-commit' and armed[0]:
                armed[0] = False
                raise WikiError('interrupted before the commit')
            result = real_commit(repo, *args, **kwargs)
            if window == 'after-ref' and result and armed[0]:
                armed[0] = False
                raise WikiError('interrupted after update-ref, before the checkpoint')
            return result
        with mock.patch.object(gitops, 'run', run), mock.patch.object(gitops, 'commit', commit):
            with self.assertRaises(WikiError):
                operations.save(Wiki(self.root), self.state, plan)
        self.assertFalse(armed[0])

    def test_crash_after_commit_tree_before_update_ref_retries_without_a_duplicate(self):
        plan = self.plan('before-ref')
        before = git(self.root, 'rev-parse', 'HEAD')
        self.interrupt(plan, 'before-ref')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), before)
        pending = self.journal('before-ref')['pending']
        self.assertEqual(pending['parent'], before)
        self.assertNotEqual(pending['commit'], before)  # the unreferenced object is harmless
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD^'), before)  # exactly one new commit
        self.assertEqual(len(self.journal('before-ref')['commits']), 1)
        self.assertIsNone(self.journal('before-ref')['pending'])
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertTrue(self.data('check')['ok'])

    def test_crash_after_update_ref_is_recognised_by_id_even_when_the_subject_differs(self):
        plan = self.plan('by-id')
        before = git(self.root, 'rev-parse', 'HEAD')
        self.interrupt(plan, 'after-ref', edit='Written by hand')
        head = git(self.root, 'rev-parse', 'HEAD')
        self.assertEqual(git(self.root, 'log', '-1', '--format=%s'), 'Written by hand')
        self.assertEqual(self.journal('by-id')['pending'], dict(
            repo=str(self.root), commit=head, parent=before))
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD^'), before)  # no second commit
        journal = self.journal('by-id')
        self.assertEqual(journal['status'], 'complete')
        self.assertEqual([c['commit'] for c in journal['commits']], [head])
        self.assertIsNone(journal['pending'])

    def test_commit_with_the_pisar_subject_that_pisar_did_not_write_is_refused(self):
        plan = self.plan('foreign')
        before = git(self.root, 'rev-parse', 'HEAD')
        self.interrupt(plan, 'before-ref')
        git(self.root, 'commit', '-q', '--allow-empty', '-m', 'wiki: foreign')
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan, ok=False)
        self.assertNotEqual(git(self.root, 'rev-parse', 'HEAD^'), before)
        self.assertEqual(self.journal('foreign')['status'], 'incomplete')

    def test_foreign_commit_with_the_pisar_subject_is_refused_without_a_pending_record(self):
        plan = self.plan('no-intent')
        before = git(self.root, 'rev-parse', 'HEAD')
        self.interrupt(plan, 'before-commit')
        self.assertIsNone(self.journal('no-intent')['pending'])
        git(self.root, 'commit', '-q', '--allow-empty', '-m', 'wiki: no-intent')
        self.assertNotEqual(git(self.root, 'rev-parse', 'HEAD^'), before)
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan, ok=False)
        self.assertEqual(self.journal('no-intent')['status'], 'incomplete')

    def test_journal_of_the_old_format_without_pending_still_resumes_by_subject(self):
        plan = self.plan('legacy')
        before = git(self.root, 'rev-parse', 'HEAD')
        self.interrupt(plan, 'after-ref')
        journal = self.journal('legacy')
        head = git(self.root, 'rev-parse', 'HEAD')
        self.assertEqual(git(self.root, 'log', '-1', '--format=%s'), 'wiki: legacy')
        del journal['pending']  # written by pisar before the commit record existed
        operations.Runtime(self.state, self.root).store(journal)
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD^'), before)
        resumed = self.journal('legacy')
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual([c['commit'] for c in resumed['commits']], [head])
        self.assertIn('pending', resumed)  # the marker is persisted for every later commit


if __name__ == '__main__':
    unittest.main()
