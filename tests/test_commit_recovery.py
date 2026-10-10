"""Recovery of an interrupted commit is decided by the commit ids pisar journaled, never by its text."""
from pathlib import Path
import hashlib
import json
import os
import unittest
from unittest import mock

from pisar import gitops, operations, spacecmd
from pisar.operations import Runtime
from pisar.safety import WikiError
from pisar.spaces import Wiki
from .support import Fixture, git
from .test_spacecmd import SpaceCommandFixture


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

    def history(self):
        return git(self.root, 'rev-list', 'HEAD').split()

    def interrupt(self, plan, window, edit=None):
        """Run save and interrupt it once: before the commit, after commit-tree, or after update-ref.

        EDIT replaces the message given to commit-tree, so the commit has another subject."""
        real_run, real_commit = gitops.run, gitops.commit
        armed = [True]

        def run(repo, *args, **kwargs):
            if args and args[0] == 'commit-tree' and edit is not None:
                kwargs = {**kwargs, 'input': f'{edit}\n'.encode()}  # the message is read from stdin
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

    def assert_checkpointed(self, operation):
        """Every commit in history is one the journal recorded, once, and nothing is pending."""
        journal = self.journal(operation)
        recorded = [c['commit'] for c in journal['commits']]
        self.assertEqual(journal['status'], 'complete')
        self.assertIsNone(journal['pending'])
        self.assertEqual(len(set(recorded)), len(recorded))
        self.assertEqual(len(self.history()), 1 + len(recorded))  # the fixture commit is the only other one
        self.assertTrue(set(recorded) <= set(self.history()))
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_crash_after_commit_tree_before_update_ref_retries_without_a_duplicate(self):
        plan = self.plan('before-ref')
        before = git(self.root, 'rev-parse', 'HEAD')
        self.interrupt(plan, 'before-ref')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), before)
        pending = self.journal('before-ref')['pending']
        self.assertEqual(pending['parent'], before)
        self.assertNotEqual(pending['commit'], before)  # the unreferenced object is harmless
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan)
        self.assert_checkpointed('before-ref')
        self.assertTrue(self.data('check')['ok'])

    def test_crash_after_update_ref_is_recognised_by_id_even_when_the_subject_differs(self):
        plan = self.plan('by-id')
        before = git(self.root, 'rev-parse', 'HEAD')
        self.interrupt(plan, 'after-ref', edit='Written by hand')
        head = git(self.root, 'rev-parse', 'HEAD')
        self.assertEqual(git(self.root, 'log', '-1', '--format=%s'), 'Written by hand')
        self.assertEqual(self.journal('by-id')['pending'], dict(repo=str(self.root), commit=head, parent=before))
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan)
        self.assertEqual(self.journal('by-id')['commits'][0], dict(repo=str(self.root), commit=head))
        self.assertIn(head, self.history())
        self.assertIn('Written by hand', git(self.root, 'log', '--format=%s'))  # never amended or rewritten
        self.assert_checkpointed('by-id')

    def test_commit_with_the_pisar_subject_that_pisar_did_not_write_is_refused(self):
        plan = self.plan('foreign')
        self.interrupt(plan, 'before-ref')
        git(self.root, 'commit', '-q', '--allow-empty', '-m', 'wiki: foreign')
        head = git(self.root, 'rev-parse', 'HEAD')
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan, ok=False)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.journal('foreign')['status'], 'incomplete')

    def test_foreign_commit_with_the_pisar_subject_is_refused_without_a_pending_record(self):
        plan = self.plan('no-intent')
        self.interrupt(plan, 'before-commit')
        self.assertIsNone(self.journal('no-intent')['pending'])
        git(self.root, 'commit', '-q', '--allow-empty', '-m', 'wiki: no-intent')
        head = git(self.root, 'rev-parse', 'HEAD')
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan, ok=False)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.journal('no-intent')['status'], 'incomplete')

    def test_journal_of_the_old_format_without_pending_still_resumes_by_subject(self):
        plan = self.plan('legacy')
        self.interrupt(plan, 'after-ref')
        head = git(self.root, 'rev-parse', 'HEAD')
        self.assertEqual(git(self.root, 'log', '-1', '--format=%s'), 'wiki: legacy')
        journal = self.journal('legacy')
        journal.pop('pending')  # written by pisar before the commit record existed
        operations.Runtime(self.state, self.root).store(journal)
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan)
        self.assertEqual(self.journal('legacy')['commits'][0], dict(repo=str(self.root), commit=head))
        self.assert_checkpointed('legacy')  # the marker is persisted: later commits are recorded by id


class SpaceRecovery(SpaceCommandFixture):
    """A domain add with --repo makes two commits in two repositories; each one is recovered by its id."""

    def crash(self, repo, window, call, edit=None):
        """Run CALL and interrupt it once in REPO: between commit-tree and update-ref, or right after update-ref."""
        real_run = gitops.run
        armed = [True]

        def run(target, *args, **kwargs):
            mine = Path(target) == repo and armed[0]
            if mine and window == 'before-ref' and args[:1] == ('update-ref',):
                armed[0] = False
                raise WikiError('interrupted between commit-tree and update-ref')
            if mine and edit is not None and args[:1] == ('commit-tree',):
                kwargs = {**kwargs, 'input': f'{edit}\n'.encode()}  # the message is read from stdin
            result = real_run(target, *args, **kwargs)
            if mine and window == 'after-ref' and args[:1] == ('update-ref',):
                armed[0] = False
                raise WikiError('interrupted after update-ref, before the checkpoint')
            return result
        home = self.base / 'crash-home'
        home.mkdir(exist_ok=True)
        (home / '.gitconfig').write_text('[user]\n\temail = synthetic@example.invalid\n\tname = Synthetic fixture\n')
        with mock.patch.dict(os.environ, {'HOME': str(home)}), mock.patch.object(gitops, 'run', run):
            with self.assertRaises(WikiError):
                call()
        self.assertFalse(armed[0])

    def test_crash_before_the_parent_update_ref_retries_the_parent_commit_once(self):
        bare = self.origin()
        root_before = git(self.root, 'rev-parse', 'HEAD')
        self.crash(self.root, 'before-ref', lambda: spacecmd.domain_add(
            Wiki(self.root), self.state, 'acme', 'Acme', str(bare), 'para'))
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), root_before)
        self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme', '--repo', str(bare))
        self.assert_recorded('domain-add-acme')

    def test_crash_after_the_later_parent_commit_is_recognised_by_id_when_its_subject_differs(self):
        bare = self.origin()
        self.crash(self.root, 'after-ref', lambda: spacecmd.domain_add(
            Wiki(self.root), self.state, 'acme', 'Acme', str(bare), 'para'), edit='Written by hand')
        self.assertEqual(git(self.root, 'log', '-1', '--format=%s'), 'Written by hand')
        self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme', '--repo', str(bare))
        self.assertIn('Written by hand', git(self.root, 'log', '--format=%s'))
        self.assert_recorded('domain-add-acme')

    def assert_recorded(self, operation):
        """The journal records each commit it made once, and each of them is in its repository's history."""
        journal = Runtime(self.state, self.root).load(operation)
        self.assertEqual(journal['status'], 'complete')
        self.assertIsNone(journal['pending'])
        recorded = [(c['repo'], c['commit']) for c in journal['commits']]
        self.assertEqual(len(set(recorded)), len(recorded))
        self.assertEqual({repo for repo, _ in recorded}, {str(self.root), str(self.root / 'acme')})
        for repo, commit in recorded:
            self.assertIn(commit, git(Path(repo), 'rev-list', 'HEAD').split())
        root_commits = [c for repo, c in recorded if repo == str(self.root)]
        self.assertEqual(len(root_commits), 1)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertTrue(self.data('check')['ok'])


if __name__ == '__main__':
    unittest.main()
