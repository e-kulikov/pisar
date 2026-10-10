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

    def inbox_item(self, batch):
        inbox = self.state / 'inbox'
        inbox.mkdir(parents=True, exist_ok=True)
        source = inbox / f'{batch}.txt'
        source.write_text(f'Synthetic original for {batch}')
        self.data('triage', 'prepare', '--batch', batch)
        ident = self.data('triage', 'report', '--batch', batch)['items'][0]['id']
        return source, ident

    def wiki_files(self):
        return {str(p.relative_to(self.root)) for p in self.root.rglob('*') if '.git' not in p.parts}

    def test_reject_removes_inbox_file_and_keeps_byte_identical_processed_copy(self):
        source, ident = self.inbox_item('reject-inbox')
        head = git(self.root, 'rev-parse', 'HEAD')
        files = self.wiki_files()
        rejected = self.data('triage', 'reject', '--batch', 'reject-inbox', '--item', ident)
        self.assertEqual(rejected['item']['status'], 'rejected')
        self.assertFalse(source.exists())
        processed = Path(rejected['item']['processed_path'])
        self.assertEqual(processed.read_bytes(), b'Synthetic original for reject-inbox')
        self.assertEqual(sorted(p.name for p in processed.parent.iterdir()), ['original'])
        self.assertEqual(self.wiki_files(), files)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        report = self.data('triage', 'report', '--batch', 'reject-inbox')
        self.assertEqual(report['items'][0]['status'], 'rejected')
        self.assertIn('rejected', (Path(report['directory']) / 'report.md').read_text())

    def test_reject_of_external_source_marks_rejected_and_never_deletes_it(self):
        plan, item = self.routing()
        self.prepare(plan)
        result = self.action('reject', 'routed-call', 'morning-one', '--reason', 'Not for the wiki')
        self.assertEqual(result['item']['status'], 'rejected')
        self.assertTrue(Path(item['source']).exists())
        self.assertEqual(Path(result['item']['processed_path']).read_bytes(), Path(item['source']).read_bytes())
        self.assertIn('not deleted', result['note'])
        report = (Path(self.data('triage', 'report', '--batch', 'morning-one')['directory']) / 'report.md').read_text()
        self.assertIn('not deleted', report)
        self.assertIn('Not for the wiki', report)

    def test_interrupted_reject_after_copy_before_unlink_retries_without_duplicates(self):
        from pisar import triage
        from pisar.spaces import Wiki
        from pisar.safety import WikiError
        source, ident = self.inbox_item('reject-crash')
        with patch.object(Path, 'unlink', side_effect=OSError('synthetic crash before unlink')):
            with self.assertRaises(WikiError):
                triage.reject(Wiki(self.root), self.state, 'reject-crash', ident)
        self.assertTrue(source.exists())
        self.assertEqual(self.data('triage', 'report', '--batch', 'reject-crash')['items'][0]['status'], 'rejecting')
        report = Path(self.data('triage', 'report', '--batch', 'reject-crash')['directory']) / 'report.md'
        self.assertNotIn('removed', report.read_text())
        self.assertIn('reject in progress', report.read_text())
        for action, flags in (('accept', []), ('reroute', ['--space', 'work/alpha']), ('defer', [])):
            failed = self.cli('triage', action, '--batch', 'reject-crash', '--item', ident, *flags, ok=False)
            self.assertIn('rejected', failed.stderr)
        self.assertEqual(self.data('triage', 'report', '--batch', 'reject-crash')['items'][0]['status'], 'rejecting')
        retried = self.data('triage', 'reject', '--batch', 'reject-crash', '--item', ident)
        self.assertEqual(retried['item']['status'], 'rejected')
        self.assertFalse(source.exists())
        self.assertIn('removed', report.read_text())
        processed = Path(retried['item']['processed_path'])
        self.assertEqual(sorted(p.name for p in processed.parent.iterdir()), ['original'])
        self.assertEqual(processed.read_bytes(), b'Synthetic original for reject-crash')

    def test_interrupted_reject_after_unlink_before_status_retries_and_completed_reject_is_idempotent(self):
        from pisar import triage
        from pisar.spaces import Wiki
        source, ident = self.inbox_item('reject-late')
        persist = triage.persist

        def crash_before_rejected(directory, manifest):
            if manifest['items'][0]['status'] == 'rejected':
                raise RuntimeError('synthetic crash after unlink before status persist')
            return persist(directory, manifest)

        with patch.object(triage, 'persist', crash_before_rejected):
            with self.assertRaises(RuntimeError):
                triage.reject(Wiki(self.root), self.state, 'reject-late', ident)
        self.assertFalse(source.exists())
        first = self.data('triage', 'reject', '--batch', 'reject-late', '--item', ident)
        self.assertEqual(first['item']['status'], 'rejected')
        again = self.data('triage', 'reject', '--batch', 'reject-late', '--item', ident)
        self.assertEqual(again, first)
        self.assertEqual(sorted(p.name for p in Path(first['item']['processed_path']).parent.iterdir()), ['original'])

    def test_retry_reason_must_match_stored_reason_and_missing_reason_keeps_it(self):
        from pisar import triage
        from pisar.spaces import Wiki
        source, ident = self.inbox_item('reject-reason-retry')
        persist = triage.persist

        def crash_before_rejected(directory, manifest):
            if manifest['items'][0]['status'] == 'rejected':
                raise RuntimeError('synthetic crash before rejected status')
            return persist(directory, manifest)

        with patch.object(triage, 'persist', crash_before_rejected):
            with self.assertRaises(RuntimeError):
                triage.reject(Wiki(self.root), self.state, 'reject-reason-retry', ident, 'first note')
        failed = self.cli('triage', 'reject', '--batch', 'reject-reason-retry', '--item', ident,
                          '--reason', 'second note', ok=False)
        self.assertIn('different reason', failed.stderr)
        retried = self.data('triage', 'reject', '--batch', 'reject-reason-retry', '--item', ident)
        self.assertEqual(retried['item']['reason'], 'first note')
        self.assertEqual(retried['item']['status'], 'rejected')
        self.cli('triage', 'reject', '--batch', 'reject-reason-retry', '--item', ident,
                 '--reason', 'second note', ok=False)
        self.assertEqual(self.data('triage', 'reject', '--batch', 'reject-reason-retry', '--item', ident,
                                   '--reason', 'first note')['item']['reason'], 'first note')

    def test_completed_reject_retry_verifies_processed_copy_and_fails_when_it_changed(self):
        source, ident = self.inbox_item('reject-tampered')
        first = self.data('triage', 'reject', '--batch', 'reject-tampered', '--item', ident)
        Path(first['item']['processed_path']).write_text('tampered processed copy')
        failed = self.cli('triage', 'reject', '--batch', 'reject-tampered', '--item', ident, ok=False)
        self.assertIn('hash conflict', failed.stderr)
        self.assertEqual(Path(first['item']['processed_path']).read_text(), 'tampered processed copy')

    def test_reject_refuses_inbox_symlink_and_removes_nothing(self):
        source, ident = self.inbox_item('reject-symlink')
        target = self.base / 'symlink-target.txt'
        target.write_text('Synthetic original for reject-symlink')
        source.unlink()
        source.symlink_to(target)
        failed = self.cli('triage', 'reject', '--batch', 'reject-symlink', '--item', ident, ok=False)
        self.assertIn('symlink', failed.stderr)
        self.assertTrue(source.is_symlink())
        self.assertEqual(target.read_text(), 'Synthetic original for reject-symlink')
        self.assertFalse((self.state / 'processed/reject-symlink').exists())

    def test_reject_leaves_uncommitted_user_edit_byte_identical(self):
        plan, item = self.routing()
        self.prepare(plan)
        draft = self.alpha / 'call.md'
        draft.write_text('Uncommitted user edit that must survive')
        status = git(self.root, 'status', '--porcelain')
        self.action('reject')
        self.assertEqual(draft.read_text(), 'Uncommitted user edit that must survive')
        self.assertEqual(git(self.root, 'status', '--porcelain'), status)

    def test_reject_refused_when_source_bytes_changed_and_removes_nothing(self):
        source, ident = self.inbox_item('reject-changed')
        source.write_text('changed after prepare')
        failed = self.cli('triage', 'reject', '--batch', 'reject-changed', '--item', ident, ok=False)
        self.assertIn('hash conflict', failed.stderr)
        self.assertEqual(source.read_text(), 'changed after prepare')
        self.assertFalse((self.state / 'processed/reject-changed').exists())
        self.assertEqual(self.data('triage', 'report', '--batch', 'reject-changed')['items'][0]['status'], 'pending')

    def test_reject_refused_from_applying_incomplete_and_complete(self):
        for status in ('applying', 'incomplete', 'complete'):
            batch = f'reject-{status}'
            source, ident = self.inbox_item(batch)
            manifest_path = next(self.state.rglob(f'triage/{batch}/manifest.json'))
            manifest = json.loads(manifest_path.read_text())
            manifest['items'][0]['status'] = status
            manifest_path.write_text(json.dumps(manifest))
            failed = self.cli('triage', 'reject', '--batch', batch, '--item', ident, ok=False)
            self.assertIn('cannot reject', failed.stderr)
            self.assertTrue(source.exists())
            self.assertEqual(json.loads(manifest_path.read_text())['items'][0]['status'], status)

    def test_reject_works_from_deferred_and_then_blocks_accept_reroute_and_defer(self):
        source, ident = self.inbox_item('reject-deferred')
        self.data('triage', 'defer', '--batch', 'reject-deferred', '--item', ident)
        self.data('triage', 'reject', '--batch', 'reject-deferred', '--item', ident)
        self.assertFalse(source.exists())
        for action, flags in (('accept', []), ('reroute', ['--space', 'work/alpha']), ('defer', [])):
            failed = self.cli('triage', action, '--batch', 'reject-deferred', '--item', ident, *flags, ok=False)
            self.assertIn('rejected', failed.stderr)
        self.assertEqual(self.data('triage', 'report', '--batch', 'reject-deferred')['items'][0]['status'], 'rejected')

    def test_accept_after_reject_is_refused_for_external_source(self):
        plan, item = self.routing()
        self.prepare(plan)
        self.action('reject')
        self.cli('triage', 'accept', '--batch', 'morning-one', '--item', 'routed-call', ok=False)
        self.assertFalse((self.alpha / 'inbox/routed-call.md').exists())
        self.assertTrue(Path(item['source']).exists())

    def test_reject_is_terminal_inside_its_batch_and_same_content_prepares_in_a_new_batch(self):
        plan, item = self.routing()
        self.prepare(plan)
        self.action('reject')
        # Re-running the prepared batch returns the same snapshot, with the item still rejected.
        self.assertEqual(self.prepare(plan)['status'], 'prepared')
        self.assertEqual(self.data('triage', 'report', '--batch', 'morning-one')['items'][0]['status'], 'rejected')
        # A new batch is a new decision: the same content is prepared normally again.
        second = self.base / 'routing-again.json'
        second.write_text(plan.read_text())
        self.prepare(second, 'next-morning')
        self.assertEqual(self.data('triage', 'report', '--batch', 'next-morning')['items'][0]['status'], 'pending')

    def test_same_content_dropped_again_into_the_inbox_is_prepared_in_a_new_batch(self):
        source, ident = self.inbox_item('reject-again')
        self.data('triage', 'reject', '--batch', 'reject-again', '--item', ident)
        self.assertFalse(source.exists())
        source.write_text(f'Synthetic original for reject-again')
        self.data('triage', 'prepare', '--batch', 'reject-again-two')
        self.assertEqual(self.data('triage', 'report', '--batch', 'reject-again-two')['items'][0]['status'], 'pending')

    def test_reason_is_trimmed_bounded_stored_in_report_and_never_written_to_wiki(self):
        _, ident = self.inbox_item('reject-reason')
        self.data('triage', 'reject', '--batch', 'reject-reason', '--item', ident, '--reason', '  Duplicate of the call  ')
        report = self.data('triage', 'report', '--batch', 'reject-reason')
        self.assertEqual(report['items'][0]['reason'], 'Duplicate of the call')
        self.assertIn('Duplicate of the call', (Path(report['directory']) / 'report.md').read_text())
        self.assertFalse(any('Duplicate of the call' in p.read_text(errors='ignore')
                             for p in self.root.rglob('*') if p.is_file() and '.git' not in p.parts))
        _, empty = self.inbox_item('reject-empty')
        self.data('triage', 'reject', '--batch', 'reject-empty', '--item', empty, '--reason', '   ')
        self.assertIsNone(self.data('triage', 'report', '--batch', 'reject-empty')['items'][0]['reason'])
        _, long = self.inbox_item('reject-long')
        self.cli('triage', 'reject', '--batch', 'reject-long', '--item', long, '--reason', 'x' * 501, ok=False)
        self.cli('triage', 'reject', '--batch', 'reject-long', '--item', long, '--reason', 'two\nlines', ok=False)
        self.assertEqual(self.data('triage', 'report', '--batch', 'reject-long')['items'][0]['status'], 'pending')

    def test_reject_cli_wiring_and_help(self):
        help_text = self.run_pisar('triage', 'reject', '--help').stdout
        self.assertIn('--batch', help_text)
        self.assertIn('--item', help_text)
        self.assertIn('--reason', help_text)
        self.assertIn('reject', self.run_pisar('triage', '--help').stdout)
