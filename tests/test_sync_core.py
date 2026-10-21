"""Real-Git regression coverage for the delivery-I candidate primitives."""
import subprocess
import tempfile
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest import mock

from .support import clean_environ, init_repo, commit_all, domain, space, document
from pisar import operations, settings, sync, sync_git, sync_review
from pisar.safety import WikiError
from pisar.spaces import Wiki


class SyncGitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pisar-sync-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'repo'
        init_repo(self.repo)
        (self.repo / 'base.md').write_text('base\n')
        commit_all(self.repo)

    def test_candidate_ff_never_changes_live_head(self):
        start = sync_git.observe(self.repo)
        branch = subprocess.run(['git', '-C', str(self.repo), 'branch', 'incoming'],
                                 env=clean_environ(), capture_output=True)
        self.assertEqual(branch.returncode, 0)
        subprocess.run(['git', '-C', str(self.repo), 'switch', 'incoming'],
                       env=clean_environ(), check=True, capture_output=True)
        (self.repo / 'new.md').write_text('incoming\n')
        commit_all(self.repo)
        incoming = subprocess.run(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'],
                                  env=clean_environ(), text=True, capture_output=True, check=True).stdout.strip()
        subprocess.run(['git', '-C', str(self.repo), 'switch', start.branch.removeprefix('refs/heads/')],
                       env=clean_environ(), check=True, capture_output=True)
        candidate = self.base / 'candidate'
        sync_git.prepare_candidate(self.repo, candidate, start.head, incoming)
        self.assertEqual(sync_git.head(self.repo), start.head)
        self.assertEqual(sync_git.head(candidate), incoming)
        self.assertFalse((self.repo / 'new.md').exists())

    def test_native_merge_preserves_two_parents(self):
        base_oid = sync_git.head(self.repo)
        subprocess.run(['git', '-C', str(self.repo), 'switch', '-c', 'theirs'],
                       env=clean_environ(), check=True, capture_output=True)
        (self.repo / 'theirs.md').write_text('theirs\n')
        commit_all(self.repo)
        theirs = sync_git.head(self.repo)
        subprocess.run(['git', '-C', str(self.repo), 'switch', '-c', 'ours', base_oid],
                       env=clean_environ(), check=True, capture_output=True)
        (self.repo / 'ours.md').write_text('ours\n')
        commit_all(self.repo)
        ours = sync_git.head(self.repo)
        candidate = self.base / 'candidate'
        sync_git.prepare_candidate(self.repo, candidate, ours, theirs, merge=True)
        parents = sync_git.parents(candidate, sync_git.head(candidate))
        self.assertEqual(parents, (ours, theirs))

    def test_manual_candidate_conflict_resolution(self):
        subprocess.run(['git', '-C', str(self.repo), 'switch', '-c', 'theirs'],
                       env=clean_environ(), check=True, capture_output=True)
        (self.repo / 'base.md').write_text('theirs\n')
        commit_all(self.repo)
        theirs = sync_git.head(self.repo)
        subprocess.run(['git', '-C', str(self.repo), 'switch', '-c', 'ours', 'HEAD^'],
                       env=clean_environ(), check=True, capture_output=True)
        (self.repo / 'base.md').write_text('ours\n')
        commit_all(self.repo)
        ours = sync_git.head(self.repo)
        candidate = self.base / 'candidate'
        result = sync_git.prepare_candidate(self.repo, candidate, ours, theirs, merge=True)
        self.assertEqual(result['state'], 'conflict')
        (candidate / 'base.md').write_text('resolved\n')
        sync_git.run(candidate, 'add', '--', 'base.md')
        resolved = sync_git.commit_merge(candidate)
        self.assertEqual(sync_git.parents(candidate, resolved), (ours, theirs))

    def test_observe_refuses_uncommitted_edits_and_detached_head(self):
        (self.repo / 'dirty.md').write_text('user data\n')
        with self.assertRaises(WikiError):
            sync_git.observe(self.repo)
        (self.repo / 'dirty.md').unlink()
        subprocess.run(['git', '-C', str(self.repo), 'switch', '--detach', 'HEAD'],
                       env=clean_environ(), check=True, capture_output=True)
        with self.assertRaises(WikiError):
            sync_git.observe(self.repo)

    def test_observe_refuses_assume_unchanged_and_sparse_checkout(self):
        subprocess.run(['git', '-C', str(self.repo), 'update-index', '--assume-unchanged', 'base.md'],
                       env=clean_environ(), check=True, capture_output=True)
        with self.assertRaisesRegex(WikiError, 'assume-unchanged'):
            sync_git.observe(self.repo)
        subprocess.run(['git', '-C', str(self.repo), 'update-index', '--no-assume-unchanged', 'base.md'],
                       env=clean_environ(), check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.repo), 'config', 'core.sparseCheckout', 'true'],
                       env=clean_environ(), check=True, capture_output=True)
        with self.assertRaisesRegex(WikiError, 'sparse'):
            sync_git.observe(self.repo)


class SyncLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pisar-sync-lifecycle-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root, self.state = self.base / 'wiki', self.base / 'runtime'
        init_repo(self.root)
        domain(self.root, 'work')
        path = space(self.root, 'work/10-projects/alpha', 'alpha')
        (path / 'note.md').write_text(document('note', 'work/alpha'))
        (path / 'sources').mkdir()
        (path / 'sources' / 'original.txt').write_bytes(b'original\r\nsource\r\n')
        commit_all(self.root)
        self.env = clean_environ(XDG_DATA_HOME=str(self.base / 'data'),
                                 XDG_CONFIG_HOME=str(self.base / 'config'),
                                 XDG_CACHE_HOME=str(self.base / 'cache'))
        self.remote = self.base / 'origin.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], env=self.env, check=True)
        self.git(self.root, 'remote', 'add', 'origin', str(self.remote))
        self.git(self.root, 'push', '-q', '-u', 'origin', 'HEAD:refs/heads/main')
        self.git(self.remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.clone = self.base / 'remote-writer'
        subprocess.run(['git', 'clone', '--no-local', '-q', str(self.remote), str(self.clone)],
                       env=self.env, check=True)
        self.git(self.clone, 'config', 'user.email', 'synthetic@example.invalid')
        self.git(self.clone, 'config', 'user.name', 'Synthetic fixture')

    def git(self, repo, *args):
        return subprocess.run(['git', '-C', str(repo), *args], env=self.env, text=True,
                              capture_output=True, check=True).stdout.strip()

    def incoming(self, path='incoming.md', content='incoming\n'):
        target = self.clone / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git(self.clone, 'add', '--', path)
        self.git(self.clone, 'commit', '-qm', 'incoming fixture')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        return self.git(self.clone, 'rev-parse', 'HEAD')

    def _needs_decision(self, request):
        path = request['changed_paths'][0]
        quote = request['versions']['candidate'][path]
        return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                'domain': request['domain'], 'model': 'synthetic-A',
                'prompt_version': sync_review.PROMPT_VERSION, 'confidence': 0.99,
                'context_complete': True, 'verdict': 'needs_decision', 'statements': [],
                'findings': [{'id': 'f1', 'category': 'contradiction', 'paths': [path],
                              'evidence_quotes': [quote], 'explanation': 'Synthetic conflict.',
                              'options': [], 'question': 'Which statement?'}],
                'summary': 'Owner decision required.'}

    def _scoped_resolution(self, context, tools):
        from hashlib import sha256
        written = []
        for path, current in context['files'].items():
            value = current + '\nResolver correction.\n'
            result = tools.candidate_write(path, sha256(current.encode()).hexdigest(), value)
            written.append((path, result['sha256']))
        tools.candidate_stage([path for path, _ in written])
        return {'summary': 'Scoped resolution', 'confidence': 0.99, 'context_complete': True,
                'statements': [{'id': 's1', 'disposition': 'preserved'}], 'findings': [],
                'mappings': [{'path': path, 'sha256': digest, 'statement_ids': ['s1']}
                             for path, digest in written]}

    def test_accept_only_records_decision_then_apply_moves_exact_candidate(self):
        incoming = self.incoming()
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        self.assertEqual(prepared['candidate'], incoming)
        before = self.git(self.root, 'rev-parse', 'HEAD')
        status = self.git(self.root, 'status', '--porcelain=v1')
        sync.manual_review(wiki, self.state, fetched['operation_id'])
        sync.accept(wiki, self.state, fetched['operation_id'], prepared['revision'])
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), before)
        self.assertEqual(self.git(self.root, 'status', '--porcelain=v1'), status)
        applied = sync.apply(wiki, self.state, fetched['operation_id'])
        self.assertEqual(applied['head'], incoming)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), incoming)
        self.assertEqual(sync.apply(wiki, self.state, fetched['operation_id']), applied)

    def test_applied_operation_does_not_block_next_incoming_sync(self):
        first_tip = self.incoming('first.md')
        wiki = Wiki(self.root)
        first = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, first['operation_id'])
        sync.manual_review(wiki, self.state, first['operation_id'])
        sync.accept(wiki, self.state, first['operation_id'], prepared['revision'])
        sync.apply(wiki, self.state, first['operation_id'])
        self.incoming('second.md')
        second = sync.fetch(wiki, self.state)
        self.assertNotEqual(first['operation_id'], second['operation_id'])
        self.assertEqual(second['ours'], first_tip)

    def test_candidate_change_after_acceptance_is_refused(self):
        self.incoming()
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        sync.manual_review(wiki, self.state, fetched['operation_id'])
        sync.accept(wiki, self.state, fetched['operation_id'], prepared['revision'])
        namespace = operations.sha256(str(self.root).encode())[:24]
        candidate = self.state / 'roots' / namespace / 'candidates' / fetched['operation_id']
        (candidate / 'late.md').write_text('late candidate edit\n')
        self.git(candidate, 'add', 'late.md')
        self.git(candidate, 'commit', '-qm', 'late candidate edit')
        before = self.git(self.root, 'rev-parse', 'HEAD')
        with self.assertRaises(WikiError):
            sync.apply(wiki, self.state, fetched['operation_id'])
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), before)

    def test_task_file_changes_are_blocked_before_delivery_three_c(self):
        self.incoming('.ruwana/tasks/one.md', 'task\n')
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        with self.assertRaisesRegex(WikiError, 'task-validation-unavailable'):
            sync.prepare(wiki, self.state, fetched['operation_id'])

    def test_semantic_review_binds_full_candidate_and_feedback_stores_no_excerpt(self):
        self.incoming()
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        calls = []

        def reviewer(request):
            calls.append(request)
            path = request['changed_paths'][0]
            content = request['versions']['candidate'][path]
            return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                    'domain': request['domain'], 'model': 'synthetic',
                    'prompt_version': 'sync-review-v1', 'confidence': 0.94,
                    'context_complete': True, 'verdict': 'clear',
                    'statements': [{'id': 's1', 'side': 'candidate', 'path': path,
                                    'quote': content, 'disposition': 'preserved',
                                    'output_path': path, 'output_quote': content,
                                    'reason': 'The incoming content remains.'}],
                    'findings': [], 'summary': 'All changed content is accounted for.'}

        before = self.git(self.root, 'rev-parse', 'HEAD')
        reviewed = sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=reviewer)
        self.assertEqual(reviewed['status'], 'reviewed')
        self.assertEqual(calls[0]['candidate_revision'], reviewed['candidate_revision'])
        self.assertNotEqual(reviewed['candidate_revision'], prepared['revision'])
        self.assertEqual(calls[0]['domain'], 'workspace')
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), before)
        sync.accept(wiki, self.state, fetched['operation_id'], reviewed['revision'])
        summary_before = sync.feedback_summary(wiki, self.state)
        self.assertEqual(sum(row['missing_label'] for row in summary_before['groups']), 1)
        feedback = sync.feedback(wiki, self.state, fetched['operation_id'], 'correct')
        feedback_file = self.state / 'roots' / operations.sha256(str(self.root).encode())[:24] / 'feedback' / (
            f"{fetched['operation_id']}-{reviewed['candidate_revision']}.json")
        self.assertEqual(feedback['rating'], 'correct')
        self.assertNotIn('quote', feedback_file.read_text())
        self.assertNotIn('incoming', feedback_file.read_text())
        summary_after = sync.feedback_summary(wiki, self.state)
        self.assertEqual(sum(row['correct'] for row in summary_after['groups']), 1)
        self.assertEqual(sum(row['missing_label'] for row in summary_after['groups']), 0)
        self.assertEqual(summary_after['threshold_tuning'], 'disabled')
        record = json.loads(feedback_file.read_text())
        record['rating'] = 'corrected'
        feedback_file.write_text(json.dumps(record))
        corrected = sync.feedback_summary(wiki, self.state)['groups'][0]
        self.assertEqual(corrected['corrected'], 1)
        self.assertEqual(corrected['corrected_or_rejected_fraction'], 1.0)
        self.assertEqual(corrected['confidence_band'], '0.90-0.94')

    def test_auto_pull_applies_only_after_clear_review_meets_threshold(self):
        incoming = self.incoming('work/10-projects/alpha/note.md', document('note', 'work/alpha',
                                                                          body='A supported update.'))
        wiki = Wiki(self.root)
        policy = sync_review.SemanticPolicy(approval='auto', min_confidence=0.95)

        def reviewer(request):
            path = request['changed_paths'][0]
            content = request['versions']['candidate'][path]
            return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                    'domain': request['domain'], 'model': 'synthetic',
                    'prompt_version': sync_review.PROMPT_VERSION, 'confidence': 0.99,
                    'context_complete': True, 'verdict': 'clear',
                    'statements': [{'id': 's1', 'side': 'candidate', 'path': path,
                                    'quote': content, 'disposition': 'preserved',
                                    'output_path': path, 'output_quote': content,
                                    'reason': 'The changed assertion is supported.'}],
                    'findings': [], 'summary': 'No unresolved semantic issue.'}

        before = sync_git.head(self.root)
        with mock.patch.object(sync_review, 'policy', return_value=policy), \
                mock.patch.object(settings, 'semantic_policy', return_value={'approval': 'auto'}):
            result = sync.pull(wiki, self.state, review_backend=reviewer)
            self.assertEqual(result['status'], 'applied-automatically')
        self.assertEqual(sync_git.head(self.root), incoming)
        self.assertNotEqual(before, incoming)

    def test_auto_pull_does_not_apply_below_confidence_threshold(self):
        self.incoming('work/10-projects/alpha/note.md', document('note', 'work/alpha',
                                                                  body='A questionable update.'))
        wiki = Wiki(self.root)
        policy = sync_review.SemanticPolicy(approval='auto', min_confidence=0.95)

        def reviewer(request):
            path = request['changed_paths'][0]
            content = request['versions']['candidate'][path]
            return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                    'domain': request['domain'], 'model': 'synthetic',
                    'prompt_version': sync_review.PROMPT_VERSION, 'confidence': 0.94,
                    'context_complete': True, 'verdict': 'clear',
                    'statements': [{'id': 's1', 'side': 'candidate', 'path': path,
                                    'quote': content, 'disposition': 'preserved',
                                    'output_path': path, 'output_quote': content,
                                    'reason': 'The update remains.'}],
                    'findings': [], 'summary': 'Low confidence.'}

        before = sync_git.head(self.root)
        with mock.patch.object(sync_review, 'policy', return_value=policy), \
                mock.patch.object(settings, 'semantic_policy', return_value={'approval': 'auto'}):
            result = sync.pull(wiki, self.state, review_backend=reviewer)
        self.assertEqual(result['status'], 'needs-decision')
        self.assertEqual(sync_git.head(self.root), before)

    def test_resolution_lifecycle_keeps_git_conflict_candidate_only_until_owner_approval(self):
        note = self.root / 'work/10-projects/alpha/note.md'
        note.write_text(document('note', 'work/alpha', body='Ours statement.'))
        commit_all(self.root)
        ours = sync_git.head(self.root)
        remote_note = self.clone / 'work/10-projects/alpha/note.md'
        remote_note.write_text(document('note', 'work/alpha', body='Theirs statement.'))
        self.git(self.clone, 'add', '--', 'work/10-projects/alpha/note.md')
        self.git(self.clone, 'commit', '-qm', 'incoming conflicting statement')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        theirs = self.git(self.clone, 'rev-parse', 'HEAD')
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        conflict = sync.prepare(wiki, self.state, fetched['operation_id'], merge=True)
        self.assertEqual(conflict['status'], 'conflict')
        live_before = sync_git.head(self.root)

        def resolver(context, tools):
            path = 'work/10-projects/alpha/note.md'
            output = document('note', 'work/alpha', body='Both statements, with their sources retained.')
            written = tools.candidate_write(path, __import__('hashlib').sha256(
                context['files'][path].encode()).hexdigest(), output)
            tools.candidate_stage([path])
            return {'summary': 'Retain both attributed statements', 'confidence': 0.99,
                    'context_complete': True,
                    'statements': [{'id': 'ours-1', 'disposition': 'preserved'},
                                   {'id': 'theirs-1', 'disposition': 'preserved'}],
                    'findings': [], 'mappings': [{'path': path, 'sha256': written['sha256'],
                                                  'statement_ids': ['ours-1', 'theirs-1']}]}

        proposal = sync.resolve(wiki, self.state, fetched['operation_id'], backend=resolver)
        self.assertEqual(proposal['status'], 'resolution-pending')
        self.assertEqual(sync_git.head(self.root), live_before)
        with self.assertRaisesRegex(WikiError, 'owner decision'):
            sync.resolve(wiki, self.state, fetched['operation_id'], revision=proposal['revision'])
        completed = sync.resolve(wiki, self.state, fetched['operation_id'], revision=proposal['revision'],
                                 decision='Preserve both statements with attribution.')
        self.assertEqual(completed['status'], 'prepared')
        candidate = Path(completed['candidate_worktree'])
        self.assertEqual(sync_git.parents(candidate, completed['candidate']), (ours, theirs))
        self.assertEqual(sync_git.head(self.root), live_before)

        def reviewer(request):
            path = request['changed_paths'][0]
            content = request['versions']['candidate'][path]
            return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                    'domain': request['domain'], 'model': 'synthetic',
                    'prompt_version': sync_review.PROMPT_VERSION, 'confidence': 0.99,
                    'context_complete': True, 'verdict': 'clear',
                    'statements': [{'id': 's1', 'side': 'candidate', 'path': path, 'quote': content,
                                    'disposition': 'preserved', 'output_path': path, 'output_quote': content,
                                    'reason': 'Fresh review accounts for the resolution.'}],
                    'findings': [], 'summary': 'Resolution has no remaining hard finding.'}

        reviewed = sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=reviewer)
        sync.accept(wiki, self.state, fetched['operation_id'], reviewed['revision'])
        applied = sync.apply(wiki, self.state, fetched['operation_id'])
        self.assertEqual(applied['head'], completed['candidate'])
        feedback = sync.feedback(wiki, self.state, fetched['operation_id'], 'correct')
        self.assertEqual(feedback['resolver_proposal_revision'], proposal['revision'])
        resolver_group = next(row for row in sync.feedback_summary(wiki, self.state)['groups']
                              if row['review_kind'] == 'resolver')
        self.assertEqual(resolver_group['correct'], 1)

    def test_auto_resolver_commits_candidate_then_requires_fresh_clear_review_before_apply(self):
        from hashlib import sha256
        note_rel, other_rel = ('work/10-projects/alpha/note.md', 'work/10-projects/alpha/other.md')
        (self.root / note_rel).write_text(document('note', 'work/alpha', body='Status: active.'))
        (self.root / other_rel).write_text(document('other-note', 'work/alpha', body='Decision: continue.'))
        commit_all(self.root)
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(self.clone, 'fetch', '-q', 'origin')
        self.git(self.clone, 'merge', '--ff-only', 'FETCH_HEAD')
        (self.root / note_rel).write_text(document('note', 'work/alpha', body='Status: active in June.'))
        commit_all(self.root)
        (self.clone / other_rel).write_text(document('other-note', 'work/alpha', body='Decision: stop in June.'))
        self.git(self.clone, 'add', other_rel)
        self.git(self.clone, 'commit', '-qm', 'remote decision')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        sync.prepare(wiki, self.state, fetched['operation_id'], merge=True)
        policy = sync_review.SemanticPolicy(approval='auto', min_confidence=0.95)

        def contradictory_review(request):
            paths = request['changed_paths']
            return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                    'domain': request['domain'], 'model': 'synthetic',
                    'prompt_version': sync_review.PROMPT_VERSION, 'confidence': 0.99,
                    'context_complete': True, 'verdict': 'needs_decision', 'statements': [],
                    'findings': [{'id': 'f1', 'category': 'contradiction', 'paths': paths,
                                  'evidence_quotes': [request['versions']['candidate'][p] for p in paths],
                                  'explanation': 'The separate documents disagree.', 'options': [],
                                  'question': 'Which is current?'}],
                    'summary': 'A contradiction remains.'}

        def resolver(context, tools):
            outputs = {}
            for path, current in context['files'].items():
                fixed = current.replace('Status: active in June.', 'Status: active in June, decision pending.') \
                    .replace('Decision: stop in June.', 'Decision: stop in June, status remains under review.')
                if fixed == current:
                    continue
                outputs[path] = tools.candidate_write(path, sha256(current.encode()).hexdigest(), fixed)
            tools.candidate_stage(sorted(outputs))
            ids = [f's{index}' for index in range(len(outputs))]
            return {'summary': 'Reconcile the June statements with explicit uncertainty.',
                    'confidence': 0.99, 'context_complete': True, 'findings': [],
                    'statements': [{'id': ident, 'disposition': 'preserved'} for ident in ids],
                    'mappings': [{'path': path, 'sha256': value['sha256'], 'statement_ids': [ids[index]]}
                                 for index, (path, value) in enumerate(sorted(outputs.items()))]}

        def clear_review(request):
            paths = request['changed_paths']
            statements = []
            for index, path in enumerate(paths):
                content = request['versions']['candidate'][path]
                statements.append({'id': f'r{index}', 'side': 'candidate', 'path': path,
                                   'quote': content, 'disposition': 'preserved', 'output_path': path,
                                   'output_quote': content, 'reason': 'The resolution states the uncertainty.'})
            return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                    'domain': request['domain'], 'model': 'synthetic',
                    'prompt_version': sync_review.PROMPT_VERSION, 'confidence': 0.99,
                    'context_complete': True, 'verdict': 'clear', 'statements': statements,
                    'findings': [], 'summary': 'Fresh review passes.'}

        before = sync_git.head(self.root)
        with mock.patch.object(sync_review, 'policy', return_value=policy), \
                mock.patch.object(settings, 'semantic_policy', return_value={'approval': 'auto'}):
            first = sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=contradictory_review)
            with self.assertRaises(WikiError):
                sync.accept(wiki, self.state, fetched['operation_id'], first['revision'])
            result = sync.resolve(wiki, self.state, fetched['operation_id'], backend=resolver,
                                  review_backend=clear_review)
        self.assertEqual(result['status'], 'applied-automatically')
        self.assertNotEqual(sync_git.head(self.root), before)

    def test_candidate_change_while_reviewer_runs_discards_the_stale_report(self):
        self.incoming()
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        candidate = self.state / 'roots' / operations.sha256(str(self.root).encode())[:24] / 'candidates' / fetched['operation_id']

        def reviewer(request):
            (candidate / 'late.md').write_text('external change during review\n')
            self.git(candidate, 'add', 'late.md')
            self.git(candidate, 'commit', '-qm', 'external change during review')
            path = request['changed_paths'][0]
            text = request['versions']['candidate'][path]
            return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                    'domain': request['domain'], 'model': 'synthetic',
                    'prompt_version': 'sync-review-v1', 'confidence': 0.99,
                    'context_complete': True, 'verdict': 'clear',
                    'statements': [{'id': 's1', 'side': 'candidate', 'path': path, 'quote': text,
                                    'disposition': 'preserved', 'output_path': path, 'output_quote': text,
                                    'reason': 'The changed document is preserved.'}],
                    'findings': [], 'summary': 'Current revision reviewed.'}

        with self.assertRaisesRegex(WikiError, 'candidate commit changed'):
            sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=reviewer)
        self.assertEqual(sync.inspect(wiki, self.state, fetched['operation_id'])['status'], 'prepared')
        self.assertEqual(sync.inspect(wiki, self.state, fetched['operation_id']).get('semantic_review'), None)
        self.assertEqual(prepared['revision'], sync.inspect(wiki, self.state, fetched['operation_id'])['revision'])

    def test_closed_operation_does_not_reactivate_when_review_returns_late(self):
        self.incoming()
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        def reviewer(request):
            sync.close(wiki, self.state, fetched['operation_id'])
            path = request['changed_paths'][0]
            text = request['versions']['candidate'][path]
            return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                    'domain': request['domain'], 'model': 'synthetic',
                    'prompt_version': 'sync-review-v1', 'confidence': 0.99,
                    'context_complete': True, 'verdict': 'clear',
                    'statements': [{'id': 's1', 'side': 'candidate', 'path': path, 'quote': text,
                                    'disposition': 'preserved', 'output_path': path, 'output_quote': text,
                                    'reason': 'The incoming document is retained.'}],
                    'findings': [], 'summary': 'All assertions are accounted for.'}

        with self.assertRaisesRegex(WikiError, 'became stale'):
            sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=reviewer)
        status = sync.inspect(wiki, self.state, fetched['operation_id'])
        self.assertEqual(status['status'], 'closed')
        self.assertEqual(status['revision'], prepared['revision'])

    def test_semantic_policy_change_invalidates_acceptance_before_apply(self):
        self.incoming()
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        sync.manual_review(wiki, self.state, fetched['operation_id'])
        sync.accept(wiki, self.state, fetched['operation_id'], prepared['revision'])
        changed = {'review': 'conflicts', 'approval': 'manual', 'agent': 'claude', 'min_confidence': 0.95}
        before = self.git(self.root, 'rev-parse', 'HEAD')
        with mock.patch('pisar.sync.settings.semantic_policy', return_value=changed):
            with self.assertRaisesRegex(WikiError, 'policy changed after acceptance'):
                sync.apply(wiki, self.state, fetched['operation_id'])
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), before)

    def test_nontext_source_context_stops_semantic_review_with_explicit_reason(self):
        source = self.root / 'work/10-projects/alpha/sources/scan.pdf'
        source.write_bytes(b'%PDF synthetic')
        commit_all(self.root)
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(self.clone, 'fetch', '-q', 'origin')
        self.git(self.clone, 'merge', '--ff-only', 'FETCH_HEAD')
        self.incoming('work/10-projects/alpha/new.md', document('new-note', 'work/alpha'))
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        operation = sync._load(operations.Runtime(self.state, self.root), fetched['operation_id'])
        review_request = sync_review.requests(wiki, operation, self.state / 'roots' /
                                              operations.sha256(str(self.root).encode())[:24] /
                                              'candidates' / fetched['operation_id'])
        self.assertFalse(review_request[0]['context_complete'], review_request[0])
        called = []
        with self.assertRaisesRegex(WikiError, 'context is incomplete'):
            sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=lambda req: called.append(req))
        self.assertEqual(called, [])

    def test_review_context_is_split_by_personal_and_work_domain(self):
        domain(self.root, 'personal')
        personal = space(self.root, 'personal/10-projects/home', 'home')
        (personal / 'note.md').write_text(document('private-note', 'personal/home', body='Private base fact.'))
        commit_all(self.root)
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(self.clone, 'fetch', '-q', 'origin')
        self.git(self.clone, 'merge', '--ff-only', 'FETCH_HEAD')
        work_note = self.clone / 'work/10-projects/alpha/note.md'
        work_note.write_text(document('note', 'work/alpha', body='Public work fact.'))
        private_note = self.clone / 'personal/10-projects/home/note.md'
        private_note.write_text(document('private-note', 'personal/home', body='Private new fact.'))
        self.git(self.clone, 'add', '--', 'work/10-projects/alpha/note.md', 'personal/10-projects/home/note.md')
        self.git(self.clone, 'commit', '-qm', 'change both domains')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        observed = []

        def reviewer(request):
            observed.append(request)
            path = request['changed_paths'][0]
            text = request['versions']['candidate'][path]
            return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                    'domain': request['domain'], 'model': 'synthetic',
                    'prompt_version': 'sync-review-v1', 'confidence': 0.99,
                    'context_complete': True, 'verdict': 'clear',
                    'statements': [{'id': 's1', 'side': 'candidate', 'path': path, 'quote': text,
                                    'disposition': 'preserved', 'output_path': path, 'output_quote': text,
                                    'reason': 'The candidate preserves the changed document.'}],
                    'findings': [], 'summary': 'Changed statement accounted for.'}

        sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=reviewer)
        self.assertEqual({request['domain'] for request in observed}, {'personal', 'work'})
        by_domain = {request['domain']: json.dumps(request) for request in observed}
        self.assertIn('Private new fact.', by_domain['personal'])
        self.assertNotIn('Private new fact.', by_domain['work'])
        self.assertIn('Public work fact.', by_domain['work'])
        self.assertNotIn('Public work fact.', by_domain['personal'])
        self.assertEqual({request['candidate_revision'] for request in observed},
                         {observed[0]['candidate_revision']})

    def test_removed_domain_changes_remain_partitioned_by_original_domain(self):
        domain(self.root, 'personal')
        private = space(self.root, 'personal/10-projects/private', 'private')
        (private / 'note.md').write_text(document('private-note', 'personal/private', body='PRIVATE MATERIAL'))
        commit_all(self.root)
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(self.clone, 'pull', '--ff-only')
        self.git(self.clone, 'rm', '-r', 'personal')
        self.git(self.clone, 'rm', 'work/.domain.toml', 'work/10-projects/alpha/note.md')
        self.git(self.clone, 'commit', '-qm', 'remove domains and documents')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        sync.prepare(wiki, self.state, fetched['operation_id'])
        operation = sync._load(operations.Runtime(self.state, self.root), fetched['operation_id'])
        candidate = sync._candidate(operations.Runtime(self.state, self.root), fetched['operation_id'])
        requests = sync_review.requests(wiki, operation, candidate)
        self.assertEqual({request['domain'] for request in requests}, {'personal', 'work'})
        payloads = {request['domain']: json.dumps(request) for request in requests}
        self.assertIn('PRIVATE MATERIAL', payloads['personal'])
        self.assertNotIn('PRIVATE MATERIAL', payloads['work'])
        self.assertIn('Discussed', payloads['work'])
        self.assertNotIn('Discussed', payloads['personal'])

    def test_review_domain_partition_uses_pinned_git_snapshots_after_live_domain_removal(self):
        domain(self.root, 'personal')
        private = space(self.root, 'personal/10-projects/private', 'private')
        (private / 'note.md').write_text(document('private-note', 'personal/private', body='PINNED PRIVATE'))
        commit_all(self.root)
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(self.clone, 'pull', '--ff-only')
        self.git(self.clone, 'rm', '-r', 'personal')
        self.git(self.clone, 'rm', 'work/.domain.toml', 'work/10-projects/alpha/note.md')
        self.git(self.clone, 'commit', '-qm', 'remove domains and documents')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        initial_wiki = Wiki(self.root)
        fetched = sync.fetch(initial_wiki, self.state)
        sync.prepare(initial_wiki, self.state, fetched['operation_id'])
        self.git(self.root, 'rm', '-r', 'personal')
        self.git(self.root, 'rm', 'work/.domain.toml', 'work/10-projects/alpha/note.md')
        self.git(self.root, 'commit', '-qm', 'remove live domains before review packaging')
        stale_wiki = Wiki(self.root)
        runtime = operations.Runtime(self.state, self.root)
        operation = sync._load(runtime, fetched['operation_id'])
        packaged = sync_review.requests(stale_wiki, operation,
                                        sync._candidate(runtime, fetched['operation_id']))
        self.assertEqual({request['domain'] for request in packaged}, {'personal', 'work'})
        payloads = {request['domain']: json.dumps(request) for request in packaged}
        self.assertIn('PINNED PRIVATE', payloads['personal'])
        self.assertNotIn('PINNED PRIVATE', payloads['work'])
        self.assertIn('Discussed', payloads['work'])

    def test_review_context_includes_directly_referenced_same_domain_document(self):
        policy_space = space(self.root, 'work/10-projects/beta', 'beta')
        (policy_space / 'policy.md').write_text(
            document('deployment-policy', 'work/beta', body='Canonical decision: deployment is forbidden.'))
        commit_all(self.root)
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(self.clone, 'pull', '--ff-only')
        note = self.clone / 'work/10-projects/alpha/note.md'
        note.write_text(document('note', 'work/alpha', body=(
            'Deployment is authorized according to [Policy](wiki:work/beta:deployment-policy).')))
        self.git(self.clone, 'add', '--', 'work/10-projects/alpha/note.md')
        self.git(self.clone, 'commit', '-qm', 'reference deployment policy')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        sync.prepare(wiki, self.state, fetched['operation_id'])
        runtime = operations.Runtime(self.state, self.root)
        operation = sync._load(runtime, fetched['operation_id'])
        requests = sync_review.requests(wiki, operation, sync._candidate(runtime, fetched['operation_id']))
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]['context_complete'])
        self.assertIn('deployment is forbidden', json.dumps(requests[0]).lower())

    def test_unresolved_semantic_finding_requires_exact_owner_decision(self):
        self.incoming()
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])

        def reviewer(request):
            path = request['changed_paths'][0]
            quote = request['versions']['candidate'][path]
            return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                    'domain': request['domain'], 'model': 'synthetic',
                    'prompt_version': 'sync-review-v1', 'confidence': 1.0,
                    'context_complete': True, 'verdict': 'needs_decision', 'statements': [],
                    'findings': [{'id': 'f1', 'category': 'contradiction', 'paths': [path],
                                  'evidence_quotes': [quote], 'explanation': 'Two statements conflict.',
                                  'options': [], 'question': 'Which statement is current?'}],
                    'summary': 'Owner decision required.'}

        reviewed = sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=reviewer)
        with self.assertRaisesRegex(WikiError, 'owner decision'):
            sync.accept(wiki, self.state, fetched['operation_id'], reviewed['revision'])
        accepted = sync.accept(wiki, self.state, fetched['operation_id'], reviewed['revision'],
                               decision='Keep the candidate statement as current.')
        self.assertEqual(accepted['status'], 'accepted')
        runtime = operations.Runtime(self.state, self.root)
        operation = sync._load(runtime, fetched['operation_id'])
        operation['accepted']['decision'] = 'changed after acceptance'
        sync._save(runtime, operation)
        with self.assertRaisesRegex(WikiError, 'decision binding'):
            sync.apply(wiki, self.state, fetched['operation_id'])

    def test_resolve_rejects_stale_semantic_candidate_before_backend_call(self):
        rel = 'work/10-projects/alpha/note.md'
        self.incoming(rel, document('note', 'work/alpha', body='Incoming fact.'))
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=self._needs_decision)
        candidate = Path(prepared['candidate_worktree'])
        (candidate / rel).write_text(document('note', 'work/alpha', body='Externally corrected fact.'))
        self.git(candidate, 'add', rel)
        self.git(candidate, 'commit', '-qm', 'external candidate correction')
        calls = []
        with self.assertRaisesRegex(WikiError, 'candidate commit changed'):
            sync.resolve(wiki, self.state, fetched['operation_id'],
                         backend=lambda context, tools: (calls.append(context),
                                                         self._scoped_resolution(context, tools))[1])
        self.assertEqual(calls, [])

    def test_resolve_validates_persisted_finding_report_before_using_its_paths(self):
        rel = 'work/10-projects/alpha/note.md'
        unrelated = 'work/10-projects/alpha/other.md'
        (self.root / unrelated).write_text(document('other', 'work/alpha', body='Unchanged.'))
        commit_all(self.root)
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(self.clone, 'fetch', '-q', 'origin')
        self.git(self.clone, 'merge', '--ff-only', 'FETCH_HEAD')
        self.incoming(rel, document('note', 'work/alpha', body='Incoming fact.'))
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        sync.prepare(wiki, self.state, fetched['operation_id'])
        sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=self._needs_decision)
        operation = sync._load(operations.Runtime(self.state, self.root), fetched['operation_id'])
        report_path = Path(operation['semantic_review']['report_path'])
        reports = json.loads(report_path.read_text(encoding='utf-8'))
        reports[0]['findings'][0]['paths'] = [unrelated]
        report_path.write_text(json.dumps(reports), encoding='utf-8')
        calls = []
        with self.assertRaisesRegex(WikiError, 'report digest'):
            sync.resolve(wiki, self.state, fetched['operation_id'],
                         backend=lambda context, tools: (calls.append(context),
                                                         self._scoped_resolution(context, tools))[1])
        self.assertEqual(calls, [])
        self.assertEqual((self.root / unrelated).read_text(), document('other', 'work/alpha', body='Unchanged.'))

    def test_resolve_refuses_removed_domain_markers_before_combining_context(self):
        personal = space(self.root, 'personal/projects/home', 'home')
        (personal / 'private.md').write_text(document('private', 'personal/home', body='Private original.'))
        commit_all(self.root)
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(self.clone, 'fetch', '-q', 'origin')
        self.git(self.clone, 'merge', '--ff-only', 'FETCH_HEAD')
        work_rel, personal_rel = ('work/10-projects/alpha/note.md', 'personal/projects/home/private.md')
        self.incoming(work_rel, document('note', 'work/alpha', body='Work incoming fact.'))
        self.git(self.clone, 'pull', '-q', '--ff-only', 'origin', 'main')
        self.incoming(personal_rel, document('private', 'personal/home', body='Personal incoming fact.'))
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=self._needs_decision)
        candidate = Path(prepared['candidate_worktree'])
        (candidate / 'work/.domain.toml').unlink()
        (candidate / 'personal/.domain.toml').unlink()
        calls = []
        with self.assertRaises(WikiError):
            sync.resolve(wiki, self.state, fetched['operation_id'],
                         backend=lambda context, tools: (calls.append(context),
                                                         self._scoped_resolution(context, tools))[1])
        self.assertEqual(calls, [])

    def test_two_domain_resolver_proposal_keeps_sessions_separate(self):
        domain(self.root, 'personal')
        personal = space(self.root, 'personal/projects/home', 'home')
        (personal / 'private.md').write_text(document('private', 'personal/home', body='Private original.'))
        commit_all(self.root)
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(self.clone, 'fetch', '-q', 'origin')
        self.git(self.clone, 'merge', '--ff-only', 'FETCH_HEAD')
        work_rel, personal_rel = ('work/10-projects/alpha/note.md', 'personal/projects/home/private.md')
        self.incoming(work_rel, document('note', 'work/alpha', body='Work incoming fact.'))
        self.git(self.clone, 'pull', '-q', '--ff-only', 'origin', 'main')
        self.incoming(personal_rel, document('private', 'personal/home', body='Personal incoming fact.'))
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        review_domains = []

        def reviewer(request):
            review_domains.append(request['domain'])
            return self._needs_decision(request)

        sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=reviewer)
        resolver_contexts = []

        def resolver(context, tools):
            resolver_contexts.append(context)
            return self._scoped_resolution(context, tools)

        proposal = sync.resolve(wiki, self.state, fetched['operation_id'], backend=resolver)
        self.assertEqual(proposal['status'], 'resolution-pending')
        self.assertEqual(review_domains, ['personal', 'work'])
        self.assertEqual([context['domain'] for context in resolver_contexts], ['personal', 'work'])
        self.assertEqual([sorted(context['files']) for context in resolver_contexts],
                         [[personal_rel], [work_rel]])
        self.assertNotIn('Personal incoming fact.', json.dumps(resolver_contexts[1]))
        self.assertNotIn('Work incoming fact.', json.dumps(resolver_contexts[0]))
        approved = sync.resolve(wiki, self.state, fetched['operation_id'], revision=proposal['revision'],
                                decision='Approve this exact two-domain candidate.')
        self.assertEqual(approved['status'], 'prepared')
        candidate = Path(approved['candidate_worktree'])
        self.assertIn('Resolver correction.', (candidate / personal_rel).read_text())
        self.assertIn('Resolver correction.', (candidate / work_rel).read_text())

    def test_two_domain_native_conflict_resolution_preserves_merge_parents(self):
        domain(self.root, 'personal')
        personal = space(self.root, 'personal/projects/home', 'home')
        (personal / 'private.md').write_text(document('private', 'personal/home', body='Personal base.'))
        work_rel, personal_rel = ('work/10-projects/alpha/note.md', 'personal/projects/home/private.md')
        commit_all(self.root)
        ours = sync_git.head(self.root)
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(self.clone, 'fetch', '-q', 'origin')
        self.git(self.clone, 'merge', '--ff-only', 'FETCH_HEAD')
        for path, ident, owner, text in ((work_rel, 'note', 'work/alpha', 'Remote work fact.'),
                                         (personal_rel, 'private', 'personal/home', 'Remote personal fact.')):
            (self.clone / path).write_text(document(ident, owner, body=text))
        self.git(self.clone, 'add', '--', work_rel, personal_rel)
        self.git(self.clone, 'commit', '-qm', 'remote changes in both domains')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        theirs = self.git(self.clone, 'rev-parse', 'HEAD')
        for path, ident, owner, text in ((work_rel, 'note', 'work/alpha', 'Local work fact.'),
                                         (personal_rel, 'private', 'personal/home', 'Local personal fact.')):
            (self.root / path).write_text(document(ident, owner, body=text))
        commit_all(self.root)
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        conflict = sync.prepare(wiki, self.state, fetched['operation_id'], merge=True)
        self.assertEqual(conflict['status'], 'conflict')
        resolver_contexts = []

        def resolver(context, tools):
            from hashlib import sha256
            resolver_contexts.append(context)
            written = []
            for path, current in context['files'].items():
                if path == personal_rel:
                    value = document('private', 'personal/home', body='Both local and remote personal facts.')
                else:
                    value = document('note', 'work/alpha', body='Both local and remote work facts.')
                result = tools.candidate_write(path, sha256(current.encode()).hexdigest(), value)
                written.append((path, result['sha256']))
            tools.candidate_stage([path for path, _ in written])
            return {'summary': 'Preserve both domain statements', 'confidence': 0.99,
                    'context_complete': True,
                    'statements': [{'id': 's1', 'disposition': 'preserved'}], 'findings': [],
                    'mappings': [{'path': path, 'sha256': digest, 'statement_ids': ['s1']}
                                 for path, digest in written]}

        proposal = sync.resolve(wiki, self.state, fetched['operation_id'], backend=resolver)
        self.assertEqual([context['domain'] for context in resolver_contexts], ['personal', 'work'])
        self.assertEqual([sorted(context['files']) for context in resolver_contexts],
                         [[personal_rel], [work_rel]])
        completed = sync.resolve(wiki, self.state, fetched['operation_id'], revision=proposal['revision'],
                                 decision='Resolve both domains with separate scoped inputs.')
        candidate = Path(completed['candidate_worktree'])
        operation = sync._load(operations.Runtime(self.state, self.root), fetched['operation_id'])
        self.assertEqual(sync_git.parents(candidate, completed['candidate']),
                         (operation['ours'], operation['theirs']))

    def test_auto_resolver_rejects_duplicate_processing_before_candidate_commit(self):
        space_root = self.root / 'work/10-projects/alpha'
        for ident, source, raw in (('meeting-a', 'sources/meetings/meeting-a/transcript.md', b'original A\\n'),
                                   ('meeting-b', 'sources/meetings/meeting-b/transcript.md', b'original B\\n')):
            source_path = space_root / source
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(raw)
            meeting_path = space_root / 'meetings' / f'{ident}.md'
            meeting_path.parent.mkdir(parents=True, exist_ok=True)
            meeting_path.write_text(document(ident, 'work/alpha', sources=[source], body=f'{ident} completed.'))
        commit_all(self.root)
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(self.clone, 'fetch', '-q', 'origin')
        self.git(self.clone, 'merge', '--ff-only', 'FETCH_HEAD')
        rel = 'work/10-projects/alpha/meetings/meeting-a.md'
        self.incoming(rel, document('meeting-a', 'work/alpha',
                                    sources=['sources/meetings/meeting-a/transcript.md'],
                                    body='Incoming clarification.'))
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        candidate = Path(prepared['candidate_worktree'])
        before_candidate = sync_git.head(candidate)
        before_live = sync_git.head(self.root)

        def duplicate(context, tools):
            from hashlib import sha256
            path = next(iter(context['files']))
            value = document('meeting-a', 'work/alpha',
                             sources=['sources/meetings/meeting-b/transcript.md'],
                             body='Resolver claimed completion with another original.')
            output = tools.candidate_write(path, sha256(context['files'][path].encode()).hexdigest(), value)
            tools.candidate_stage([path])
            return {'summary': 'Duplicate processing provenance', 'confidence': 1.0,
                    'context_complete': True, 'statements': [{'id': 's1', 'disposition': 'preserved'}],
                    'findings': [], 'mappings': [{'path': path, 'sha256': output['sha256'],
                                                   'statement_ids': ['s1']}]}

        policy = sync_review.SemanticPolicy(approval='auto', min_confidence=0.95)
        with mock.patch.object(sync_review, 'policy', return_value=policy), \
                mock.patch.object(settings, 'semantic_policy', return_value={'agent': 'claude', 'approval': 'auto'}):
            sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=self._needs_decision)
            with self.assertRaisesRegex(WikiError, 'duplicate-source-decision-required'):
                sync.resolve(wiki, self.state, fetched['operation_id'], backend=duplicate)
        self.assertEqual(sync_git.head(candidate), before_candidate)
        self.assertEqual(sync_git.head(self.root), before_live)

    def test_auto_resolver_validates_candidate_structure_before_candidate_commit(self):
        rel = 'work/10-projects/alpha/note.md'
        self.incoming(rel, document('note', 'work/alpha', body='Incoming fact.'))
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        policy = sync_review.SemanticPolicy(approval='auto', min_confidence=0.95)
        candidate = Path(prepared['candidate_worktree'])
        before_candidate = sync_git.head(candidate)
        before_live = sync_git.head(self.root)

        def invalid_resolver(context, tools):
            from hashlib import sha256
            path = context['changed_paths'][0] if 'changed_paths' in context else next(iter(context['files']))
            text = document('note', 'personal/nonexistent', body='Invalid owner address.')
            result = tools.candidate_write(path, sha256(context['files'][path].encode()).hexdigest(), text)
            tools.candidate_stage([path])
            return {'summary': 'Invalid structure', 'confidence': 1.0, 'context_complete': True,
                    'statements': [{'id': 's1', 'disposition': 'preserved'}], 'findings': [],
                    'mappings': [{'path': path, 'sha256': result['sha256'], 'statement_ids': ['s1']}]}

        with mock.patch.object(sync_review, 'policy', return_value=policy), \
                mock.patch.object(settings, 'semantic_policy', return_value={'approval': 'auto'}):
            sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=self._needs_decision)
            with self.assertRaisesRegex(WikiError, 'structural check|space_ids'):
                sync.resolve(wiki, self.state, fetched['operation_id'], backend=invalid_resolver)
        self.assertEqual(sync_git.head(candidate), before_candidate)
        self.assertEqual(sync_git.head(self.root), before_live)

    def test_feedback_summary_keeps_labelled_historical_reviewer_after_rereview(self):
        rel = 'work/10-projects/alpha/note.md'
        self.incoming(rel, document('note', 'work/alpha', body='Incoming fact.'))
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        sync.prepare(wiki, self.state, fetched['operation_id'])
        first = sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=self._needs_decision)
        sync.feedback(wiki, self.state, fetched['operation_id'], 'rejected')
        policy = sync_review.SemanticPolicy(model='synthetic-B')

        def second(request):
            report = self._needs_decision(request)
            report['model'] = 'synthetic-B'
            return report

        with mock.patch.object(sync_review, 'policy', return_value=policy):
            sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=second)
        groups = sync.feedback_summary(wiki, self.state)['groups']
        old = next(row for row in groups if row['model'] == 'synthetic-A')
        new = next(row for row in groups if row['model'] == 'synthetic-B')
        self.assertEqual(old['rejected'], 1)
        self.assertEqual(old['corrected_or_rejected_fraction'], 1.0)
        self.assertEqual(new['missing_label'], 1)

    def test_semantic_acceptance_token_changes_with_policy_and_binds_report_bytes(self):
        self.incoming()
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        sync.prepare(wiki, self.state, fetched['operation_id'])

        def reviewer(request):
            path = request['changed_paths'][0]
            text = request['versions']['candidate'][path]
            return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                    'domain': request['domain'], 'model': 'synthetic',
                    'prompt_version': 'sync-review-v1', 'confidence': 0.99,
                    'context_complete': True, 'verdict': 'clear',
                    'statements': [{'id': 's1', 'side': 'candidate', 'path': path, 'quote': text,
                                    'disposition': 'preserved', 'output_path': path, 'output_quote': text,
                                    'reason': 'The candidate content is retained.'}],
                    'findings': [], 'summary': 'First report.'}

        first = sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=reviewer)
        with mock.patch.object(sync_review, 'PROMPT_VERSION', 'sync-review-v2'):
            with self.assertRaisesRegex(WikiError, 'policy changed'):
                sync.accept(wiki, self.state, fetched['operation_id'], first['revision'])
        original_policy = settings.semantic_policy()
        changed_policy = {**original_policy, 'min_confidence': 0.99}
        with mock.patch('pisar.sync.settings.semantic_policy', return_value=changed_policy):
            refreshed = sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=reviewer)
            self.assertNotEqual(first['revision'], refreshed['revision'])
            with self.assertRaisesRegex(WikiError, 'revision'):
                sync.accept(wiki, self.state, fetched['operation_id'], first['revision'])

        runtime = operations.Runtime(self.state, self.root)
        operation = sync._load(runtime, fetched['operation_id'])
        report_path = Path(operation['semantic_review']['report_path'])
        reports = json.loads(report_path.read_text(encoding='utf-8'))
        reports[0]['summary'] = 'Replaced report after review.'
        report_path.write_text(json.dumps(reports), encoding='utf-8')
        with mock.patch('pisar.sync.settings.semantic_policy', return_value=changed_policy):
            with self.assertRaisesRegex(WikiError, 'report digest'):
                sync.accept(wiki, self.state, fetched['operation_id'], refreshed['revision'])

    def test_configured_model_change_invalidates_cache_and_old_acceptance_token(self):
        self.incoming()
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        sync.prepare(wiki, self.state, fetched['operation_id'])
        model = ['model-A']

        def bounded(argv, prompt, cwd, environment):
            request = json.loads(prompt)
            path = request['changed_paths'][0]
            text = request['versions']['candidate'][path]
            response = {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                        'domain': request['domain'], 'model': 'untrusted-model-field',
                        'prompt_version': 'sync-review-v1', 'confidence': 0.99,
                        'context_complete': True, 'verdict': 'clear',
                        'statements': [{'id': 's1', 'side': 'candidate', 'path': path, 'quote': text,
                                        'disposition': 'preserved', 'output_path': path, 'output_quote': text,
                                        'reason': 'The candidate content is retained.'}],
                        'findings': [], 'summary': 'Configured reviewer response.'}
            envelope = json.dumps({'structured_output': response})
            return SimpleNamespace(state='ok', returncode=0, stdout=envelope.encode(), stderr='')

        with mock.patch('pisar.sync_review.shutil.which', return_value='/synthetic/claude'), \
                mock.patch('pisar.sync_review.research.run_bounded', side_effect=bounded), \
                mock.patch('pisar.settings.agent_option', side_effect=lambda *args: (model[0], 'config')):
            first = sync.semantic_review(wiki, self.state, fetched['operation_id'])
            model[0] = 'model-B'
            second = sync.semantic_review(wiki, self.state, fetched['operation_id'])
            self.assertFalse(second.get('reused', False))
            self.assertNotEqual(first['candidate_revision'], second['candidate_revision'])
            self.assertEqual(second['reports'][0]['model'], 'model-B')
            with self.assertRaisesRegex(WikiError, 'revision'):
                sync.accept(wiki, self.state, fetched['operation_id'], first['revision'])

    def test_clean_merge_with_opposing_paragraphs_is_reviewed_and_stays_unapplied(self):
        note_rel = 'work/10-projects/alpha/note.md'
        other_rel = 'work/10-projects/alpha/other.md'
        note = self.root / note_rel
        note.write_text(document('note', 'work/alpha', body='Status: active.'))
        (self.root / other_rel).write_text(document('other-note', 'work/alpha', body='Decision: continue.'))
        commit_all(self.root)
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(self.clone, 'fetch', '-q', 'origin')
        self.git(self.clone, 'merge', '--ff-only', 'FETCH_HEAD')
        note.write_text(document('note', 'work/alpha', body='Status: active in June.'))
        commit_all(self.root)
        ours = self.git(self.root, 'rev-parse', 'HEAD')
        incoming = document('other-note', 'work/alpha', body='Decision: stop in June.')
        (self.clone / other_rel).write_text(incoming)
        self.git(self.clone, 'add', other_rel)
        self.git(self.clone, 'commit', '-qm', 'remote decision')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'], merge=True)
        self.assertEqual(prepared['status'], 'prepared')
        before = self.git(self.root, 'rev-parse', 'HEAD')

        def reviewer(request):
            changed = request['changed_paths']
            quotes = [request['versions']['candidate'][path] for path in changed]
            return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                    'domain': request['domain'], 'model': 'synthetic',
                    'prompt_version': 'sync-review-v1', 'confidence': 1.0,
                    'context_complete': True, 'verdict': 'needs_decision', 'statements': [],
                    'findings': [{'id': 'f1', 'category': 'contradiction', 'paths': changed,
                                  'evidence_quotes': quotes,
                                  'explanation': 'Separate paragraphs give opposite status decisions.',
                                  'options': [], 'question': 'Which June decision is current?'}],
                    'summary': 'The Git-clean result contains an unresolved contradiction.'}

        reviewed = sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=reviewer)
        with self.assertRaisesRegex(WikiError, 'owner decision'):
            sync.accept(wiki, self.state, fetched['operation_id'], reviewed['revision'])
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), before)

    def test_task_file_change_then_revert_is_still_blocked(self):
        path = '.ruwana/tasks/one.md'
        task = self.clone / path
        task.parent.mkdir(parents=True)
        task.write_text('temporary task\n')
        self.git(self.clone, 'add', path)
        self.git(self.clone, 'commit', '-qm', 'temporary task')
        self.git(self.clone, 'rm', path)
        self.git(self.clone, 'commit', '-qm', 'revert task')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        with self.assertRaisesRegex(WikiError, 'task-validation-unavailable'):
            sync.prepare(wiki, self.state, fetched['operation_id'])

    def test_incoming_gitlink_is_rejected_during_prepare(self):
        child = self.base / 'child'
        init_repo(child)
        (child / 'child.txt').write_text('child\n')
        commit_all(child)
        child_oid = self.git(child, 'rev-parse', 'HEAD')
        (self.clone / '.gitmodules').write_text('[submodule "extra-child"]\n\tpath = extra-child\n\turl = ../child\n')
        self.git(self.clone, 'add', '.gitmodules')
        self.git(self.clone, 'update-index', '--add', '--cacheinfo', f'160000,{child_oid},extra-child')
        self.git(self.clone, 'commit', '-qm', 'add nested repository')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        with self.assertRaisesRegex(WikiError, 'submodule'):
            sync.prepare(wiki, self.state, fetched['operation_id'])
        self.assertNotEqual(self.git(self.root, 'rev-parse', 'HEAD'), fetched['theirs'])

    def test_corrected_candidate_can_be_revalidated_and_requires_new_review(self):
        self.incoming()
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        first = sync.prepare(wiki, self.state, fetched['operation_id'])
        sync.manual_review(wiki, self.state, fetched['operation_id'])
        sync.accept(wiki, self.state, fetched['operation_id'], first['revision'])
        candidate = self.state / 'roots' / operations.sha256(str(self.root).encode())[:24] / 'candidates' / fetched['operation_id']
        (candidate / 'correction.md').write_text('correction\n')
        self.git(candidate, 'add', 'correction.md')
        self.git(candidate, 'commit', '-qm', 'manual correction')
        second = sync.prepare(wiki, self.state, fetched['operation_id'])
        self.assertNotEqual(second['candidate'], first['candidate'])
        self.assertNotEqual(second['revision'], first['revision'])
        with self.assertRaisesRegex(WikiError, 'manual review'):
            sync.accept(wiki, self.state, fetched['operation_id'], first['revision'])

    def test_original_source_bytes_are_preserved_and_changed_originals_block(self):
        self.incoming('work/10-projects/alpha/notes.md', 'line one\r\nline two\r\n')
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        namespace = operations.sha256(str(self.root).encode())[:24]
        candidate = self.state / 'roots' / namespace / 'candidates' / fetched['operation_id']
        self.assertEqual((candidate / 'work/10-projects/alpha/sources/original.txt').read_bytes(),
                         b'original\r\nsource\r\n')
        self.assertEqual((candidate / 'work/10-projects/alpha/notes.md').read_bytes(),
                         b'line one\r\nline two\r\n')
        self.assertEqual(prepared['status'], 'prepared')

        # A second operation receives an incoming edit to an already committed original.
        source_path = 'work/10-projects/alpha/sources/original.txt'
        (self.clone / source_path).write_bytes(b'altered original\n')
        self.git(self.clone, 'add', '--', source_path)
        self.git(self.clone, 'commit', '-qm', 'alter original fixture')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        isolated_state = self.base / 'second-runtime'
        fetched2 = sync.fetch(wiki, isolated_state)
        with self.assertRaisesRegex(WikiError, 'original source changed'):
            sync.prepare(wiki, isolated_state, fetched2['operation_id'])

    def test_publish_is_explicit_and_refuses_a_remote_advanced_by_divergence(self):
        incoming = self.incoming()
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        with self.assertRaises(WikiError):
            sync.publish(wiki, self.state, fetched['operation_id'])
        sync.manual_review(wiki, self.state, fetched['operation_id'])
        sync.accept(wiki, self.state, fetched['operation_id'], prepared['revision'])
        sync.apply(wiki, self.state, fetched['operation_id'])
        published = sync.publish(wiki, self.state, fetched['operation_id'])
        self.assertEqual(published['status'], 'published')
        self.assertEqual(self.git(self.remote, 'rev-parse', 'refs/heads/main'), incoming)

    def test_publish_only_review_accept_publish_needs_no_apply(self):
        note = self.root / 'work/10-projects/alpha/note.md'
        note.write_text(note.read_text().replace('Discussed русский launch.', 'Local-only finding.'))
        commit_all(self.root)
        local_tip = self.git(self.root, 'rev-parse', 'HEAD')
        wiki = Wiki(self.root)
        candidate = sync.publish_only(wiki, self.state)
        self.assertEqual(candidate['candidate'], local_tip)
        sync.manual_review(wiki, self.state, candidate['operation_id'])
        sync.accept(wiki, self.state, candidate['operation_id'], candidate['revision'])
        self.assertEqual(sync.apply(wiki, self.state, candidate['operation_id'])['status'], 'not-applicable')
        self.assertEqual(sync.publish(wiki, self.state, candidate['operation_id'])['status'], 'published')
        self.assertEqual(self.git(self.remote, 'rev-parse', 'refs/heads/main'), local_tip)

    def test_dry_run_status_does_not_create_runtime(self):
        wiki = Wiki(self.root)
        result = sync.pull(wiki, self.state, dry_run=True)
        self.assertEqual(result['status'], 'dry-run')
        self.assertEqual(result['remote_freshness'], 'unknown')
        self.assertFalse(self.state.exists())

    def test_no_change_candidate_needs_no_semantic_model_call(self):
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'])
        self.assertEqual(prepared['candidate'], fetched['ours'])
        with mock.patch('pisar.sync_review.shutil.which', return_value=None):
            result = sync.semantic_review(wiki, self.state, fetched['operation_id'])
        self.assertEqual(result['status'], 'no-change')
        sync.accept(wiki, self.state, fetched['operation_id'], result['revision'])

    def test_conflicts_only_marks_fast_forward_unreviewed(self):
        self.incoming()
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        sync.prepare(wiki, self.state, fetched['operation_id'])
        with mock.patch('pisar.sync_review.policy', return_value=sync_review.SemanticPolicy(review='conflicts')):
            result = sync.semantic_review(wiki, self.state, fetched['operation_id'],
                                          backend=lambda request: self.fail('uncovered FF called reviewer'))
        self.assertEqual(result['status'], 'uncovered')
        self.assertEqual(result['label'], 'not semantically reviewed')

    def test_invalid_incoming_candidate_is_never_applied(self):
        path = self.clone / 'work/10-projects/alpha/note.md'
        path.write_text(path.read_text().replace('schema_version = 1', 'schema_version = 99'))
        self.git(self.clone, 'add', '--', 'work/10-projects/alpha/note.md')
        self.git(self.clone, 'commit', '-qm', 'invalid metadata fixture')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        before = self.git(self.root, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(WikiError, 'candidate structural check failed'):
            sync.prepare(wiki, self.state, fetched['operation_id'])
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), before)

    def test_manual_resolution_keeps_native_merge_and_applies_after_review(self):
        note = self.root / 'work/10-projects/alpha/note.md'
        note.write_text(note.read_text().replace('Discussed русский launch.', 'Local decision.'))
        commit_all(self.root)
        remote_note = self.clone / 'work/10-projects/alpha/note.md'
        remote_note.write_text(remote_note.read_text().replace('Discussed русский launch.', 'Remote proposal.'))
        self.git(self.clone, 'add', '--', 'work/10-projects/alpha/note.md')
        self.git(self.clone, 'commit', '-qm', 'remote conflict fixture')
        self.git(self.clone, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        first = sync.prepare(wiki, self.state, fetched['operation_id'], merge=True)
        self.assertEqual(first['status'], 'conflict')
        namespace = operations.sha256(str(self.root).encode())[:24]
        candidate = self.state / 'roots' / namespace / 'candidates' / fetched['operation_id']
        # The resolution is intentionally a full valid record; no conflict marker reaches validation.
        (candidate / 'work/10-projects/alpha/note.md').write_text(document('note', 'work/alpha', body='Reviewed both proposals.'))
        self.git(candidate, 'add', '--', 'work/10-projects/alpha/note.md')
        result = sync.prepare(wiki, self.state, fetched['operation_id'], merge=True)
        self.assertEqual(result['status'], 'prepared')
        self.assertEqual(sync_git.parents(candidate, result['candidate']), (fetched['ours'], fetched['theirs']))

        def reviewer(request):
            statements = []
            path = 'work/10-projects/alpha/note.md'
            source = request['versions']['theirs'][path]
            output = request['versions']['candidate'][path]
            statements.append({'id': 's1', 'side': 'theirs', 'path': path, 'quote': 'Remote proposal.',
                               'disposition': 'equivalent', 'output_path': path, 'output_quote': output,
                               'reason': 'The candidate explicitly records both proposals.'})
            return {'schema_version': 1, 'candidate_revision': request['candidate_revision'],
                    'domain': request['domain'], 'model': 'synthetic', 'prompt_version': 'sync-review-v1',
                    'confidence': 0.99, 'context_complete': True, 'verdict': 'clear',
                    'statements': statements, 'findings': [], 'summary': f'Reviewed {len(source)} bytes.'}

        reviewed = sync.semantic_review(wiki, self.state, fetched['operation_id'], backend=reviewer)
        sync.accept(wiki, self.state, fetched['operation_id'], reviewed['revision'])
        self.assertEqual(sync.apply(wiki, self.state, fetched['operation_id'])['head'], result['candidate'])


if __name__ == '__main__':
    unittest.main()
