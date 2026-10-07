import json
from pathlib import Path
import unittest

from .support import Fixture, git, init_repo


DIRTY = 'Alpha rollout: write to jane@client.example.\n'
CLEAN = 'Size a launch team by the number of parallel workstreams.\n'


class LessonCase(Fixture):
    def start(self, source='work', to='personal/home', title='Sizing a launch team'):
        data = self.data('lesson', 'start', '--from', source, '--to', to, '--title', title)
        return data['batch'], Path(data['draft'])

    def check(self, batch, text=None, draft=None, ok=True):
        if text is not None:
            draft.write_text(text, encoding='utf-8')
        return self.data('lesson', 'check', '--batch', batch, ok=ok) if ok else \
            self.cli('lesson', 'check', '--batch', batch, ok=False)

    def review(self, checked, verdict='clear', findings=(), name='review.json', **override):
        body = {'schema_version': 1, 'revision': checked['revision'], 'sha256': checked['sha256'],
                'verdict': verdict, 'findings': list(findings), 'reviewer': {'model': 'synthetic'}}
        body.update(override)
        path = self.base / name
        path.write_text(json.dumps(body))
        return path

    def attach(self, batch, checked, **kwargs):
        return self.data('lesson', 'review', '--batch', batch, '--file', self.review(checked, **kwargs))

    def prepared(self, text=CLEAN, **kwargs):
        batch, draft = self.start(**kwargs)
        checked = self.check(batch, text, draft)
        self.attach(batch, checked)
        return batch, draft, checked

    def workspace(self, batch):
        return self.state / 'lessons' / batch

    def journal_events(self, batch):
        return json.loads((self.workspace(batch) / 'journal.json').read_text())['events']

    def archived(self, batch):
        return list(self.state.glob(f'roots/*/lessons/{batch}.json'))

    def note(self, result):
        return self.root / result['path']


class StartTests(LessonCase):
    def test_start_creates_an_empty_draft_in_the_state_directory(self):
        batch, draft = self.start()
        self.assertEqual(draft, self.workspace(batch) / 'draft.md')
        self.assertEqual(draft.read_text(), '')
        self.assertTrue((self.workspace(batch) / 'meta.json').is_file())
        self.assertFalse(draft.is_relative_to(self.root))

    def test_batch_ids_are_unique(self):
        self.assertNotEqual(self.start()[0], self.start()[0])

    def test_from_may_be_an_address(self):
        batch, _ = self.start(source='work/alpha')
        self.assertTrue(self.workspace(batch).is_dir())

    def test_unknown_domain_or_space_is_refused(self):
        self.cli('lesson', 'start', '--from', 'nowhere', '--to', 'personal/home', '--title', 'T', ok=False)
        self.cli('lesson', 'start', '--from', 'work', '--to', 'personal/missing', '--title', 'T', ok=False)
        self.cli('lesson', 'start', '--from', 'work', '--to', 'home', '--title', 'T', ok=False)
        self.cli('lesson', 'start', '--from', 'work', '--to', 'personal/home', '--title', '  ', ok=False)

    def test_workspace_inside_a_git_repository_or_the_root_is_refused(self):
        outer = self.base / 'repo'
        init_repo(outer)
        for state in (outer / 'state', self.root / 'state'):
            p = self.run_pisar('--root', self.root, '--state-dir', state, 'lesson', 'start',
                               '--from', 'work', '--to', 'personal/home', '--title', 'T', ok=False)
            self.assertIn('pisar:', p.stderr)
        self.assertFalse((outer / 'state/lessons').exists())
        self.assertFalse((self.root / 'state').exists())

    def test_cross_domain_start_warns_prominently(self):
        data = self.data('lesson', 'start', '--from', 'work', '--to', 'personal/home', '--title', 'T')
        self.assertTrue(data['warnings'])
        same = self.data('lesson', 'start', '--from', 'work', '--to', 'work/alpha', '--title', 'T')
        self.assertEqual(same['warnings'], [])


class CheckTests(LessonCase):
    def test_check_snapshots_revisions_and_reports_findings(self):
        batch, draft = self.start()
        first = self.check(batch, DIRTY, draft)
        self.assertEqual(first['revision'], 'r1')
        ws = self.workspace(batch)
        self.assertEqual((ws / 'revisions/r1.md').read_text(), DIRTY)
        self.assertTrue((ws / 'r1.report.json').is_file())
        categories = {f['category'] for f in first['findings']}
        self.assertIn('email', categories)
        self.assertIn('term', categories)  # "Alpha" is a space id of the source domain
        self.assertTrue(all(f['id'] for f in first['findings']))
        second = self.check(batch, CLEAN, draft)
        self.assertEqual(second['revision'], 'r2')
        self.assertEqual(second['findings'], [])
        self.assertIn('-Alpha rollout', second['diff'])
        self.assertIn('+Size a launch', second['diff'])
        self.assertEqual((ws / 'revisions/r1.md').read_text(), DIRTY)  # immutable

    def test_unchanged_draft_keeps_the_current_revision_and_its_review(self):
        batch, draft, checked = self.prepared()
        again = self.check(batch)
        self.assertEqual(again['revision'], checked['revision'])
        self.assertEqual(again['sha256'], checked['sha256'])
        self.assertTrue((self.workspace(batch) / 'r1.review.json').is_file())

    def test_finding_ids_are_stable_across_revisions(self):
        batch, draft = self.start()
        one = self.check(batch, DIRTY, draft)
        two = self.check(batch, DIRTY + 'More text.\n', draft)
        self.assertEqual({f['id'] for f in one['findings']}, {f['id'] for f in two['findings']})

    def test_empty_draft_is_refused(self):
        batch, _ = self.start()
        self.check(batch, ok=False)

    def test_check_never_edits_the_draft(self):
        batch, draft = self.start()
        self.check(batch, DIRTY, draft)
        self.assertEqual(draft.read_text(), DIRTY)

    def test_unknown_batch_is_refused(self):
        self.cli('lesson', 'check', '--batch', 'lesson-nope', ok=False)


class ReviewTests(LessonCase):
    def test_review_is_attached_to_the_current_revision(self):
        batch, draft = self.start()
        checked = self.check(batch, CLEAN, draft)
        out = self.attach(batch, checked, verdict='concerns', findings=[
            {'tier': 'ask', 'category': 'quote', 'excerpt': 'x', 'comment': 'c', 'suggestion': 's'}])
        self.assertEqual(out['verdict'], 'concerns')
        stored = json.loads((self.workspace(batch) / 'r1.review.json').read_text())
        self.assertEqual(stored['sha256'], checked['sha256'])

    def test_stale_review_is_refused(self):
        batch, draft = self.start()
        old = self.check(batch, DIRTY, draft)
        self.check(batch, CLEAN, draft)
        p = self.cli('lesson', 'review', '--batch', batch, '--file', self.review(old), ok=False)
        self.assertIn('stale', p.stderr)
        self.assertFalse((self.workspace(batch) / 'r1.review.json').exists())

    def test_wrong_hash_is_refused(self):
        batch, draft = self.start()
        checked = self.check(batch, CLEAN, draft)
        self.cli('lesson', 'review', '--batch', batch, '--file',
                 self.review(checked, sha256='0' * 64), ok=False)

    def test_contract_violations_are_refused(self):
        batch, draft = self.start()
        checked = self.check(batch, CLEAN, draft)
        bad = [dict(verdict='fine'), dict(schema_version=2), dict(findings='none'), dict(reviewer={}),
               dict(findings=[{'tier': 'huge', 'category': 'c', 'excerpt': 'e', 'comment': 'c'}]),
               dict(findings=[{'tier': 'ask', 'category': 'c', 'excerpt': 'e'}]),
               dict(extra=1)]
        for override in bad:
            self.cli('lesson', 'review', '--batch', batch, '--file',
                     self.review(checked, **override), ok=False)
        (self.base / 'broken.json').write_text('{')
        self.cli('lesson', 'review', '--batch', batch, '--file', self.base / 'broken.json', ok=False)
        self.assertFalse((self.workspace(batch) / 'r1.review.json').exists())


class ShowTests(LessonCase):
    def test_show_is_a_human_readable_report(self):
        batch, draft = self.start()
        first = self.check(batch, CLEAN, draft)
        checked = self.check(batch, DIRTY, draft)
        self.attach(batch, checked, verdict='block', findings=[
            {'tier': 'severe', 'category': 'client', 'excerpt': 'jane', 'comment': 'names a client',
             'suggestion': 'say "a client"'}])
        out = self.cli('lesson', 'show', '--batch', batch).stdout
        self.assertFalse(out.lstrip().startswith('{'))
        for expected in ('personal/home', 'jane@client.example', 'severe', 'block', 'names a client',
                         'say "a client"', 'CROSS-DOMAIN', '+Alpha rollout', batch, 'r2'):
            self.assertIn(expected, out)
        self.assertEqual(first['revision'], 'r1')

    def test_show_lists_open_decisions(self):
        batch, draft = self.start()
        self.check(batch, DIRTY, draft)
        out = self.cli('lesson', 'show', '--batch', batch).stdout
        self.assertIn('no review', out.lower())
        self.assertIn('--keep', out)


class AcceptTests(LessonCase):
    def accept(self, batch, *extra, ok=True):
        return self.cli('lesson', 'accept', '--batch', batch, *extra, ok=ok)

    def test_accept_writes_exactly_the_reviewed_revision(self):
        text = 'Line one\r\n\n  Indented, trailing spaces   \n\nлаunch — unicode\n'
        batch, draft, checked = self.prepared(text)
        result = json.loads(self.accept(batch).stdout)
        note = self.note(result)
        revision = (self.workspace(batch) / 'revisions/r1.md').read_bytes()
        self.assertEqual(revision, text.encode())
        self.assertTrue(note.read_bytes().endswith(revision))
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertIn(result['operation_id'], git(self.root, 'log', '-1', '--format=%s'))
        self.assertEqual(git(self.root, 'show', '--name-only', '--format=', 'HEAD'),
                         result['path'])
        self.assertEqual(self.data('check')['ok'], True)
        doc = self.data('read', result['reference'])
        self.assertEqual(doc['metadata']['type'], 'note')
        self.assertEqual(doc['metadata']['sources'], [])
        self.assertEqual(doc['metadata']['space_ids'], ['personal/home'])
        self.assertEqual(doc['metadata']['title'], 'Sizing a launch team')

    def test_note_carries_no_reference_to_the_origin(self):
        batch, _, _ = self.prepared(source='work/alpha')
        text = self.note(json.loads(self.accept(batch).stdout)).read_text()
        self.assertNotIn('wiki:', text)
        self.assertNotRegex(text, r'(?i)\b(work|alpha|origin)\b')

    def test_mention_origin_adds_only_a_plain_text_domain_title(self):
        batch, _, _ = self.prepared(source='work/alpha')
        result = json.loads(self.accept(batch, '--mention-origin').stdout)
        text = self.note(result).read_text()
        doc = self.data('read', result['reference'])
        self.assertIn('Origin: Work', text)
        self.assertNotIn('wiki:', text)
        self.assertNotIn('alpha', text)
        self.assertEqual(doc['metadata']['sources'], [])
        self.assertTrue(text.endswith(CLEAN))
        self.assertTrue(self.journal_events(batch)[-1]['mention_origin'])

    def test_accept_requires_a_review_of_the_current_revision(self):
        batch, draft = self.start()
        self.check(batch, CLEAN, draft)
        p = self.accept(batch, ok=False)
        self.assertIn('review', p.stderr)
        self.assertFalse((self.root / 'personal/10-projects/home/notes').exists())

    def test_edit_after_review_makes_the_review_stale(self):
        batch, draft, _ = self.prepared()
        self.check(batch, CLEAN + 'Another point.\n', draft)
        self.assertIn('review', self.accept(batch, ok=False).stderr)

    def test_unchecked_draft_changes_are_refused(self):
        batch, draft, _ = self.prepared()
        draft.write_text(CLEAN + 'unchecked\n')
        self.assertIn('check', self.accept(batch, ok=False).stderr)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_findings_need_a_decision(self):
        batch, draft = self.start()
        checked = self.check(batch, DIRTY, draft)
        self.attach(batch, checked)
        p = self.accept(batch, ok=False)
        for finding in checked['findings']:
            self.assertIn(finding['id'], p.stderr)
        ids = [f['id'] for f in checked['findings']]
        self.accept(batch, '--keep', ','.join(ids[:-1]), ok=False)
        result = json.loads(self.accept(batch, '--keep', ','.join(ids)).stdout)
        self.assertEqual(sorted(result['keeps']), sorted(ids))
        self.assertTrue(self.note(result).read_text().endswith(DIRTY))  # nothing stripped
        event = self.journal_events(batch)[-1]
        self.assertEqual(sorted(event['keeps']), sorted(ids))

    def test_keep_accepts_repeated_flags_and_rejects_unknown_ids(self):
        batch, draft = self.start()
        checked = self.check(batch, DIRTY, draft)
        self.attach(batch, checked)
        ids = [f['id'] for f in checked['findings']]
        self.accept(batch, '--keep', ids[0], '--keep', 'deadbeef00', ok=False)
        self.accept(batch, '--keep', ','.join(ids))

    def test_a_finding_removed_in_a_later_revision_needs_no_keep(self):
        batch, draft = self.start()
        self.check(batch, DIRTY, draft)
        checked = self.check(batch, CLEAN, draft)
        self.attach(batch, checked)
        result = json.loads(self.accept(batch).stdout)
        self.assertEqual(result['keeps'], [])

    def test_skip_review_is_recorded(self):
        batch, draft = self.start()
        self.check(batch, CLEAN, draft)
        result = json.loads(self.accept(batch, '--skip-review').stdout)
        self.assertTrue(result['skip_review'])
        self.assertTrue(self.journal_events(batch)[-1]['skip_review'])

    def test_block_verdict_is_advisory(self):
        batch, draft = self.start()
        checked = self.check(batch, CLEAN, draft)
        self.attach(batch, checked, verdict='block')
        result = json.loads(self.accept(batch).stdout)
        self.assertEqual(result['verdict'], 'block')
        self.assertTrue(self.note(result).is_file())

    def test_retry_of_a_completed_accept_changes_nothing(self):
        batch, _, _ = self.prepared()
        first = json.loads(self.accept(batch).stdout)
        head = git(self.root, 'rev-parse', 'HEAD')
        again = json.loads(self.accept(batch).stdout)
        self.assertEqual(again['path'], first['path'])
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.accept(batch, '--mention-origin', ok=False)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_retry_after_user_edit_of_the_note_preserves_it(self):
        batch, _, _ = self.prepared()
        note = self.note(json.loads(self.accept(batch).stdout))
        note.write_text(note.read_text() + 'my own edit\n')
        self.accept(batch, ok=False)
        self.assertTrue(note.read_text().endswith('my own edit\n'))

    def test_uncommitted_user_edits_block_the_write(self):
        batch, _, _ = self.prepared()
        stray = self.personal / 'stray.md'
        stray.write_text('user work in progress\n')
        self.accept(batch, ok=False)
        self.assertEqual(stray.read_text(), 'user work in progress\n')
        self.assertFalse((self.personal / 'notes').exists())
        stray.unlink()
        self.accept(batch)

    def test_accept_unknown_target_after_space_removed(self):
        batch, _, _ = self.prepared()
        git(self.root, 'rm', '-rq', 'personal/10-projects/home')
        git(self.root, 'commit', '-qm', 'remove space')
        self.accept(batch, ok=False)

    def test_cross_domain_warning_is_reported_but_not_blocking(self):
        batch, _, _ = self.prepared(source='personal', to='work/alpha')
        result = json.loads(self.accept(batch).stdout)
        self.assertTrue(result['warnings'])
        self.assertTrue(self.note(result).is_file())

    def test_same_domain_has_no_warning(self):
        batch, _, _ = self.prepared(source='work', to='work/beta')
        self.assertEqual(json.loads(self.accept(batch).stdout)['warnings'], [])

    def test_duplicate_note_titles_get_distinct_documents_or_a_clear_refusal(self):
        first, _, _ = self.prepared()
        second, _, _ = self.prepared()
        one = json.loads(self.accept(first).stdout)
        p = self.accept(second, ok=False)
        self.assertIn('personal/home', one['reference'])
        self.assertIn('pisar:', p.stderr)
        self.assertEqual(self.data('check')['ok'], True)

    def test_accept_into_a_submodule_commits_child_then_parent(self):
        module = self.add_submodule()
        batch, _, _ = self.prepared(source='personal', to='work/module')
        result = json.loads(self.accept(batch).stdout)
        self.assertEqual(git(module, 'status', '--porcelain'), '')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertIn(result['operation_id'], git(module, 'log', '-1', '--format=%s'))
        self.assertIn(result['operation_id'], git(self.root, 'log', '-1', '--format=%s'))
        self.assertTrue(self.note(result).is_file())

    def test_accepted_batch_cannot_be_checked_or_reviewed_again(self):
        batch, draft, checked = self.prepared()
        self.accept(batch)
        self.check(batch, CLEAN + 'late\n', draft, ok=False)
        self.cli('lesson', 'review', '--batch', batch, '--file', self.review(checked), ok=False)


class DiscardTests(LessonCase):
    def test_discard_removes_the_workspace_but_keeps_a_journal_entry(self):
        batch, _, _ = self.prepared()
        self.data('lesson', 'discard', '--batch', batch)
        self.assertFalse(self.workspace(batch).exists())
        entries = self.archived(batch)
        self.assertEqual(len(entries), 1)
        events = [e['event'] for e in json.loads(entries[0].read_text())['events']]
        self.assertEqual(events[0], 'start')
        self.assertEqual(events[-1], 'discard')
        self.assertFalse(any(entries[0].is_relative_to(self.root) for _ in [0]))

    def test_discarded_batch_is_gone(self):
        batch, _, _ = self.prepared()
        self.data('lesson', 'discard', '--batch', batch)
        for args in (('check',), ('show',), ('accept',), ('discard',)):
            self.cli('lesson', *args, '--batch', batch, ok=False)

    def test_discard_after_accept_keeps_the_note_and_the_accept_record(self):
        batch, _, _ = self.prepared()
        result = json.loads(self.cli('lesson', 'accept', '--batch', batch).stdout)
        self.data('lesson', 'discard', '--batch', batch)
        self.assertTrue(self.note(result).is_file())
        entry = self.archived(batch)[0]
        self.assertIn('accept', [e['event'] for e in json.loads(entry.read_text())['events']])


if __name__ == '__main__':
    unittest.main()
