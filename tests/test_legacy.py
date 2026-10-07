"""State left by pisar <= 0.3 (plain space ids) is diagnosed, never resumed or reinterpreted."""
import hashlib
import json
import unittest

from pisar.operations import Runtime
from .support import Fixture, git


class LegacyState(Fixture):
    def operations(self):
        return Runtime(self.state, self.root).path / 'operations'

    def old_journal(self, ident, status='incomplete', artifacts=()):
        """A journal shaped like pisar 0.3 wrote it: no format marker."""
        path = self.operations() / f'{ident}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(
            schema_version=1, root=str(self.root), operation_id=ident, fingerprint='0' * 64, status=status,
            artifacts=list(artifacts), commits=[], tasks={}, heads={})))
        return path

    def plan(self, space_id, ident='save-old'):
        source = self.external()
        path = self.base / f'{ident}.json'
        path.write_text(json.dumps(dict(
            schema_version=1, operation_id=ident, space_id=space_id,
            source=dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
            meeting=dict(id='old-meeting', title='Old meeting', body='Agreed to launch.'), tasks=[], pages=[])))
        return path

    def assert_diagnosed(self, p, journal, before):
        for text in ('pisar <= 0.3', 'plain space ids', 'BEFORE upgrading', 'abandon', str(journal)):
            self.assertIn(text, p.stderr)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), before)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(json.loads(journal.read_text())['status'], journal_status(journal))

    def test_an_old_save_gets_the_diagnostic_with_its_original_or_converted_plan(self):
        journal = self.old_journal('save-old')
        before = git(self.root, 'rev-parse', 'HEAD')
        for space_id in ('alpha', 'work/alpha'):
            p = self.cli('--ruwana', 'ruwana', 'save', '--plan', self.plan(space_id), ok=False)
            self.assert_diagnosed(p, journal, before)
        self.assertEqual(sorted(path.name for path in self.operations().iterdir()), ['save-old.json'])

    def test_an_old_completed_capture_is_diagnosed_not_reported_as_a_duplicate(self):
        original = 'work/team/alpha/sources/captures/c1/original'
        journal = self.old_journal('capture-alpha-c1', 'complete', [dict(
            path=original, before=None, after='1' * 64, data='', repo=str(self.root))])
        before = git(self.root, 'rev-parse', 'HEAD')
        p = self.cli('capture', '--space', 'work/alpha', '--id', 'c1', '--source', self.external(), ok=False)
        self.assert_diagnosed(p, journal, before)
        self.assertNotIn('duplicate', p.stderr)

    def test_an_unrelated_old_journal_does_not_block_a_new_capture(self):
        # Same plain name, but it wrote files in another space: not this capture's history.
        self.old_journal('capture-alpha-c2', 'complete', [dict(
            path='personal/10-projects/home/sources/captures/c2/original', before=None, after='1' * 64,
            data='', repo=str(self.root))])
        self.cli('capture', '--space', 'work/alpha', '--id', 'c2', '--source', self.external())

    def test_an_old_triage_batch_is_diagnosed(self):
        directory = self.state / 'triage' / 'old-batch'
        directory.mkdir(parents=True)
        manifest = directory / 'manifest.json'
        manifest.write_text(json.dumps(dict(schema_version=1, root=str(self.root), batch_id='old-batch',
                                            fingerprint='0' * 64, status='prepared', items=[])))
        for action in ('report', 'prepare'):
            p = self.cli('triage', action, '--batch', 'old-batch', ok=False)
            self.assertIn('pisar <= 0.3', p.stderr)
            self.assertIn(str(manifest), p.stderr)
        self.assertEqual(json.loads(manifest.read_text())['status'], 'prepared')


def journal_status(path):
    return 'complete' if 'capture' in path.name else 'incomplete'


if __name__ == '__main__':
    unittest.main()
