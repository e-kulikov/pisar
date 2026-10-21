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


if __name__ == '__main__':
    unittest.main()
