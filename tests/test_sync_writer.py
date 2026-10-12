"""With the commit-message writer enabled, an interrupted merge or resolve resumes from its journal (review 20,
finding 7). The writer is a counting stub of the message composer; the journal, the message cache and the commit
creation are the real code. A retry must neither write a second message nor create a second merge commit."""
from pathlib import Path
from unittest import mock

from pisar import config, sync
from .sync_faults import Faults, RemoteFixture, run_interrupted
from .support import document, git


class WriterStub:
    def __init__(self):
        self.calls = []

    def compose(self, changes, operation, explanation, options, root, base):
        self.calls.append(operation)
        return f'wiki: {operation}\n\nSynthetic writer text for the merge.'


class EnabledWriterTests(RemoteFixture):
    def setUp(self):
        super().setUp()
        self.writer = WriterStub()
        options = {**config.COMMIT_MESSAGE_DEFAULTS, 'enabled': True}
        patches = [
            mock.patch('pisar.settings.commit_message', return_value=options),
            mock.patch('pisar.commit_message.current', return_value={
                'what': 'Merge the remote change', 'why': 'Both sides are kept.', 'context': None}),
            mock.patch('pisar.commit_message.compose', side_effect=self.writer.compose),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def diverge(self):
        (self.root / 'local.md').write_text(document('local-note', 'work/alpha', body='Local.'))
        self.publish(lambda repo: (repo / 'work/team/alpha/remote.md').write_text(
            document('remote-note', 'work/alpha', body='Remote.')))
        (self.root / 'work/team/alpha/local.md').write_text(document('local-note', 'work/alpha', body='Local.'))
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'local change')

    def test_a_merge_interrupted_around_the_commit_resumes_with_one_message_and_one_commit(self):
        for fault in ('message-ready', 'merge-commit'):
            with self.subTest(fault=fault):
                self.new_world(f'merge-{fault}')
                self.writer.calls.clear()
                self.diverge()
                self.start()
                before = int(git(self.root, 'rev-list', '--count', '--merges', 'HEAD'))
                with Faults(fault):
                    self.assertTrue(run_interrupted(sync.do_pull, self.root, self.state, merge=True))
                result = sync.do_pull(self.root, self.state, merge=True)
                self.assertEqual(result['status'], 'merge', result)
                self.assertEqual(len(self.writer.calls), 1, self.writer.calls)
                self.assertEqual(int(git(self.root, 'rev-list', '--count', '--merges', 'HEAD')), before + 1)
                self.assertEqual(git(self.root, 'rev-parse', 'HEAD^2'),
                                 git(self.root, 'rev-parse', f'refs/pisar/sync/origin/{self.branch}'))
                self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_a_resolve_interrupted_around_the_commit_resumes_with_one_message_and_one_commit(self):
        for fault in ('message-ready', 'merge-commit'):
            with self.subTest(fault=fault):
                self.new_world(f'resolve-{fault}')
                self.writer.calls.clear()
                self.publish(lambda repo: (repo / 'work/team/alpha/call.md').write_text(
                    document(related=('work/beta',), body='Remote wording.')))
                (self.root / 'work/team/alpha/call.md').write_text(
                    document(related=('work/beta',), body='Local wording.'))
                git(self.root, 'commit', '-q', '-am', 'local wording')
                self.start()
                conflicted = sync.do_pull(self.root, self.state, merge=True)
                self.assertEqual(conflicted['status'], 'conflicts')
                (Path(conflicted['work']) / 'work/team/alpha/call.md').write_text(
                    document(related=('work/beta',), body='Both kept.'))
                before = int(git(self.root, 'rev-list', '--count', '--merges', 'HEAD'))
                with Faults(fault):
                    self.assertTrue(run_interrupted(sync.do_resolve, self.root, self.state, conflicted['sync_id']))
                result = sync.do_pull(self.root, self.state, merge=True)
                self.assertEqual(result['status'], 'merge', result)
                self.assertEqual(len(self.writer.calls), 1, self.writer.calls)
                self.assertEqual(int(git(self.root, 'rev-list', '--count', '--merges', 'HEAD')), before + 1)
                self.assertIn('Both kept.', (self.root / 'work/team/alpha/call.md').read_text())

    def test_a_dry_run_of_a_prepared_merge_writes_nothing(self):
        self.new_world('prepared-dry-run')
        self.writer.calls.clear()
        self.diverge()
        self.start()
        with Faults('message-ready'):
            self.assertTrue(run_interrupted(sync.do_pull, self.root, self.state, merge=True))
        before, journals = self.snapshot(), self.journals()
        result = sync.do_pull(self.root, self.state, merge=True, dry_run=True)
        self.assertEqual((result['status'], result['phase']), ('would-resume', 'prepared'))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.journals(), journals)
        self.assertEqual(len(self.writer.calls), 1)  # the message is cached; the dry run asked for none
