"""Real-Git graph candidate and child-first application regressions."""
import subprocess
import tempfile
from pathlib import Path
import unittest

from .support import clean_environ, commit_all, domain, document, init_repo
from pisar import sync
from pisar.safety import WikiError
from pisar.spaces import Wiki


class SyncSubmoduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pisar-sync-submodules-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = clean_environ(XDG_DATA_HOME=str(self.base / 'data'),
                                 XDG_CONFIG_HOME=str(self.base / 'config'),
                                 XDG_CACHE_HOME=str(self.base / 'cache'))
        self.root = self.base / 'wiki'
        init_repo(self.root)
        domain(self.root, 'work')
        self.child_path = 'work/10-projects/alpha'
        self.child_source = self.base / 'child-source'
        init_repo(self.child_source)
        (self.child_source / '.wiki.toml').write_text(
            'schema_version = 1\nid = "alpha"\nkind = "project"\nstatus = "active"\n')
        (self.child_source / 'note.md').write_text(document('alpha-note', 'work/alpha', body='Initial child.'))
        self.grandchild_source = self.base / 'grandchild-source'
        init_repo(self.grandchild_source)
        (self.grandchild_source / 'grandchild.txt').write_text('Nested child fixture.\n')
        commit_all(self.grandchild_source)
        self.grandchild_remote = self.base / 'grandchild.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.grandchild_remote)], env=self.env, check=True)
        self.git(self.grandchild_source, 'branch', '-M', 'main')
        self.git(self.grandchild_source, 'remote', 'add', 'origin', str(self.grandchild_remote))
        self.git(self.grandchild_source, 'push', '-q', '-u', 'origin', 'main')
        self.git(self.grandchild_remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.git(self.child_source, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q',
                 str(self.grandchild_remote), 'nested')
        commit_all(self.child_source)
        self.child_remote = self.base / 'child.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.child_remote)], env=self.env, check=True)
        self.git(self.child_source, 'branch', '-M', 'main')
        self.git(self.child_source, 'remote', 'add', 'origin', str(self.child_remote))
        self.git(self.child_source, 'push', '-q', '-u', 'origin', 'main')
        self.git(self.child_remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.git(self.root, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q',
                 str(self.child_remote), self.child_path)
        self.git(self.root, '-c', 'protocol.file.allow=always', 'submodule', 'update', '--init', '--recursive')
        self.git(self.root / self.child_path / 'nested', 'switch', 'main')
        self.git(self.root / self.child_path / 'nested', 'branch', '--set-upstream-to=origin/main', 'main')
        (self.root / 'work' / '10-projects' / 'index.md').write_text(
            document('work-index', 'work/index', body='The unchanged root document.'))
        commit_all(self.root)
        self.root_remote = self.base / 'root.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.root_remote)], env=self.env, check=True)
        self.git(self.root, 'branch', '-M', 'main')
        self.git(self.root, 'remote', 'add', 'origin', str(self.root_remote))
        self.git(self.root, 'push', '-q', '-u', 'origin', 'main')
        self.git(self.root_remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.state = self.base / 'runtime'

    def git(self, repo, *args):
        result = subprocess.run(['git', '-C', str(repo), *args], env=self.env, text=True,
                                capture_output=True)
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def test_candidate_refuses_repository_local_driver_in_child(self):
        child = self.root / self.child_path
        self.git(child, 'config', 'user.name', 'Synthetic fixture')
        self.git(child, 'config', 'user.email', 'synthetic@example.invalid')
        (child / '.gitattributes').write_text('note.md merge=guard\n')
        self.git(child, 'config', 'merge.guard.driver', 'false')
        commit_all(child)
        self.git(child, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        commit_all(self.root)
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        with self.assertRaisesRegex(WikiError, 'candidate-compatibility.*merge.guard.driver'):
            sync.prepare(wiki, self.state, fetched['operation_id'])

    def test_child_task_checks_use_selected_ruwana(self):
        from unittest import mock
        self.incoming_child_and_root_pin(child_task=True)
        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        selected = str(self.base / 'child-selected-ruwana')
        with mock.patch('pisar.ruwana.Ruwana', side_effect=WikiError('synthetic child adapter stop')) as adapter:
            with self.assertRaisesRegex(WikiError, 'synthetic child adapter stop'):
                sync.prepare(wiki, self.state, fetched['operation_id'], ruwana=selected)
        self.assertEqual(adapter.call_args.args[1], selected)

    def test_auto_pull_integrates_child_history_only_pin_change(self):
        from unittest import mock
        from pisar import settings, sync_review
        child = self.root / self.child_path
        old_child_tree = self.git(child, 'rev-parse', 'HEAD^{tree}')
        self.git(self.child_source, 'commit', '--allow-empty', '-qm', 'Synthetic child history update')
        incoming_child = self.git(self.child_source, 'rev-parse', 'HEAD')
        self.git(self.child_source, 'push', '-q', 'origin', 'main')
        writer = self.base / 'history-writer'
        subprocess.run(['git', 'clone', '-q', str(self.root_remote), str(writer)], env=self.env, check=True)
        self.git(writer, 'config', 'user.name', 'Synthetic fixture')
        self.git(writer, 'config', 'user.email', 'synthetic@example.invalid')
        self.git(writer, 'update-index', '--cacheinfo', f'160000,{incoming_child},{self.child_path}')
        self.git(writer, 'commit', '-qm', 'Advance child history pin')
        incoming_root = self.git(writer, 'rev-parse', 'HEAD')
        self.git(writer, 'push', '-q', 'origin', 'main')
        policy = sync_review.SemanticPolicy(approval='auto', min_confidence=0.95)
        reviewer = mock.Mock(side_effect=AssertionError('unchanged child content invoked reviewer'))
        with mock.patch.object(sync_review, 'policy', return_value=policy), \
                mock.patch.object(settings, 'semantic_policy', return_value={'approval': 'auto'}):
            result = sync.pull(Wiki(self.root), self.state, review_backend=reviewer)
            self.assertEqual(result['status'], 'applied-automatically')
            self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), incoming_root)
            self.assertEqual(self.git(child, 'rev-parse', 'HEAD'), incoming_child)
            self.assertEqual(self.git(child, 'rev-parse', 'HEAD^{tree}'), old_child_tree)
            self.assertEqual(sync.pull(Wiki(self.root), self.state, review_backend=reviewer)['status'], 'no-change')
        reviewer.assert_not_called()

    def incoming_child_and_root_pin(self, *, child_metadata=None, child_task=False):
        writer = self.base / 'root-writer'
        subprocess.run(['git', 'clone', '--no-local', '-q', str(self.root_remote), str(writer)],
                       env=self.env, check=True)
        self.git(writer, 'config', 'user.name', 'Synthetic fixture')
        self.git(writer, 'config', 'user.email', 'synthetic@example.invalid')
        self.git(writer, '-c', 'protocol.file.allow=always', 'submodule', 'update', '--init', '--recursive')
        child = writer / self.child_path
        self.git(child / 'nested', 'switch', 'main')
        self.git(child / 'nested', 'branch', '--set-upstream-to=origin/main', 'main')
        self.git(child, 'config', 'user.name', 'Synthetic fixture')
        self.git(child, 'config', 'user.email', 'synthetic@example.invalid')
        self.git(child, 'switch', 'main')
        if child_metadata is not None:
            (child / 'note.md').write_text(child_metadata)
        if child_task:
            (child / '.ruwana').mkdir()
            (child / '.ruwana' / 'tasks.json').write_text('{"task": "changed"}\n')
        with (child / 'note.md').open('a') as stream:
            stream.write('\nIncoming child assertion.\n')
        self.git(child, 'add', '--', 'note.md', *(['.ruwana/tasks.json'] if child_task else []))
        self.git(child, 'commit', '-qm', 'incoming child assertion')
        incoming_child = self.git(child, 'rev-parse', 'HEAD')
        self.git(child, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(writer, 'add', '--', self.child_path)
        self.git(writer, 'commit', '-qm', 'advance incoming child pin')
        incoming_root = self.git(writer, 'rev-parse', 'HEAD')
        self.git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        return incoming_child, incoming_root

    def test_git_config_reads_registered_paths_and_urls_from_pinned_manifest(self):
        output = subprocess.run(['git', '-C', str(self.root), 'config', '--null', '--file', str(self.root / '.gitmodules'),
                                 '--get-regexp', r'^submodule\..*\.(path|url)$'],
                                env=self.env, capture_output=True, check=True).stdout
        pairs = [record.split(b'\n', 1) for record in output.split(b'\0') if record]
        values = {key.decode(): value.decode() for key, value in pairs}
        self.assertEqual(values['submodule.work/10-projects/alpha.path'], self.child_path)
        self.assertEqual(values['submodule.work/10-projects/alpha.url'], str(self.child_remote))

    def test_existing_child_fast_forward_is_validated_and_applied_before_parent(self):
        incoming_child, incoming_root = self.incoming_child_and_root_pin()
        initial_root = self.git(self.root, 'rev-parse', 'HEAD')
        initial_child = self.git(self.root / self.child_path, 'rev-parse', 'HEAD')
        wiki = Wiki(self.root)
        result = sync.pull(wiki, self.state)
        self.assertEqual(result['status'], 'needs-approval')
        self.assertEqual(result['candidate'], incoming_root)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), initial_root)
        self.assertEqual(self.git(self.root / self.child_path, 'rev-parse', 'HEAD'), initial_child)
        candidate = Path(result['candidate_worktree'])
        child_candidate = candidate / self.child_path
        self.assertEqual(self.git(child_candidate, 'rev-parse', 'HEAD'), incoming_child)
        self.assertTrue((child_candidate / 'nested').is_dir())
        self.assertEqual(self.git(candidate, 'rev-parse', f'HEAD:{self.child_path}'), incoming_child)
        Wiki(candidate).require_valid()
        from pisar import sync_review
        from pisar.operations import Runtime
        operation = sync._load(Runtime(self.state, self.root), result['operation_id'])
        requests = sync_review.requests(wiki, operation, candidate)
        self.assertEqual(len(requests), 1)
        self.assertIn(f'{self.child_path}/note.md', requests[0]['changed_paths'])
        sync.manual_review(wiki, self.state, result['operation_id'])
        sync.accept(wiki, self.state, result['operation_id'], result['revision'])
        applied = sync.apply(wiki, self.state, result['operation_id'])
        self.assertEqual(applied['status'], 'applied')
        self.assertEqual(self.git(self.root / self.child_path, 'rev-parse', 'HEAD'), incoming_child)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), incoming_root)
        self.assertEqual(self.git(self.root, 'rev-parse', f'HEAD:{self.child_path}'), incoming_child)
        self.assertEqual(self.git(self.root, 'status', '--porcelain=v1', '--ignore-submodules=none'), '')

    def test_apply_interruption_retries_exact_child_candidate(self):
        incoming_child, incoming_root = self.incoming_child_and_root_pin()
        wiki = Wiki(self.root)
        initial_root = self.git(self.root, 'rev-parse', 'HEAD')
        result = sync.pull(wiki, self.state)
        sync.manual_review(wiki, self.state, result['operation_id'])
        sync.accept(wiki, self.state, result['operation_id'], result['revision'])
        # Move the remote child beyond the accepted pin after candidate preparation.
        child_writer = self.base / 'child-writer'
        subprocess.run(['git', 'clone', '--no-local', '-q', str(self.child_remote), str(child_writer)],
                       env=self.env, check=True)
        self.git(child_writer, 'config', 'user.name', 'Synthetic fixture')
        self.git(child_writer, 'config', 'user.email', 'synthetic@example.invalid')
        with (child_writer / 'note.md').open('a') as stream:
            stream.write('\nNewer child commit after review.\n')
        self.git(child_writer, 'add', '--', 'note.md')
        self.git(child_writer, 'commit', '-qm', 'newer child after review')
        newer_child = self.git(child_writer, 'rev-parse', 'HEAD')
        self.git(child_writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')

        # Simulate an interruption immediately after the real child fast-forward.
        from pisar import sync_graph
        original = sync_graph._apply_parent
        def interrupt(*args, **kwargs):
            raise WikiError('injected parent interruption')
        sync_graph._apply_parent = interrupt
        try:
            with self.assertRaisesRegex(WikiError, 'injected parent interruption'):
                sync.apply(wiki, self.state, result['operation_id'])
        finally:
            sync_graph._apply_parent = original
        self.assertEqual(self.git(self.root / self.child_path, 'rev-parse', 'HEAD'), incoming_child)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), initial_root)
        self.assertEqual(self.git(self.root / self.child_path, 'rev-parse', 'HEAD'), incoming_child)
        resumed = sync.apply(wiki, self.state, result['operation_id'])
        self.assertEqual(resumed['status'], 'applied')
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), incoming_root)
        self.assertEqual(self.git(self.root / self.child_path, 'rev-parse', 'HEAD'), incoming_child)
        self.assertNotEqual(incoming_child, newer_child)

    def test_invalid_incoming_child_blocks_before_any_live_branch_moves(self):
        self.incoming_child_and_root_pin(child_metadata='+++\nid = [\n+++\nInvalid child.\n')
        initial_root = self.git(self.root, 'rev-parse', 'HEAD')
        initial_child = self.git(self.root / self.child_path, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(WikiError, 'candidate structural check failed'):
            sync.pull(Wiki(self.root), self.state)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), initial_root)
        self.assertEqual(self.git(self.root / self.child_path, 'rev-parse', 'HEAD'), initial_child)

    def test_incoming_child_task_history_remains_blocked_until_delivery_three_c(self):
        self.incoming_child_and_root_pin(child_task=True)
        with self.assertRaisesRegex(WikiError, 'task-validation-unavailable'):
            sync.pull(Wiki(self.root), self.state)

    def test_candidate_topology_change_is_refused(self):
        self.incoming_child_and_root_pin()
        writer = self.base / 'root-writer'
        manifest = writer / '.gitmodules'
        text = manifest.read_text()
        manifest.write_text(text.replace(self.child_path, 'work/10-projects/renamed'))
        self.git(writer, 'add', '--', '.gitmodules')
        self.git(writer, 'commit', '-qm', 'change registered submodule topology')
        self.git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        initial_root = self.git(self.root, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(WikiError, 'topology'):
            sync.pull(Wiki(self.root), self.state)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), initial_root)

    def test_candidate_only_url_change_is_refused_on_reprepare(self):
        self.incoming_child_and_root_pin()
        wiki = Wiki(self.root)
        result = sync.pull(wiki, self.state, merge=True)
        candidate = Path(result['candidate_worktree'])
        manifest = candidate / '.gitmodules'
        manifest.write_text(manifest.read_text().replace(str(self.child_remote), str(self.base / 'wrong.git')))
        self.git(candidate, 'add', '--', '.gitmodules')
        self.git(candidate, 'commit', '-qm', 'manual URL correction')
        with self.assertRaisesRegex(WikiError, 'topology|URL'):
            sync.prepare(wiki, self.state, result['operation_id'], merge=True)

    def test_candidate_nested_url_change_is_refused_on_reprepare(self):
        self.incoming_child_and_root_pin()
        wiki = Wiki(self.root)
        result = sync.pull(wiki, self.state, merge=True)
        candidate_child = Path(result['candidate_worktree']) / self.child_path
        self.git(candidate_child, 'config', 'user.name', 'Synthetic fixture')
        self.git(candidate_child, 'config', 'user.email', 'synthetic@example.invalid')
        manifest = candidate_child / '.gitmodules'
        original = manifest.read_text()
        self.assertIn(str(self.grandchild_remote), original)
        manifest.write_text(original.replace(str(self.grandchild_remote),
                                             str(self.base / 'wrong-nested.git')))
        self.git(candidate_child, 'add', '--', '.gitmodules')
        self.git(candidate_child, 'commit', '-qm', 'manual nested URL correction')
        candidate_root = Path(result['candidate_worktree'])
        self.git(candidate_root, 'add', '--', self.child_path)
        self.git(candidate_root, 'commit', '-qm', 'pin manual nested URL correction')
        with self.assertRaisesRegex(WikiError, 'topology|URL'):
            sync.prepare(wiki, self.state, result['operation_id'], merge=True)

    def test_committed_child_correction_survives_reprepare(self):
        self.incoming_child_and_root_pin()
        wiki = Wiki(self.root)
        result = sync.pull(wiki, self.state, merge=True)
        candidate = Path(result['candidate_worktree'])
        child = candidate / self.child_path
        self.git(child, 'config', 'user.name', 'Synthetic fixture')
        self.git(child, 'config', 'user.email', 'synthetic@example.invalid')
        correction = child / 'correction.txt'
        correction.write_text('Owner correction.\n')
        self.git(child, 'add', '--', 'correction.txt')
        self.git(child, 'commit', '-qm', 'owner correction')
        corrected = self.git(child, 'rev-parse', 'HEAD')
        self.git(candidate, 'add', '--', self.child_path)
        self.git(candidate, 'commit', '-qm', 'pin owner correction')
        prepared = sync.prepare(wiki, self.state, result['operation_id'], merge=True)
        self.assertEqual(prepared['status'], 'prepared')
        self.assertEqual(self.git(child, 'rev-parse', 'HEAD'), corrected)
        self.assertEqual((child / 'correction.txt').read_text(), 'Owner correction.\n')
        self.assertNotEqual(prepared['revision'], result['revision'])

    def test_nested_divergent_gitlinks_reconcile_before_parent_merge(self):
        # Local and incoming grandchild commits touch separate files. Both nested
        # gitlink histories diverge independently, so the correct result is a
        # grandchild merge followed by child and root pin reconciliation.
        self.incoming_child_and_root_pin()
        local_gc = self.root / self.child_path / 'nested'
        self.git(local_gc, 'config', 'user.name', 'Synthetic fixture')
        self.git(local_gc, 'config', 'user.email', 'synthetic@example.invalid')
        self.git(self.root / self.child_path, 'config', 'user.name', 'Synthetic fixture')
        self.git(self.root / self.child_path, 'config', 'user.email', 'synthetic@example.invalid')
        (local_gc / 'local-nested.txt').write_text('Local nested.\n')
        self.git(local_gc, 'add', '--', 'local-nested.txt')
        self.git(local_gc, 'commit', '-qm', 'local grandchild change')
        self.git(self.root / self.child_path, 'add', '--', 'nested')
        self.git(self.root / self.child_path, 'commit', '-qm', 'pin local grandchild')
        self.git(self.root, 'add', '--', self.child_path)
        self.git(self.root, 'commit', '-qm', 'pin local child')
        writer = self.base / 'root-writer'
        child = writer / self.child_path
        grandchild = child / 'nested'
        self.git(grandchild, 'config', 'user.name', 'Synthetic fixture')
        self.git(grandchild, 'config', 'user.email', 'synthetic@example.invalid')
        (grandchild / 'remote-nested.txt').write_text('Remote nested.\n')
        self.git(grandchild, 'add', '--', 'remote-nested.txt')
        self.git(grandchild, 'commit', '-qm', 'remote grandchild change')
        self.git(grandchild, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(child, 'add', '--', 'nested')
        self.git(child, 'commit', '-qm', 'pin remote grandchild')
        self.git(child, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(writer, 'add', '--', self.child_path)
        self.git(writer, 'commit', '-qm', 'pin remote child')
        self.git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        result = sync.pull(Wiki(self.root), self.state, merge=True)
        self.assertEqual(result['status'], 'needs-approval')
        candidate = Path(result['candidate_worktree'])
        candidate_gc = candidate / self.child_path / 'nested'
        merged_gc = self.git(candidate_gc, 'rev-parse', 'HEAD')
        self.assertEqual(set(self.git(candidate_gc, 'rev-list', '--parents', '-n', '1', merged_gc).split()[1:]),
                         {self.git(local_gc, 'rev-parse', 'HEAD'), self.git(grandchild, 'rev-parse', 'HEAD')})
        self.assertEqual(self.git(candidate / self.child_path, 'rev-parse', f'HEAD:nested'), merged_gc)
        self.assertEqual(self.git(candidate, 'rev-parse', f'HEAD:{self.child_path}'),
                         self.git(candidate / self.child_path, 'rev-parse', 'HEAD'))

    def test_nested_text_conflict_is_handed_off_at_grandchild(self):
        self.incoming_child_and_root_pin()
        writer = self.base / 'root-writer'
        remote_gc = writer / self.child_path / 'nested'
        self.git(remote_gc, 'config', 'user.name', 'Synthetic fixture')
        self.git(remote_gc, 'config', 'user.email', 'synthetic@example.invalid')
        (remote_gc / 'grandchild.txt').write_text('Remote conflicting nested edit.\n')
        self.git(remote_gc, 'add', '--', 'grandchild.txt')
        self.git(remote_gc, 'commit', '-qm', 'remote nested conflict')
        self.git(remote_gc, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        remote_child = writer / self.child_path
        self.git(remote_child, 'add', '--', 'nested')
        self.git(remote_child, 'commit', '-qm', 'pin remote nested conflict')
        self.git(remote_child, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(writer, 'add', '--', self.child_path)
        self.git(writer, 'commit', '-qm', 'pin remote nested conflict in root')
        self.git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')

        local_child = self.root / self.child_path
        local_gc = local_child / 'nested'
        self.git(local_gc, 'config', 'user.name', 'Synthetic fixture')
        self.git(local_gc, 'config', 'user.email', 'synthetic@example.invalid')
        (local_gc / 'grandchild.txt').write_text('Local conflicting nested edit.\n')
        self.git(local_gc, 'add', '--', 'grandchild.txt')
        self.git(local_gc, 'commit', '-qm', 'local nested conflict')
        self.git(local_child, 'config', 'user.name', 'Synthetic fixture')
        self.git(local_child, 'config', 'user.email', 'synthetic@example.invalid')
        self.git(local_child, 'add', '--', 'nested')
        self.git(local_child, 'commit', '-qm', 'pin local nested conflict')
        self.git(self.root, 'add', '--', self.child_path)
        self.git(self.root, 'commit', '-qm', 'pin local nested conflict')

        result = sync.pull(Wiki(self.root), self.state, merge=True)
        self.assertEqual(result['status'], 'conflict')
        conflict = result['child_conflicts'][0]
        self.assertEqual(conflict['path'], f'{self.child_path}/nested')
        entries = [line for line in conflict['unmerged'] if line]
        self.assertTrue(entries)
        self.assertTrue(all(line.startswith('100644 ') for line in entries))
        candidate_gc = Path(conflict['candidate'])
        self.git(candidate_gc, 'config', 'user.name', 'Synthetic fixture')
        self.git(candidate_gc, 'config', 'user.email', 'synthetic@example.invalid')
        (candidate_gc / 'grandchild.txt').write_text('Reviewed nested resolution.\n')
        self.git(candidate_gc, 'add', '--', 'grandchild.txt')
        self.git(candidate_gc, 'commit', '-qm', 'resolve nested conflict')
        resolution = self.git(candidate_gc, 'rev-parse', 'HEAD')
        (candidate_gc / 'followup.txt').write_text('Nested owner followup.\n')
        self.git(candidate_gc, 'add', '--', 'followup.txt')
        self.git(candidate_gc, 'commit', '-qm', 'nested followup correction')
        followup = self.git(candidate_gc, 'rev-parse', 'HEAD')
        prepared = sync.prepare(Wiki(self.root), self.state, result['operation_id'], merge=True)
        self.assertEqual(prepared['status'], 'prepared')
        candidate_root = Path(prepared['candidate_worktree'])
        candidate_child = candidate_root / self.child_path
        self.assertEqual(self.git(candidate_gc, 'rev-parse', 'HEAD'), followup)
        self.assertEqual((candidate_gc / 'followup.txt').read_text(), 'Nested owner followup.\n')
        self.assertEqual(self.git(candidate_child, 'rev-parse', 'HEAD:nested'), followup)
        self.assertEqual(self.git(candidate_root, 'rev-parse', f'HEAD:{self.child_path}'),
                         self.git(candidate_child, 'rev-parse', 'HEAD'))
        self.assertEqual(self.git(candidate_gc, 'rev-list', '--parents', '-n', '1', resolution).split()[1:],
                         [self.git(local_gc, 'rev-parse', 'HEAD'), self.git(remote_gc, 'rev-parse', 'HEAD')])

    def test_unrelated_parent_edit_after_child_application_is_preserved_and_blocks_retry(self):
        self.incoming_child_and_root_pin()
        wiki = Wiki(self.root)
        result = sync.pull(wiki, self.state)
        sync.manual_review(wiki, self.state, result['operation_id'])
        sync.accept(wiki, self.state, result['operation_id'], result['revision'])
        from pisar import sync_graph
        original = sync_graph._apply_parent
        sync_graph._apply_parent = lambda *args, **kwargs: (_ for _ in ()).throw(WikiError('interrupt'))
        try:
            with self.assertRaisesRegex(WikiError, 'interrupt'):
                sync.apply(wiki, self.state, result['operation_id'])
        finally:
            sync_graph._apply_parent = original
        unrelated = self.root / 'work' / '10-projects' / 'local.md'
        unrelated.write_text(document('local', 'work/local', body='Unrelated local edit.'))
        self.git(self.root, 'add', '--', 'work/10-projects/local.md')
        with self.assertRaisesRegex(WikiError, 'unexpected edits'):
            sync.apply(wiki, self.state, result['operation_id'])
        self.assertTrue(unrelated.is_file())
        self.assertIn('work/10-projects/local.md', self.git(self.root, 'status', '--porcelain=v1'))

    def test_retry_records_success_if_interrupted_after_parent_fast_forward(self):
        _, incoming_root = self.incoming_child_and_root_pin()
        wiki = Wiki(self.root)
        result = sync.pull(wiki, self.state)
        sync.manual_review(wiki, self.state, result['operation_id'])
        sync.accept(wiki, self.state, result['operation_id'], result['revision'])
        original_save = sync._save
        def interrupt_save(runtime, operation):
            if operation.get('status') == 'applied':
                raise OSError('injected journal interruption')
            return original_save(runtime, operation)
        sync._save = interrupt_save
        try:
            with self.assertRaisesRegex(OSError, 'journal interruption'):
                sync.apply(wiki, self.state, result['operation_id'])
        finally:
            sync._save = original_save
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), incoming_root)
        resumed = sync.apply(wiki, self.state, result['operation_id'])
        self.assertEqual(resumed['status'], 'applied')

    def test_retained_unpublished_child_history_is_pinned_by_candidate_root(self):
        incoming_child, _ = self.incoming_child_and_root_pin()
        child = self.root / self.child_path
        self.git(child, 'config', 'user.name', 'Synthetic fixture')
        self.git(child, 'config', 'user.email', 'synthetic@example.invalid')
        self.git(child, 'fetch', 'origin', 'main')
        self.git(child, 'merge', '--ff-only', 'FETCH_HEAD')
        (child / 'offline.md').write_text('Local child history.\n')
        self.git(child, 'add', '--', 'offline.md')
        self.git(child, 'commit', '-qm', 'offline child history')
        local_child = self.git(child, 'rev-parse', 'HEAD')
        self.git(self.root, 'add', '--', self.child_path)
        self.git(self.root, 'commit', '-qm', 'pin local child history')
        root_writer = self.base / 'root-only-writer'
        subprocess.run(['git', 'clone', '--no-local', '-q', str(self.root_remote), str(root_writer)],
                       env=self.env, check=True)
        self.git(root_writer, 'config', 'user.name', 'Synthetic fixture')
        self.git(root_writer, 'config', 'user.email', 'synthetic@example.invalid')
        (root_writer / 'incoming.md').write_text('Incoming root history.\n')
        self.git(root_writer, 'add', '--', 'incoming.md')
        self.git(root_writer, 'commit', '-qm', 'incoming root change')
        self.git(root_writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')

        result = sync.pull(Wiki(self.root), self.state, merge=True)
        candidate = Path(result['candidate_worktree'])
        self.assertEqual(self.git(candidate / self.child_path, 'rev-parse', 'HEAD'), local_child)
        self.assertEqual(self.git(candidate, 'rev-parse', f'HEAD:{self.child_path}'), local_child)
        self.assertNotEqual(local_child, incoming_child)
        self.assertEqual(self.git(candidate / self.child_path, 'status', '--porcelain=v1'), '')

    def test_divergent_child_histories_merge_and_parent_pin_names_merge(self):
        incoming_child, _ = self.incoming_child_and_root_pin()
        child = self.root / self.child_path
        self.git(child, 'config', 'user.name', 'Synthetic fixture')
        self.git(child, 'config', 'user.email', 'synthetic@example.invalid')
        (child / 'local.md').write_text('Local child assertion.\n')
        self.git(child, 'add', '--', 'local.md')
        self.git(child, 'commit', '-qm', 'local child assertion')
        local_child = self.git(child, 'rev-parse', 'HEAD')
        self.git(self.root, 'add', '--', self.child_path)
        self.git(self.root, 'commit', '-qm', 'pin local child assertion')

        result = sync.pull(Wiki(self.root), self.state, merge=True)
        if result['status'] != 'needs-approval':
            self.fail(f'candidate preparation failed: {result}')
        candidate_root = Path(result['candidate_worktree'])
        candidate_child = candidate_root / self.child_path
        child_commit = self.git(candidate_child, 'rev-parse', 'HEAD')
        parents = self.git(candidate_child, 'rev-list', '--parents', '-n', '1', child_commit).split()
        self.assertEqual(parents[1:], [local_child, incoming_child])
        self.assertEqual(self.git(candidate_root, 'rev-parse', f'HEAD:{self.child_path}'), child_commit)
        Wiki(candidate_root).require_valid()

    def test_child_content_conflict_is_handed_off_for_manual_candidate_resolution(self):
        writer = self.base / 'child-conflict-writer'
        subprocess.run(['git', 'clone', '--no-local', '-q', str(self.root_remote), str(writer)],
                       env=self.env, check=True)
        self.git(writer, 'config', 'user.name', 'Synthetic fixture')
        self.git(writer, 'config', 'user.email', 'synthetic@example.invalid')
        self.git(writer, '-c', 'protocol.file.allow=always', 'submodule', 'update', '--init', '--recursive')
        incoming = writer / self.child_path
        self.git(incoming, 'config', 'user.name', 'Synthetic fixture')
        self.git(incoming, 'config', 'user.email', 'synthetic@example.invalid')
        self.git(incoming, 'switch', 'main')
        (incoming / 'note.md').write_text(document('alpha-note', 'work/alpha', body='Remote conflicting line.'))
        self.git(incoming, 'add', '--', 'note.md')
        self.git(incoming, 'commit', '-qm', 'remote child edit')
        incoming_child = self.git(incoming, 'rev-parse', 'HEAD')
        self.git(incoming, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(writer, 'add', '--', self.child_path)
        self.git(writer, 'commit', '-qm', 'pin remote child edit')
        self.git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        local = self.root / self.child_path
        self.git(local, 'config', 'user.name', 'Synthetic fixture')
        self.git(local, 'config', 'user.email', 'synthetic@example.invalid')
        (local / 'note.md').write_text(document('alpha-note', 'work/alpha', body='Local conflicting line.'))
        self.git(local, 'add', '--', 'note.md')
        self.git(local, 'commit', '-qm', 'local child edit')
        local_child = self.git(local, 'rev-parse', 'HEAD')
        self.git(self.root, 'add', '--', self.child_path)
        self.git(self.root, 'commit', '-qm', 'pin local child edit')

        result = sync.pull(Wiki(self.root), self.state, merge=True)
        self.assertEqual(result['status'], 'conflict')
        conflict = result['child_conflicts'][0]
        candidate_child = Path(conflict['candidate'])
        self.assertEqual(conflict['path'], self.child_path)
        self.assertTrue(self.git(candidate_child, 'rev-parse', '--verify', 'MERGE_HEAD'))
        (candidate_child / 'note.md').write_text(
            document('alpha-note', 'work/alpha', body='Reviewed child resolution.'))
        self.git(candidate_child, 'add', '--', 'note.md')
        self.git(candidate_child, 'commit', '-qm', 'resolve child conflict')
        resolution = self.git(candidate_child, 'rev-parse', 'HEAD')
        (candidate_child / 'followup.txt').write_text('Followup candidate correction.\n')
        self.git(candidate_child, 'add', '--', 'followup.txt')
        self.git(candidate_child, 'commit', '-qm', 'followup child correction')
        followup = self.git(candidate_child, 'rev-parse', 'HEAD')
        prepared = sync.prepare(Wiki(self.root), self.state, result['operation_id'], merge=True)
        self.assertEqual(prepared['status'], 'prepared')
        candidate_root = Path(prepared['candidate_worktree'])
        candidate_commit = self.git(candidate_child, 'rev-parse', 'HEAD')
        self.assertEqual(candidate_commit, followup)
        self.assertEqual((candidate_child / 'followup.txt').read_text(), 'Followup candidate correction.\n')
        self.assertEqual(self.git(candidate_root, 'rev-parse', f'HEAD:{self.child_path}'), candidate_commit)
        self.assertEqual(self.git(candidate_child, 'rev-list', '--parents', '-n', '1', candidate_commit).split()[1:],
                         [resolution])
        self.assertEqual(self.git(candidate_child, 'rev-list', '--parents', '-n', '1', resolution).split()[1:],
                         [local_child, incoming_child])

    def test_changed_grandchild_applies_child_first_and_finishes_clean(self):
        incoming_child, _ = self.incoming_child_and_root_pin()
        writer = self.base / 'root-writer'
        child = writer / self.child_path
        grandchild = child / 'nested'
        self.git(grandchild, 'config', 'user.name', 'Synthetic fixture')
        self.git(grandchild, 'config', 'user.email', 'synthetic@example.invalid')
        (grandchild / 'grandchild.txt').write_text('Updated nested fixture.\n')
        self.git(grandchild, 'add', '--', 'grandchild.txt')
        self.git(grandchild, 'commit', '-qm', 'incoming grandchild update')
        self.git(grandchild, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(child, 'add', '--', 'nested')
        self.git(child, 'commit', '-qm', 'advance grandchild pin')
        incoming_child = self.git(child, 'rev-parse', 'HEAD')
        self.git(child, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(writer, 'add', '--', self.child_path)
        self.git(writer, 'commit', '-qm', 'advance child pin')
        incoming_root = self.git(writer, 'rev-parse', 'HEAD')
        self.git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        wiki = Wiki(self.root)
        result = sync.pull(wiki, self.state)
        sync.manual_review(wiki, self.state, result['operation_id'])
        sync.accept(wiki, self.state, result['operation_id'], result['revision'])
        applied = sync.apply(wiki, self.state, result['operation_id'])
        self.assertEqual(applied['status'], 'applied')
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), incoming_root)
        self.assertEqual(self.git(self.root / self.child_path, 'rev-parse', 'HEAD'), incoming_child)
        self.assertEqual(self.git(self.root / self.child_path / 'nested', 'rev-parse', 'HEAD'),
                         self.git(self.root / self.child_path, 'rev-parse', 'HEAD:nested'))
        self.assertEqual(self.git(self.root, 'status', '--porcelain=v1', '--ignore-submodules=none'), '')

    def test_retry_after_grandchild_fast_forward_resumes_parent_graph(self):
        self.incoming_child_and_root_pin()
        writer = self.base / 'root-writer'
        child = writer / self.child_path
        grandchild = child / 'nested'
        self.git(grandchild, 'config', 'user.name', 'Synthetic fixture')
        self.git(grandchild, 'config', 'user.email', 'synthetic@example.invalid')
        (grandchild / 'grandchild.txt').write_text('Interrupted nested update.\n')
        self.git(grandchild, 'add', '--', 'grandchild.txt')
        self.git(grandchild, 'commit', '-qm', 'incoming grandchild update')
        self.git(grandchild, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(child, 'add', '--', 'nested')
        self.git(child, 'commit', '-qm', 'advance grandchild pin')
        self.git(child, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(writer, 'add', '--', self.child_path)
        self.git(writer, 'commit', '-qm', 'advance child pin')
        self.git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')

        wiki = Wiki(self.root)
        result = sync.pull(wiki, self.state)
        sync.manual_review(wiki, self.state, result['operation_id'])
        sync.accept(wiki, self.state, result['operation_id'], result['revision'])
        from pisar import sync_graph
        original = sync_graph._apply_child
        def interrupt_at_parent(item, repo, candidate):
            if item['path'] == self.child_path:
                raise WikiError('injected after grandchild FF')
            return original(item, repo, candidate)
        sync_graph._apply_child = interrupt_at_parent
        try:
            with self.assertRaisesRegex(WikiError, 'injected after grandchild FF'):
                sync.apply(wiki, self.state, result['operation_id'])
        finally:
            sync_graph._apply_child = original
        grandchild_candidate = self.git(Path(result['candidate_worktree']) / self.child_path / 'nested',
                                        'rev-parse', 'HEAD')
        self.assertEqual(self.git(self.root / self.child_path / 'nested', 'rev-parse', 'HEAD'),
                         grandchild_candidate)
        original_save = sync._save
        def interrupt_completed_save(runtime, operation):
            if operation.get('status') == 'applied':
                raise OSError('injected completed graph journal interruption')
            return original_save(runtime, operation)
        sync._save = interrupt_completed_save
        try:
            with self.assertRaisesRegex(OSError, 'completed graph journal interruption'):
                sync.apply(wiki, self.state, result['operation_id'])
        finally:
            sync._save = original_save
        self.assertEqual(self.git(self.root, 'status', '--porcelain=v1', '--ignore-submodules=none'), '')
        self.assertEqual(sync.apply(wiki, self.state, result['operation_id'])['status'], 'applied')

    def test_space_submodule_path_applies_and_retries_exactly(self):
        old_path = self.child_path
        self.child_path = 'work/10-projects/alpha space "quoted"'
        self.git(self.root, 'mv', old_path, self.child_path)
        self.git(self.root, 'add', '--', '.gitmodules', self.child_path)
        self.git(self.root, 'commit', '-qm', 'move submodule to path with space')
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.incoming_child_and_root_pin()
        wiki = Wiki(self.root)
        result = sync.pull(wiki, self.state)
        sync.manual_review(wiki, self.state, result['operation_id'])
        sync.accept(wiki, self.state, result['operation_id'], result['revision'])
        from pisar import sync_graph
        original = sync_graph._apply_parent
        sync_graph._apply_parent = lambda *args, **kwargs: (_ for _ in ()).throw(WikiError('interrupt before root FF'))
        try:
            with self.assertRaisesRegex(WikiError, 'interrupt before root FF'):
                sync.apply(wiki, self.state, result['operation_id'])
        finally:
            sync_graph._apply_parent = original
        applied = sync.apply(wiki, self.state, result['operation_id'])
        self.assertEqual(applied['status'], 'applied')
        self.assertEqual(self.git(self.root, 'status', '--porcelain=v1', '--ignore-submodules=none'), '')

    def test_nested_retained_pin_fixup_propagates_to_root_candidate(self):
        self.incoming_child_and_root_pin()
        wiki = Wiki(self.root)
        live_child = self.root / self.child_path
        live_grandchild = live_child / 'nested'
        for repo in (live_child, live_grandchild):
            self.git(repo, 'config', 'user.name', 'Synthetic fixture')
            self.git(repo, 'config', 'user.email', 'synthetic@example.invalid')
        first = sync.pull(wiki, self.state)
        sync.manual_review(wiki, self.state, first['operation_id'])
        sync.accept(wiki, self.state, first['operation_id'], first['revision'])
        sync.apply(wiki, self.state, first['operation_id'])
        writer = self.base / 'root-writer'
        child = writer / self.child_path
        grandchild = child / 'nested'
        ancestor = self.git(grandchild, 'rev-parse', 'HEAD')
        self.git(grandchild, 'config', 'user.name', 'Synthetic fixture')
        self.git(grandchild, 'config', 'user.email', 'synthetic@example.invalid')
        (grandchild / 'grandchild.txt').write_text('Retained nested descendant.\n')
        self.git(grandchild, 'add', '--', 'grandchild.txt')
        self.git(grandchild, 'commit', '-qm', 'nested descendant')
        retained = self.git(grandchild, 'rev-parse', 'HEAD')
        self.git(grandchild, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(child, 'add', '--', 'nested')
        self.git(child, 'commit', '-qm', 'pin nested descendant')
        self.git(child, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(writer, 'add', '--', self.child_path)
        self.git(writer, 'commit', '-qm', 'pin child descendant')
        self.git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        second = sync.pull(wiki, self.state)
        sync.manual_review(wiki, self.state, second['operation_id'])
        sync.accept(wiki, self.state, second['operation_id'], second['revision'])
        sync.apply(wiki, self.state, second['operation_id'])

        self.git(grandchild, 'switch', '--detach', ancestor)
        self.git(child, 'add', '--', 'nested')
        self.git(child, 'commit', '-qm', 'incoming older nested pin')
        self.git(child, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.git(writer, 'add', '--', self.child_path)
        self.git(writer, 'commit', '-qm', 'incoming child with older nested pin')
        incoming_root = self.git(writer, 'rev-parse', 'HEAD')
        self.git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')

        result = sync.pull(wiki, self.state)
        self.assertEqual(result['status'], 'needs-approval')
        candidate = Path(result['candidate_worktree'])
        candidate_child = candidate / self.child_path
        self.assertEqual(self.git(candidate_child / 'nested', 'rev-parse', 'HEAD'), retained)
        candidate_child_head = self.git(candidate_child, 'rev-parse', 'HEAD')
        self.assertEqual(self.git(candidate, 'rev-parse', f'HEAD:{self.child_path}'), candidate_child_head)
        self.assertEqual(self.git(candidate_child, 'rev-parse', 'HEAD:nested'), retained)
        self.assertEqual(self.git(candidate, 'rev-list', '--parents', '-n', '1', 'HEAD').split()[1],
                         incoming_root)
        Wiki(candidate).require_valid()
        sync.manual_review(wiki, self.state, result['operation_id'])
        sync.accept(wiki, self.state, result['operation_id'], result['revision'])
        self.assertEqual(sync.apply(wiki, self.state, result['operation_id'])['status'], 'applied')

    def test_retry_after_root_ff_refuses_foreign_child_mismatch(self):
        _, incoming_root = self.incoming_child_and_root_pin()
        wiki = Wiki(self.root)
        result = sync.pull(wiki, self.state)
        sync.manual_review(wiki, self.state, result['operation_id'])
        sync.accept(wiki, self.state, result['operation_id'], result['revision'])
        original_save = sync._save
        def interrupt_save(runtime, operation):
            if operation.get('status') == 'applied':
                raise OSError('injected journal interruption')
            return original_save(runtime, operation)
        sync._save = interrupt_save
        try:
            with self.assertRaisesRegex(OSError, 'journal interruption'):
                sync.apply(wiki, self.state, result['operation_id'])
        finally:
            sync._save = original_save
        child = self.root / self.child_path
        self.git(child, 'config', 'user.name', 'Synthetic fixture')
        self.git(child, 'config', 'user.email', 'synthetic@example.invalid')
        (child / 'foreign.md').write_text('Foreign child commit.\n')
        self.git(child, 'add', '--', 'foreign.md')
        self.git(child, 'commit', '-qm', 'foreign child commit')
        foreign = self.git(child, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(WikiError, 'live graph changed|unexpected edits'):
            sync.apply(wiki, self.state, result['operation_id'])
        self.assertEqual(self.git(child, 'rev-parse', 'HEAD'), foreign)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), incoming_root)

    def test_incoming_url_change_is_refused_as_setup_change(self):
        self.incoming_child_and_root_pin()
        writer = self.base / 'root-writer'
        manifest = writer / '.gitmodules'
        manifest.write_text(manifest.read_text().replace(str(self.child_remote), '/synthetic/changed.git'))
        self.git(writer, 'add', '--', '.gitmodules')
        self.git(writer, 'commit', '-qm', 'change child URL')
        self.git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        with self.assertRaisesRegex(WikiError, 'topology|URL|registration'):
            sync.pull(Wiki(self.root), self.state)

    def test_assume_unchanged_root_file_blocks_before_child_application(self):
        self.incoming_child_and_root_pin()
        wiki = Wiki(self.root)
        result = sync.pull(wiki, self.state)
        sync.manual_review(wiki, self.state, result['operation_id'])
        sync.accept(wiki, self.state, result['operation_id'], result['revision'])
        initial_child = self.git(self.root / self.child_path, 'rev-parse', 'HEAD')
        self.git(self.root, 'update-index', '--assume-unchanged', 'work/.domain.toml')
        with (self.root / 'work/.domain.toml').open('a') as stream:
            stream.write('\n# hidden local change\n')
        with self.assertRaisesRegex(WikiError, 'unsupported|assume|skip-worktree|sparse|dirty|edits'):
            sync.apply(wiki, self.state, result['operation_id'])
        self.assertEqual(self.git(self.root / self.child_path, 'rev-parse', 'HEAD'), initial_child)


if __name__ == '__main__':
    unittest.main()
