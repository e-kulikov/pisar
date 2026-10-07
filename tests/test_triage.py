import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch
from .support import Fixture, RUWANA, git, init_repo, space, commit_all
from .test_save import BODY


class TriageTests(Fixture):
    def routing(self, items=None):
        source = self.external()
        default = dict(id='routed-call', source=str(source),
                       sha256=hashlib.sha256(source.read_bytes()).hexdigest(), space_id='work/alpha')
        plan = self.base / 'routing.json'
        plan.write_text(json.dumps(dict(schema_version=1, items=items if items is not None else [default])))
        return plan, default

    def prepare(self, plan, batch='morning-one'):
        return self.data('triage', 'prepare', '--batch', batch, '--plan', plan)

    def action(self, action, ident='routed-call', batch='morning-one', *args, **kwargs):
        return self.data('triage', action, '--batch', batch, '--item', ident, *args, **kwargs)

    def test_prepare_and_report_keep_original_and_all_drafts_outside_destination(self):
        plan, item = self.routing()
        head = git(self.root, 'rev-parse', 'HEAD')
        before = {str(p.relative_to(self.root)) for p in self.root.rglob('*') if '.git' not in p.parts}
        prepared = self.prepare(plan)
        after = {str(p.relative_to(self.root)) for p in self.root.rglob('*') if '.git' not in p.parts}
        self.assertEqual(after, before)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertTrue(Path(item['source']).exists())
        report = self.data('triage', 'report', '--batch', 'morning-one')
        self.assertEqual(report['items'][0]['status'], 'pending')
        self.assertEqual(report['items'][0]['space_id'], 'work/alpha')
        batch_dir = Path(prepared['directory'])
        self.assertTrue(batch_dir.is_relative_to(self.state))
        self.assertTrue((batch_dir / 'originals/routed-call').is_file())
        self.assertTrue((batch_dir / 'drafts/routed-call.json').is_file())
        self.assertTrue((batch_dir / 'report.md').is_file())
        self.prepare(plan)  # stable snapshot, no duplicate batch

    def test_accept_commits_once_and_records_verified_processed_original(self):
        plan, item = self.routing()
        self.prepare(plan)
        accepted = self.action('accept')
        self.assertEqual(accepted['item']['status'], 'complete')
        self.assertEqual(Path(accepted['item']['processed_path']).read_bytes(), Path(item['source']).read_bytes())
        self.assertTrue(Path(item['source']).exists())  # explicit external inputs are never removed
        self.assertEqual(self.data('read', 'wiki:work/alpha:routed-call')['metadata']['ingest_status'], 'pending')
        head = git(self.root, 'rev-parse', 'HEAD')
        self.action('accept')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_changed_original_or_draft_hash_refuses_accept_before_destination(self):
        for change in ('source', 'draft'):
            plan, item = self.routing()
            batch = 'changed-' + change
            prepared = self.prepare(plan, batch)
            if change == 'source':
                Path(item['source']).write_text('changed after prepare')
            else:
                (Path(prepared['directory']) / 'drafts/routed-call.json').write_text('{}')
            self.cli('triage', 'accept', '--batch', batch, '--item', 'routed-call', ok=False)
            self.assertFalse((self.alpha / 'inbox/routed-call.md').exists())

    def test_reroute_and_defer_do_not_apply_and_accept_uses_new_owner(self):
        plan, _ = self.routing()
        self.prepare(plan)
        self.action('defer')
        self.cli('triage', 'accept', '--batch', 'morning-one', '--item', 'routed-call', ok=False)
        self.action('reroute', 'routed-call', 'morning-one', '--space', 'work/beta')
        self.assertFalse((self.alpha / 'inbox').exists())
        self.assertFalse((self.beta / 'inbox').exists())
        self.action('accept')
        self.assertTrue((self.beta / 'inbox/routed-call.md').is_file())
        self.assertFalse((self.alpha / 'inbox/routed-call.md').exists())

    def test_unknown_destination_stays_pending_until_explicit_reroute(self):
        plan, item = self.routing()
        item['space_id'] = None
        plan.write_text(json.dumps(dict(schema_version=1, items=[item])))
        self.prepare(plan)
        self.cli('triage', 'accept', '--batch', 'morning-one', '--item', 'routed-call', ok=False)
        self.action('reroute', 'routed-call', 'morning-one', '--space', 'work/alpha')
        self.action('accept')

    def test_partial_accept_is_recoverable_without_duplicate_import(self):
        from pisar.triage import accept
        from pisar.spaces import Wiki
        from pisar.safety import WikiError
        plan, _ = self.routing()
        self.prepare(plan)
        with patch('pisar.gitops.commit', side_effect=WikiError('synthetic Git failure')):
            with self.assertRaises(WikiError):
                accept(Wiki(self.root), self.state, 'morning-one', 'routed-call', str(RUWANA))
        report = self.data('triage', 'report', '--batch', 'morning-one')
        self.assertEqual(report['items'][0]['status'], 'incomplete')
        for action, flags in (('defer', []), ('reroute', ['--space', 'work/beta'])):
            failed = self.cli('triage', action, '--batch', 'morning-one', '--item', 'routed-call', *flags, ok=False)
            self.assertIn('incomplete', failed.stderr)
            self.assertIn('retry accept', failed.stderr)
        self.action('accept')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(len(list(self.alpha.rglob('routed-call.md'))), 1)

    def test_default_global_inbox_snapshot_excludes_late_files_and_archives_original(self):
        inbox = self.state / 'inbox'
        inbox.mkdir(parents=True)
        source = inbox / 'early.txt'
        source.write_text('Early synthetic original')
        prepared = self.data('triage', 'prepare', '--batch', 'auto-one')
        late = inbox / 'late.txt'
        late.write_text('Late input for next batch')
        report = self.data('triage', 'report', '--batch', 'auto-one')
        self.assertEqual(len(report['items']), 1)
        ident = report['items'][0]['id']
        self.action('reroute', ident, 'auto-one', '--space', 'work/alpha')
        accepted = self.action('accept', ident, 'auto-one')
        self.assertEqual(Path(accepted['item']['processed_path']).read_text(), 'Early synthetic original')
        self.assertFalse(source.exists())
        self.assertTrue(late.exists())
        self.data('triage', 'prepare', '--batch', 'auto-two')
        self.assertEqual(len(self.data('triage', 'report', '--batch', 'auto-two')['items']), 1)
        self.action('accept', ident, 'auto-one')  # original already archived

    def test_runtime_inbox_and_namespace_boundaries(self):
        self.cli('triage', 'prepare', '--batch', '../escape', ok=False)
        self.cli('triage', 'prepare', '--batch', 'inside', '--inbox', self.alpha, ok=False)
        plan, _ = self.routing()
        self.prepare(plan)
        other = self.base / 'other-root'
        init_repo(other)
        space(other, 'work/alpha', 'alpha')
        commit_all(other)
        self.cli('triage', 'report', '--batch', 'morning-one', root=other, ok=False)

    def test_prepared_meeting_plan_applies_only_after_accept(self):
        plan, item = self.routing()
        item['plan'] = dict(schema_version=1, operation_id='triaged-meeting', space_id='work/alpha',
                            source=dict(path=item['source'], sha256=item['sha256']),
                            meeting=dict(id='triaged-call', title='Triaged meeting', body=BODY),
                            tasks=[], pages=[])
        plan.write_text(json.dumps(dict(schema_version=1, items=[item])))
        self.prepare(plan)
        self.assertFalse((self.alpha / 'meetings').exists())
        self.action('accept')
        self.assertTrue((self.alpha / 'meetings/triaged-call.md').is_file())
        self.assertEqual(self.data('read', 'wiki:work/alpha:routed-call')['metadata']['ingest_status'], 'processed')

    def test_duplicate_ids_and_snapshot_source_symlinks_rejected(self):
        plan, item = self.routing()
        plan.write_text(json.dumps(dict(schema_version=1, items=[item, item])))
        self.cli('triage', 'prepare', '--batch', 'duplicates', '--plan', plan, ok=False)
        link = self.base / 'linked-source'
        link.symlink_to(item['source'])
        item['source'] = str(link)
        plan.write_text(json.dumps(dict(schema_version=1, items=[item])))
        self.cli('triage', 'prepare', '--batch', 'symlink', '--plan', plan, ok=False)

    def test_duplicate_source_identity_and_identical_bytes_are_not_imported_twice(self):
        plan, item = self.routing()
        for second in ({**item, 'id': 'different-id'},
                       {**item, 'id': 'different-copy', 'source': str(self.base / 'copy.txt')}):
            Path(second['source']).write_bytes(Path(item['source']).read_bytes())
            plan.write_text(json.dumps(dict(schema_version=1, items=[item, second])))
            self.cli('triage', 'prepare', '--batch', second['id'], '--plan', plan, ok=False)
        self.assertFalse((self.alpha / 'inbox').exists())

    def test_crash_after_global_original_unlink_before_complete_manifest_recovers(self):
        from pisar import triage
        from pisar.spaces import Wiki
        inbox = self.state / 'inbox'
        inbox.mkdir(parents=True)
        original = inbox / 'crash.txt'
        original.write_text('Preserve through archiving crash')
        self.data('triage', 'prepare', '--batch', 'archive-crash')
        ident = self.data('triage', 'report', '--batch', 'archive-crash')['items'][0]['id']
        self.action('reroute', ident, 'archive-crash', '--space', 'work/alpha')
        persist = triage.persist

        def crash_before_complete(directory, manifest):
            if manifest['items'][0]['status'] == 'complete':
                raise RuntimeError('synthetic crash after source unlink before manifest persist')
            return persist(directory, manifest)

        with patch.object(triage, 'persist', crash_before_complete):
            with self.assertRaises(RuntimeError):
                triage.accept(Wiki(self.root), self.state, 'archive-crash', ident, str(RUWANA))
        self.assertFalse(original.exists())
        head = git(self.root, 'rev-parse', 'HEAD')
        accepted = self.action('accept', ident, 'archive-crash')
        self.assertEqual(accepted['item']['status'], 'complete')
        self.assertEqual(Path(accepted['item']['processed_path']).read_text(), 'Preserve through archiving crash')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_triage_retry_after_completed_save_preserves_history_and_dirty_task(self):
        self.require_ruwana()
        from pisar import triage
        from pisar.spaces import Wiki
        from pisar.safety import WikiError
        routing, item = self.routing()
        item['plan'] = dict(schema_version=1, operation_id='triaged-save', space_id='work/alpha',
                            source=dict(path=item['source'], sha256=item['sha256']),
                            meeting=dict(id='triaged-call', title='Triaged call', body=BODY),
                            tasks=[dict(id='draft', space_id='work/alpha', title='Prepare draft', agreed=True)])
        routing.write_text(json.dumps(dict(schema_version=1, items=[item])))
        self.prepare(routing)
        original_write = triage.atomic_bytes

        def fail_processed_write(path, data):
            if 'processed' in path.parts:
                raise OSError('synthetic failure after completed save before archive')
            return original_write(path, data)

        with patch.object(triage, 'atomic_bytes', fail_processed_write):
            with self.assertRaises(WikiError):
                triage.accept(Wiki(self.root), self.state, 'morning-one', 'routed-call', str(RUWANA))
        journal = next(self.state.rglob('operations/triaged-save.json'))
        history = journal.read_bytes()
        self.assertEqual(json.loads(history)['status'], 'complete')
        task = json.loads(history)['tasks']['draft']
        task_path = self.root / task['path']
        p = subprocess.run([str(RUWANA), 'done', task['id'], '--project', task['project']],
                           env={**self.env, 'WIKI_ROOT': str(self.root)}, text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        changed = task_path.read_bytes()
        self.cli('--ruwana', RUWANA, 'triage', 'accept', '--batch', 'morning-one', '--item', 'routed-call', ok=False)
        self.assertEqual(journal.read_bytes(), history)
        self.assertEqual(task_path.read_bytes(), changed)
        commit_all(self.root)
        head = git(self.root, 'rev-parse', 'HEAD')
        self.cli('--ruwana', RUWANA, 'triage', 'accept', '--batch', 'morning-one', '--item', 'routed-call')
        self.assertEqual(journal.read_bytes(), history)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(len(list(self.alpha.glob('.ruwana/*.toml'))), 1)

    def test_items_route_by_address_when_domains_reuse_an_id(self):
        acme = space(self.root, 'acme/10-projects/alpha', 'alpha')
        commit_all(self.root)
        plan, item = self.routing()
        item['space_id'] = 'alpha'
        plan.write_text(json.dumps(dict(schema_version=1, items=[item])))
        self.cli('triage', 'prepare', '--batch', 'bare-id', '--plan', plan, ok=False)
        item['space_id'] = 'acme/alpha'
        plan.write_text(json.dumps(dict(schema_version=1, items=[item])))
        self.prepare(plan)
        self.cli('triage', 'reroute', '--batch', 'morning-one', '--item', 'routed-call', '--space', 'beta', ok=False)
        self.action('accept')
        self.assertTrue((acme / 'inbox/routed-call.md').is_file())
        self.assertFalse((self.alpha / 'inbox').exists())
        self.assertEqual(self.data('read', 'wiki:acme/alpha:routed-call')['metadata']['space_ids'], ['acme/alpha'])
