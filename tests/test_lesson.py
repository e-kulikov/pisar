import json
import os
from pathlib import Path
import unittest
from unittest import mock

from pisar import gitops, lesson
from pisar.spaces import Wiki
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
        self.assertIn(f'\nrevision: r2\nsha256: {checked["sha256"]}\n', out)

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

    def test_body_references_are_validated_before_the_write(self):
        text = 'See wiki:work/alpha:call-one for the story.\n'
        batch, draft = self.start()
        checked = self.check(batch, text, draft)
        self.attach(batch, checked)
        keeps = ','.join(f['id'] for f in checked['findings'])
        p = self.accept(batch, '--keep', keeps, ok=False)
        self.assertIn('wiki:work/alpha:call-one', p.stderr)
        self.assertEqual(draft.read_text(), text)  # nothing stripped
        self.assertFalse((self.personal / 'notes').exists())
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(self.data('check')['ok'], True)

    def test_same_domain_body_reference_is_accepted(self):
        batch, draft = self.start(source='work', to='work/alpha')
        checked = self.check(batch, 'Follow-up to wiki:work/alpha:call-one.\n', draft)
        self.attach(batch, checked)
        self.accept(batch, '--keep', ','.join(f['id'] for f in checked['findings']))
        self.assertEqual(self.data('check')['ok'], True)

    def test_finalization_is_repaired_on_a_completed_retry(self):
        batch, draft = self.start()
        self.check(batch, CLEAN, draft)
        first = json.loads(self.accept(batch, '--skip-review').stdout)
        journal = self.workspace(batch) / 'journal.json'
        value = json.loads(journal.read_text())
        value['events'] = [e for e in value['events'] if e['event'] != 'accept']
        journal.write_text(json.dumps(value))
        self.archived(batch)[0].unlink()
        again = json.loads(self.accept(batch, '--skip-review').stdout)
        self.assertEqual(again['path'], first['path'])
        event = self.journal_events(batch)[-1]
        self.assertEqual((event['event'], event['skip_review']), ('accept', True))
        self.assertIn('accept', [e['event'] for e in json.loads(self.archived(batch)[0].read_text())['events']])
        self.accept(batch, '--skip-review')
        self.assertEqual([e['event'] for e in self.journal_events(batch)].count('accept'), 1)

    def interrupted_submodule_accept(self):
        self.add_submodule()
        batch, _, _ = self.prepared(source='personal', to='work/module')
        real = gitops.commit

        def commit(repo, paths, operation, **kwargs):
            if repo == self.root:
                raise gitops.WikiError('interrupted before the parent commit')
            return real(repo, paths, operation, **kwargs)
        with mock.patch.object(gitops, 'commit', commit), self.assertRaises(gitops.WikiError):
            lesson.accept(Wiki(self.root), self.state, batch)
        return batch

    def test_discard_is_refused_while_the_acceptance_is_incomplete(self):
        batch = self.interrupted_submodule_accept()
        p = self.cli('lesson', 'discard', '--batch', batch, ok=False)
        self.assertIn('accept', p.stderr)
        self.assertTrue(self.workspace(batch).is_dir())
        result = json.loads(self.accept(batch).stdout)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertTrue(self.note(result).is_file())
        self.data('lesson', 'discard', '--batch', batch)

    def test_new_revisions_are_refused_once_acceptance_has_started(self):
        batch = self.interrupted_submodule_accept()
        draft = self.workspace(batch) / 'draft.md'
        original = draft.read_text()
        draft.write_text(original + 'edited after the interruption\n')
        for args in (('check',), ('review', '--file', self.base / 'x.json')):
            p = self.cli('lesson', *args, '--batch', batch, ok=False)
            self.assertIn('accept', p.stderr)
        self.assertFalse((self.workspace(batch) / 'revisions/r2.md').exists())
        draft.write_text(original)
        result = json.loads(self.accept(batch).stdout)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertTrue(self.note(result).is_file())

    def test_resumed_acceptance_ignores_an_edited_draft(self):
        batch = self.interrupted_submodule_accept()
        draft = self.workspace(batch) / 'draft.md'
        persisted = (self.workspace(batch) / 'revisions/r1.md').read_bytes()
        draft.write_text('A different lesson, edited after the interruption.\n')
        for args in (('check',), ('discard',)):
            self.assertIn('accept', self.cli('lesson', *args, '--batch', batch, ok=False).stderr)
        result = json.loads(self.accept(batch).stdout)
        self.assertTrue(self.note(result).read_bytes().endswith(persisted))
        self.assertEqual(draft.read_text(), 'A different lesson, edited after the interruption.\n')
        self.assertTrue(any('draft' in note and 'ignored' in note for note in result['notes']))
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.data('lesson', 'discard', '--batch', batch)

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root ignores file permissions')
    def test_unreadable_draft_does_not_fail_a_resumed_acceptance(self):
        batch = self.interrupted_submodule_accept()
        draft = self.workspace(batch) / 'draft.md'
        draft.chmod(0)
        self.addCleanup(draft.chmod, 0o600)
        first = json.loads(self.accept(batch).stdout)
        again = json.loads(self.accept(batch).stdout)
        for result in (first, again):
            self.assertTrue(any('draft' in note and 'untouched' in note for note in result['notes']))
        self.assertTrue(self.note(first).is_file())
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def make_auxiliary_unreadable(self, batch):
        ws = self.workspace(batch)
        for path in [ws / 'draft.md', *ws.glob('revisions/*.md'), *ws.glob('r*.report.json'),
                     *ws.glob('r*.review.json')]:
            path.chmod(0)
            self.addCleanup(path.chmod, 0o600)

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root ignores file permissions')
    def test_finalized_and_resumed_acceptance_need_no_auxiliary_workspace_file(self):
        batch, _, _ = self.prepared()
        first = json.loads(self.accept(batch).stdout)
        baseline = json.loads(self.accept(batch).stdout)
        self.make_auxiliary_unreadable(batch)
        self.assertEqual(json.loads(self.accept(batch).stdout), baseline)
        self.assertEqual(baseline['verdict'], 'clear')
        self.assertEqual(baseline['path'], first['path'])
        # The same after an interruption: the verdict comes from the persisted decision.
        resumed = self.interrupted_submodule_accept()
        self.make_auxiliary_unreadable(resumed)
        result = json.loads(self.accept(resumed).stdout)
        self.assertEqual(result['verdict'], 'clear')
        self.assertEqual(self.journal_events(resumed)[-1]['verdict'], 'clear')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(json.loads(self.accept(resumed).stdout), result)

    def crash(self, batch, patched, *args):
        """Run accept in-process with PATCHED failing, as a process killed at that point would."""
        with patched, self.assertRaises(gitops.WikiError):
            lesson.accept(Wiki(self.root), self.state, batch, *args)

    def operation_record(self, batch):
        return json.loads(next(self.state.glob(f'roots/*/operations/{batch}.json')).read_text())

    def test_crash_before_any_repository_change_counts_as_not_started(self):
        batch, draft, _ = self.prepared()
        stores = []
        real_store = lesson.Runtime.store

        def store(runtime, journal):
            stores.append(json.loads(json.dumps(journal)))
            return real_store(runtime, journal)
        with mock.patch.object(lesson.Runtime, 'store', store):
            self.crash(batch, mock.patch.object(lesson, 'apply_files', side_effect=gitops.WikiError('crash')))
        self.assertIn('decision', stores[0])  # operation record and decision are ONE write
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        draft.write_text(CLEAN + 'edited after the crash\n')
        checked = self.check(batch)
        self.assertEqual(checked['revision'], 'r2')
        self.attach(batch, checked)
        result = json.loads(self.accept(batch).stdout)
        self.assertTrue(self.note(result).read_bytes().endswith(draft.read_bytes()))
        self.assertIn('restart', [e['event'] for e in self.journal_events(batch)])
        self.assertEqual(self.operation_record(batch)['status'], 'complete')

    def test_not_started_acceptance_can_be_edited_or_discarded(self):
        batch, draft, _ = self.prepared()
        self.crash(batch, mock.patch.object(lesson, 'apply_files', side_effect=gitops.WikiError('crash')))
        draft.write_text('A new direction.\n')
        self.accept(batch, ok=False)  # the draft is not the reviewed revision: asks for check, which works
        self.check(batch)
        self.data('lesson', 'discard', '--batch', batch)
        other, _, _ = self.prepared()
        self.crash(other, mock.patch.object(lesson, 'apply_files', side_effect=gitops.WikiError('crash')))
        self.data('lesson', 'discard', '--batch', other)

    def test_written_but_uncommitted_note_counts_as_started(self):
        batch, draft, _ = self.prepared()
        self.crash(batch, mock.patch.object(gitops, 'commit', side_effect=gitops.WikiError('crash')))
        self.assertTrue((self.personal / 'notes').is_dir())
        draft.write_text('edited\n')
        for args in (('check',), ('discard',)):
            self.assertIn('accept', self.cli('lesson', *args, '--batch', batch, ok=False).stderr)
        result = json.loads(self.accept(batch).stdout)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertTrue(self.note(result).is_file())

    def test_committed_but_unrecorded_acceptance_counts_as_started(self):
        batch, draft, _ = self.prepared()
        first = json.loads(self.accept(batch).stdout)
        record = next(self.state.glob(f'roots/*/operations/{batch}.json'))
        value = json.loads(record.read_text())
        value.update(status='incomplete', commits=[], heads={
            k: git(self.root, 'rev-parse', 'HEAD~1') for k in value['heads']})
        record.write_text(json.dumps(value))
        meta = self.workspace(batch) / 'meta.json'
        info = json.loads(meta.read_text())
        info['status'] = 'open'
        meta.write_text(json.dumps(info))
        draft.write_text('edited\n')
        for args in (('check',), ('discard',)):
            self.assertIn('accept', self.cli('lesson', *args, '--batch', batch, ok=False).stderr)
        self.assertEqual(json.loads(self.accept(batch).stdout)['path'], first['path'])

    def test_an_edit_between_write_and_staging_never_enters_the_commit(self):
        batch, _, _ = self.prepared()
        real = gitops.commit
        written = []

        def commit(repo, paths, operation, **kwargs):
            note = next((self.personal / 'notes').glob('*.md'))
            written.append(note.read_bytes())
            note.write_bytes(note.read_bytes() + b'UNREVIEWED EDIT\n')
            return real(repo, paths, operation, **kwargs)
        with mock.patch.object(gitops, 'commit', commit):
            result = lesson.accept(Wiki(self.root), self.state, batch)
        committed = git(self.root, 'show', f'HEAD:{result["path"]}')
        self.assertEqual(committed, written[0].decode().strip())
        self.assertNotIn('UNREVIEWED', committed)
        self.assertIn('UNREVIEWED EDIT', self.note(result).read_text())  # the edit is preserved, uncommitted

    def stale_record(self, batch):
        self.crash(batch, mock.patch.object(lesson, 'apply_files', side_effect=gitops.WikiError('crash')))
        item = self.operation_record(batch)['artifacts'][0]
        leftover = self.root / item['path']
        leftover = leftover.parent / item['temp']  # the journal names the owned temporary file
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_bytes(b'half written note')
        return leftover

    def test_an_owned_temporary_leftover_does_not_block_recovery(self):
        batch, _, _ = self.prepared()
        leftover = self.stale_record(batch)
        foreign = leftover.parent / '.pisar-someone-else'
        foreign.write_bytes(b'not ours')
        self.accept(batch, ok=False)  # only the operation's own leftover is recovered
        self.assertTrue(foreign.exists())
        foreign.unlink()
        result = json.loads(self.accept(batch).stdout)
        self.assertFalse(leftover.exists())
        self.assertTrue(self.note(result).is_file())
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_discard_removes_only_an_owned_leftover(self):
        batch, _, _ = self.prepared()
        leftover = self.stale_record(batch)
        self.data('lesson', 'discard', '--batch', batch)
        self.assertFalse(leftover.exists())
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_discard_waits_for_the_decision_record(self):
        batch, draft = self.start()
        self.check(batch, CLEAN, draft)
        self.accept(batch, '--skip-review')
        meta = self.workspace(batch) / 'meta.json'
        value = json.loads(meta.read_text())
        value['status'] = 'open'
        meta.write_text(json.dumps(value))
        journal = self.workspace(batch) / 'journal.json'
        value = json.loads(journal.read_text())
        value['events'] = [e for e in value['events'] if e['event'] != 'accept']
        journal.write_text(json.dumps(value))
        p = self.cli('lesson', 'discard', '--batch', batch, ok=False)
        self.assertIn('accept', p.stderr)
        self.assertTrue(self.workspace(batch).is_dir())
        self.accept(batch, '--skip-review')
        self.data('lesson', 'discard', '--batch', batch)
        events = json.loads(self.archived(batch)[0].read_text())['events']
        self.assertTrue(next(e for e in events if e['event'] == 'accept')['skip_review'])

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
