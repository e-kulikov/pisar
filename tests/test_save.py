import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import patch
from .support import Fixture, RUWANA, RUWANA_AVAILABLE, commit_all, document, git


BODY = '''# Synthetic launch meeting

## Context
Date/time and participants unknown. Related project beta.
## Summary
The team agreed to prepare a launch and review it.
## Timeline and themes
Launch options were discussed in the source.
## Decisions
Prepare a launch draft.
## Proposals
Consider a broader release; not agreed.
## Tasks at the meeting
Prepare launch draft; owner and due unknown.
## Open questions
Scope is still open.
## Contradictions
None recorded.
## Completeness and limitations
Synthetic fixture; source has no reliable timestamp.
'''


class SaveTests(Fixture):
    def plan(self, ident='save-one', tasks=None):
        source = self.external()
        value = dict(schema_version=1, operation_id=ident, space_id='alpha',
                     source=dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
                     meeting=dict(id='launch-meeting', title='Launch meeting', body=BODY,
                                  related_space_ids=['beta']),
                     tasks=tasks if tasks is not None else [
                         dict(id='prepare-launch', space_id='alpha', title='Prepare launch', agreed=True),
                         dict(id='review-launch', space_id='beta', title='Review launch', agreed=True)],
                     pages=[])
        path = self.base / f'{ident}.json'
        path.write_text(json.dumps(value))
        return path, value

    def save(self, path, binary=RUWANA, ok=True):
        return self.cli('--ruwana', binary, 'save', '--plan', path, ok=ok)

    def ruwana(self, *args):
        p = subprocess.run([str(RUWANA), *args], env={**self.env, 'WIKI_ROOT': str(self.root)},
                           cwd=self.base, text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_real_save_creates_one_meeting_and_two_distinct_tasks_and_retries_closed(self):
        self.require_ruwana()
        path, value = self.plan()
        result = json.loads(self.save(path).stdout)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(result['tasks']), 2)
        for space_id, task_id in (('alpha', 'prepare-launch'), ('beta', 'review-launch')):
            project = 'work/team/' + space_id
            tasks = json.loads(self.ruwana('list', '--project', project, '--all', '--format', 'json'))
            self.assertEqual(len(tasks), 1)
            self.assertIn('wiki:alpha:launch-meeting#task-' + task_id, tasks[0]['source'])
            self.ruwana('done', tasks[0]['id'], '--project', project)
        commit_all(self.root)  # simulate legitimate task status change outside wiki CLI
        head = git(self.root, 'rev-parse', 'HEAD')
        self.save(path)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        meeting = self.data('read', 'wiki:alpha:launch-meeting')
        self.assertIn('## Ruwana references', meeting['content'])
        self.assertIn(next(iter(result['tasks'].values()))['id'], meeting['content'])
        self.assertEqual(meeting['metadata']['space_ids'], ['alpha', 'beta'])
        self.assertEqual(len(list(self.root.rglob('launch-meeting.md'))), 1)
        self.assertEqual((self.alpha / 'sources/meetings/launch-meeting/transcript.md').read_bytes(),
                         Path(value['source']['path']).read_bytes())
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_missing_binary_reports_incomplete_and_real_retry_finishes(self):
        path, _ = self.plan()
        failed = self.save(path, binary=self.base / 'does-not-exist', ok=False)
        self.assertIn('ruwana', failed.stderr.lower())
        self.assertIn('unavailable', failed.stderr.lower())
        journals = list(self.state.rglob('operations/save-one.json'))
        self.assertEqual(len(journals), 1)
        journal = json.loads(journals[0].read_text())
        self.assertEqual(journal['status'], 'incomplete')
        self.assertEqual(journal['tasks'], {})
        self.assertTrue((self.alpha / 'meetings/launch-meeting.md').exists())
        self.assertFalse(list(self.root.rglob('.ruwana/*.toml')))
        if RUWANA_AVAILABLE:
            self.assertEqual(json.loads(self.save(path).stdout)['status'], 'complete')

    def test_crash_after_actual_task_add_before_journal_deduplicates(self):
        self.require_ruwana()
        from pisar.operations import save
        from pisar.spaces import Wiki
        from pisar.ruwana import Ruwana
        path, _ = self.plan(tasks=[dict(id='prepare-launch', space_id='alpha', title='Prepare launch', agreed=True)])
        original = Ruwana.ensure

        def interrupt_after_creation(adapter, *args, **kwargs):
            original(adapter, *args, **kwargs)
            raise RuntimeError('synthetic process crash before checkpoint')

        with patch.object(Ruwana, 'ensure', interrupt_after_creation):
            with self.assertRaises(RuntimeError):
                save(Wiki(self.root), self.state, path, str(RUWANA))
        self.save(path)
        tasks = json.loads(self.ruwana('list', '--project', 'work/team/alpha', '--all', '--format', 'json'))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_source_and_changed_operation_conflicts_before_writes(self):
        path, value = self.plan(tasks=[])
        Path(value['source']['path']).write_text('changed original')
        self.save(path, ok=False)
        self.assertFalse((self.alpha / 'meetings').exists())
        path, value = self.plan(tasks=[])
        self.save(path)
        value['meeting']['body'] += 'Different content'
        path.write_text(json.dumps(value))
        self.save(path, ok=False)

    def test_save_rejects_unagreed_tasks_and_cross_domain(self):
        for tasks in ([dict(id='idea', space_id='alpha', title='Maybe launch', agreed=False)],
                      [dict(id='private', space_id='home', title='Copy work', agreed=True)]):
            path, _ = self.plan(tasks=tasks)
            self.save(path, ok=False)
            self.assertFalse((self.alpha / 'meetings').exists())

    def test_explicit_derived_note_update_requires_matching_base_hash(self):
        existing = self.alpha / 'overview.md'
        existing.write_text(document('current-overview', 'alpha', body='Old knowledge.').replace('"meeting"', '"overview"'))
        commit_all(self.root)
        path, value = self.plan(tasks=[])
        content = document('current-overview', 'alpha', body='Reusable current knowledge.').replace('"meeting"', '"note"')
        value['pages'] = [dict(space_id='alpha', path='overview.md', content=content,
                              base_sha256='0' * 64)]
        path.write_text(json.dumps(value))
        self.save(path, ok=False)
        self.assertIn('Old knowledge.', existing.read_text())
        value['pages'][0]['base_sha256'] = hashlib.sha256(existing.read_bytes()).hexdigest()
        path.write_text(json.dumps(value))
        self.save(path)
        self.assertIn('Reusable current knowledge.', existing.read_text())
        self.assertEqual(self.data('read', 'wiki:alpha:current-overview')['metadata']['type'], 'note')

    def test_subprocess_failure_does_not_claim_task_success(self):
        binary = self.base / 'failing-ruwana'
        binary.write_text('#!/bin/sh\necho "synthetic ruwana failure" >&2\nexit 23\n')
        binary.chmod(0o700)
        path, _ = self.plan()
        self.save(path, binary=binary, ok=False)
        journal = json.loads(next(self.state.rglob('operations/save-one.json')).read_text())
        self.assertEqual(journal['tasks'], {})
        self.assertEqual(journal['status'], 'incomplete')

    def test_duplicate_task_ids_and_unsafe_page_paths_reject_whole_plan(self):
        for pages, tasks in ([dict(space_id='alpha', path='../escape.md', content='bad')], []), ([], [
                dict(id='duplicate', space_id='alpha', title='one', agreed=True),
                dict(id='duplicate', space_id='alpha', title='two', agreed=True)]):
            path, value = self.plan(tasks=tasks)
            value['pages'] = pages
            path.write_text(json.dumps(value))
            self.save(path, ok=False)
            self.assertFalse((self.alpha / 'meetings').exists())

    def test_save_marks_captured_inbox_processed_only_after_success(self):
        source = self.external()
        self.cli('capture', '--space', 'alpha', '--id', 'pending-call', '--source', source)
        path, value = self.plan()
        value['capture_id'] = 'pending-call'
        raw = self.alpha / 'sources/captures/pending-call/original'
        value['source'] = dict(path=str(raw), sha256=hashlib.sha256(raw.read_bytes()).hexdigest())
        path.write_text(json.dumps(value))
        self.save(path, binary=self.base / 'missing', ok=False)
        self.assertEqual(self.data('read', 'wiki:alpha:pending-call')['metadata']['ingest_status'], 'pending')
        if RUWANA_AVAILABLE:
            self.save(path)
            self.assertEqual(self.data('read', 'wiki:alpha:pending-call')['metadata']['ingest_status'], 'processed')
            self.assertEqual(raw.read_bytes(), source.read_bytes())
            self.cli('capture', '--space', 'alpha', '--id', 'pending-call', '--source', source)
            self.assertEqual(self.data('read', 'wiki:alpha:pending-call')['metadata']['ingest_status'], 'processed')

    def test_plan_cross_domain_body_and_missing_evidence_rejected_before_write(self):
        for bad_body in ('See wiki:home:private-note.', 'See wiki:alpha:absent.'):
            path, value = self.plan(tasks=[])
            value['meeting']['body'] = bad_body
            path.write_text(json.dumps(value))
            self.save(path, ok=False)
            self.assertFalse((self.alpha / 'meetings').exists())

    def test_meeting_and_capture_ids_must_differ(self):
        path, value = self.plan(tasks=[])
        value['capture_id'] = value['meeting']['id']
        path.write_text(json.dumps(value))
        failed = self.save(path, ok=False)
        self.assertIn('must differ', failed.stderr)
        self.assertFalse((self.alpha / 'meetings').exists())

    def test_real_due_uses_input_calendar_day_at_local_end_of_day_and_retries(self):
        self.require_ruwana()
        self.env['TZ'] = 'Europe/Warsaw'
        cases = [('2026-10-10T12:00:00+02:00', '2026-10-10T23:59:59+02:00'),
                 ('2026-10-10T23:30:00Z', '2026-10-10T23:59:59+02:00'),
                 ('2026-12-10T12:00:00+02:00', '2026-12-10T23:59:59+01:00')]
        for index, (due, expected) in enumerate(cases):
            with self.subTest(due=due):
                ident = f'due-{index}'
                path, value = self.plan(ident, tasks=[dict(id='scheduled', space_id='alpha',
                                                         title='Scheduled task', agreed=True, due=due)])
                value['meeting']['id'] = ident
                path.write_text(json.dumps(value))
                result = json.loads(self.save(path).stdout)
                task = result['tasks']['scheduled']
                actual = json.loads(self.ruwana('show', task['id'], '--project', 'work/team/alpha', '--format', 'json'))
                self.assertEqual(actual['due'], expected)
                head = git(self.root, 'rev-parse', 'HEAD')
                self.save(path)
                self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
                self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_real_due_crash_after_add_before_verification_recovers_without_duplicates(self):
        self.require_ruwana()
        from pisar.operations import save
        from pisar.spaces import Wiki
        from pisar.ruwana import Ruwana
        path, _ = self.plan(tasks=[dict(id='scheduled', space_id='alpha', title='Scheduled task', agreed=True,
                                       due='2026-10-10T23:30:00Z')])
        original = Ruwana.run

        def crash_after_add(adapter, *args):
            output = original(adapter, *args)
            if args[0] == 'add':
                raise RuntimeError('synthetic crash after real add before verification/checkpoint')
            return output

        self.env['TZ'] = 'Europe/Warsaw'
        self.addCleanup(time.tzset)
        with patch.dict(os.environ, {'TZ': 'Europe/Warsaw'}):
            time.tzset()
            with patch.object(Ruwana, 'run', crash_after_add):
                with self.assertRaises(RuntimeError):
                    save(Wiki(self.root), self.state, path, str(RUWANA))
        time.tzset()
        self.save(path)
        tasks = json.loads(self.ruwana('list', '--project', 'work/team/alpha', '--all', '--format', 'json'))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['due'], '2026-10-10T23:59:59+02:00')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_real_due_changed_day_is_still_a_conflict(self):
        self.require_ruwana()
        self.env['TZ'] = 'UTC'
        path, _ = self.plan(tasks=[dict(id='scheduled', space_id='alpha', title='Scheduled task', agreed=True,
                                       due='2026-10-10T12:00:00+02:00')])
        result = json.loads(self.save(path).stdout)
        task_id = result['tasks']['scheduled']['id']
        self.ruwana('edit', task_id, '--project', 'work/team/alpha', '--due=2026-10-11')
        commit_all(self.root)
        task_path = self.root / result['tasks']['scheduled']['path']
        before = task_path.read_bytes()
        self.assertIn('due conflict', self.save(path, ok=False).stderr)
        self.assertEqual(task_path.read_bytes(), before)

    def test_real_dash_title_and_markdown_description_are_literal_arguments(self):
        self.require_ruwana()
        tasks = [dict(id='dash-title', space_id='alpha', title='-1 day slip', agreed=True,
                      description='- item one\n- item two'),
                 dict(id='option-title', space_id='alpha', title='--help', agreed=True,
                      description='--source=literal text')]
        path, _ = self.plan(tasks=tasks)
        result = json.loads(self.save(path).stdout)
        for task in tasks:
            actual = json.loads(self.ruwana('show', result['tasks'][task['id']]['id'],
                                            '--project', 'work/team/alpha', '--format', 'json'))
            self.assertEqual(actual['title'], task['title'])
            self.assertEqual(actual['description'], task['description'])
        self.save(path)
        self.assertEqual(len(json.loads(self.ruwana('list', '--project', 'work/team/alpha', '--all', '--format', 'json'))), 2)

    def test_real_task_text_normalization_and_replay_preserve_nonempty_description(self):
        self.require_ruwana()
        cases = [
            ('empty', 'Empty description', '', 'Empty description', None),
            ('blank', 'Blank description', ' \t\n\u00a0\u2003 ', 'Blank description', None),
            ('title', ' \tPrepare  draft \n', None, 'Prepare  draft', None),
            ('markdown', '\u2003Review draft\u00a0', ' \t\n  - Keep spacing.  \n ',
             'Review draft', ' \t\n  - Keep spacing.  \n '),
            # Rust's Unicode White_Space excludes these Python strip characters.
            ('control', '\x1cKeep separators\x1c', '\x1c', '\x1cKeep separators\x1c', '\x1c'),
        ]
        tasks = [dict(id=ident, space_id='alpha', title=title, agreed=True,
                      **({} if description is None else dict(description=description)))
                 for ident, title, description, _, _ in cases]
        path, _ = self.plan(tasks=tasks)
        plan_bytes = path.read_bytes()
        result = json.loads(self.save(path).stdout)
        for ident, _, _, title, description in cases:
            with self.subTest(task=ident):
                actual = json.loads(self.ruwana('show', result['tasks'][ident]['id'],
                                                '--project', 'work/team/alpha', '--format', 'json'))
                self.assertEqual(actual['title'], title)
                self.assertEqual(actual['description'], description)
        head = git(self.root, 'rev-parse', 'HEAD')
        journal = next(self.state.rglob('operations/save-one.json'))
        historical = journal.read_bytes()
        self.save(path)
        self.assertEqual(journal.read_bytes(), historical)
        self.assertEqual(path.read_bytes(), plan_bytes)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(len(json.loads(self.ruwana('list', '--project', 'work/team/alpha',
                                                  '--all', '--format', 'json'))), len(cases))

    def test_real_normalized_text_crash_after_add_recovers_same_task_and_retries(self):
        self.require_ruwana()
        from pisar.operations import save
        from pisar.spaces import Wiki
        from pisar.ruwana import Ruwana
        original = Ruwana.run

        def crash_after_add(adapter, *args):
            output = original(adapter, *args)
            if args[0] == 'add':
                raise RuntimeError('synthetic crash after real add before verification/checkpoint')
            return output

        cases = [('empty', 'Prepare draft', '', 'Prepare draft', None),
                 ('blank', 'Prepare draft', ' \t\n\u2003 ', 'Prepare draft', None),
                 ('title', ' \tPrepare  draft \n', None, 'Prepare  draft', None),
                 ('markdown', ' Review draft ', ' \n- Keep spacing. \t',
                  'Review draft', ' \n- Keep spacing. \t')]
        for ident, title, description, expected_title, expected_description in cases:
            with self.subTest(task=ident):
                operation = 'text-crash-' + ident
                task = dict(id=ident, space_id='alpha', title=title, agreed=True)
                if description is not None:
                    task['description'] = description
                path, value = self.plan(operation, tasks=[task])
                value['meeting']['id'] = operation
                path.write_text(json.dumps(value))
                plan_bytes = path.read_bytes()
                with patch.object(Ruwana, 'run', crash_after_add):
                    with self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
                        save(Wiki(self.root), self.state, path, str(RUWANA))
                records = json.loads(self.ruwana('list', '--project', 'work/team/alpha',
                                                '--all', '--format', 'json'))
                marker = f'wiki:alpha:{operation}#task-{ident}'
                created = [record for record in records if marker in record['source']]
                self.assertEqual(len(created), 1)
                created_id = created[0]['id']
                actual = json.loads(self.ruwana('show', created_id, '--project', 'work/team/alpha',
                                                '--format', 'json'))
                self.assertEqual(actual['title'], expected_title)
                self.assertEqual(actual['description'], expected_description)
                result = json.loads(self.save(path).stdout)
                self.assertEqual(result['tasks'][ident]['id'], created_id)
                self.assertEqual(result['status'], 'complete')
                head = git(self.root, 'rev-parse', 'HEAD')
                self.save(path)
                self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
                self.assertEqual(path.read_bytes(), plan_bytes)
                self.assertEqual(git(self.root, 'status', '--porcelain'), '')
                records = json.loads(self.ruwana('list', '--project', 'work/team/alpha',
                                                '--all', '--format', 'json'))
                self.assertEqual(len([record for record in records if marker in record['source']]), 1)

    def test_real_nonempty_description_edge_whitespace_change_is_content_conflict(self):
        self.require_ruwana()
        path, _ = self.plan(tasks=[dict(id='spacing', space_id='alpha', title='Review draft', agreed=True,
                                       description=' \n- Keep spacing. \t')])
        result = json.loads(self.save(path).stdout)
        task_id = result['tasks']['spacing']['id']
        self.ruwana('edit', task_id, '--project', 'work/team/alpha', '--description=- Keep spacing.')
        commit_all(self.root)
        task_path = self.root / result['tasks']['spacing']['path']
        before = task_path.read_bytes()
        self.assertIn('content conflict', self.save(path, ok=False).stderr)
        self.assertEqual(task_path.read_bytes(), before)

    def test_completed_replay_preserves_history_through_dirty_done_and_later_meeting_edit(self):
        self.require_ruwana()
        path, _ = self.plan(tasks=[dict(id='task-one', space_id='alpha', title='Agreed task', agreed=True)])
        result = json.loads(self.save(path).stdout)
        journal = next(self.state.rglob('operations/save-one.json'))
        historical = journal.read_bytes()
        task_id = result['tasks']['task-one']['id']
        task_path = self.root / result['tasks']['task-one']['path']
        self.ruwana('done', task_id, '--project', 'work/team/alpha')
        done_bytes = task_path.read_bytes()
        self.save(path, ok=False)
        self.assertEqual(journal.read_bytes(), historical)
        self.assertEqual(task_path.read_bytes(), done_bytes)
        commit_all(self.root)
        head = git(self.root, 'rev-parse', 'HEAD')
        self.save(path)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(journal.read_bytes(), historical)
        meeting = self.alpha / 'meetings/launch-meeting.md'
        meeting.write_text(meeting.read_text() + '\nLater legitimate correction.\n')
        commit_all(self.root)
        edited = meeting.read_bytes()
        self.save(path, ok=False)
        self.assertEqual(meeting.read_bytes(), edited)
        self.assertEqual(journal.read_bytes(), historical)
        self.assertEqual(json.loads(journal.read_text())['status'], 'complete')

    def test_save_rejects_in_root_cross_domain_sources_before_destination_writes(self):
        path, value = self.plan(tasks=[])
        head = git(self.root, 'rev-parse', 'HEAD')
        for source, target in ((self.personal / 'note.md', 'alpha'), (self.alpha / 'call.md', 'home')):
            with self.subTest(target=target):
                value['space_id'] = target
                value['meeting']['related_space_ids'] = []
                value['source'] = dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest())
                path.write_text(json.dumps(value))
                self.assertIn('source scope', self.save(path, ok=False).stderr)
                self.assertFalse((self.alpha / 'meetings').exists())
                self.assertFalse((self.personal / 'meetings').exists())
                self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_save_allows_existing_same_domain_source(self):
        path, value = self.plan(tasks=[])
        source = self.beta / 'transcript.txt'
        source.write_text('Synthetic same-domain source')
        commit_all(self.root)
        value['source'] = dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        path.write_text(json.dumps(value))
        self.save(path)
        self.assertEqual((self.alpha / 'sources/meetings/launch-meeting/transcript.md').read_bytes(), source.read_bytes())
