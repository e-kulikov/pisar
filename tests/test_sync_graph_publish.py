"""Real-Git publication graph and bootstrap regressions."""
import subprocess
import tempfile
from pathlib import Path
import unittest

from pisar import sync_graph
from pisar.safety import WikiError


class SyncGraphPublishTests(unittest.TestCase):
    def setUp(self):
        from .test_sync_submodules import SyncSubmoduleTests
        self.fixture = SyncSubmoduleTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture

    def _candidate_graph(self):
        self.f.incoming_child_and_root_pin()
        from pisar.spaces import Wiki
        from pisar import sync
        result = sync.pull(Wiki(self.f.root), self.f.state)
        from pisar.operations import Runtime
        op = sync._load(Runtime(self.f.state, self.f.root), result['operation_id'])
        entries = []
        candidate = Path(result['candidate_worktree'])
        for item in op['graph']:
            entry = dict(item)
            entry['commit'] = entry['candidate']
            entry['candidate'] = str(candidate if item['path'] == '.' else candidate / item['path'])
            entry['repo'] = self.f.root if item['path'] == '.' else self.f.root / item['path']
            entries.append(entry)
        return sync_graph.CandidateGraph(candidate, tuple(entries)), op

    def test_exact_destination_reachability_required_for_every_new_pin(self):
        graph, op = self._candidate_graph()
        targets = sync_graph.required_publication(graph, {
            '.': (op['remote'], op['target_branch']),
            'work/10-projects/alpha': ('origin', 'main'),
            'work/10-projects/alpha/nested': ('origin', 'main'),
        })
        self.assertEqual([target.path for target in targets],
                         ['work/10-projects/alpha/nested', 'work/10-projects/alpha', '.'])

    def test_pin_at_publication_destination_but_missing_from_bootstrap_source_is_refused(self):
        graph, op = self._candidate_graph()
        empty_source = self.f.base / 'empty-bootstrap.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(empty_source)], env=self.f.env, check=True)
        destinations = {'.': (op['remote'], op['target_branch']),
                        'work/10-projects/alpha': ('origin', 'main'),
                        'work/10-projects/alpha/nested': ('origin', 'main')}
        with self.assertRaisesRegex(WikiError, 'bootstrap-source-unavailable|bootstrap source'):
            sync_graph.required_publication(graph, destinations,
                {'work/10-projects/alpha': {'url': str(empty_source), 'branch': 'main'}})
        targets = sync_graph.required_publication(graph, destinations,
            {'work/10-projects/alpha': {'url': str(self.f.child_remote), 'branch': 'main'},
             'work/10-projects/alpha/nested': {'url': str(self.f.grandchild_remote), 'branch': 'main'}})
        self.assertEqual(targets[-1].path, '.')

    def test_missing_pin_at_destination_blocks_publication(self):
        graph, op = self._candidate_graph()
        child = next(entry for entry in graph.repositories if entry['path'] != '.')
        self.f.git(child['repo'], 'remote', 'remove', 'origin')
        with self.assertRaisesRegex(WikiError, 'missing|unreachable|publication'):
            sync_graph.required_publication(graph, {'.': (op['remote'], op['target_branch']),
                                                     'work/10-projects/alpha': ('missing-destination', 'main'),
                                                     'work/10-projects/alpha/nested': ('origin', 'main')})

    def test_pin_available_only_in_wrong_remote_is_not_accepted(self):
        graph, op = self._candidate_graph()
        child = next(entry for entry in graph.repositories if entry['path'] != '.')
        self.f.git(child['repo'], 'remote', 'add', 'wrong', str(self.f.child_remote))
        # The configured destination must be selected by the caller, not any remote with the object.
        with self.assertRaisesRegex(WikiError, 'missing|unreachable|publication'):
            sync_graph.required_publication(graph, {'.': (op['remote'], op['target_branch']),
                                                     'work/10-projects/alpha': ('missing-destination', 'main'),
                                                     'work/10-projects/alpha/nested': ('origin', 'main')})

    def test_historical_intermediate_pin_is_required_even_when_final_pin_is_available(self):
        base = self.f.base / 'history-check'
        child, child_remote = base / 'child', base / 'child.git'
        root, root_remote = base / 'root', base / 'root.git'
        for remote in (child_remote, root_remote):
            subprocess.run(['git', 'init', '--bare', '-q', str(remote)], env=self.f.env, check=True)
        subprocess.run(['git', 'init', '-q', str(child)], env=self.f.env, check=True)
        self.f.git(child, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(child, 'config', 'user.email', 'synthetic@example.invalid')
        (child / 'base').write_text('base\n')
        self.f.git(child, 'add', '--', 'base')
        self.f.git(child, 'commit', '-qm', 'child base')
        base_oid = self.f.git(child, 'rev-parse', 'HEAD')
        self.f.git(child, 'switch', '--orphan', 'historical')
        (child / 'history').write_text('historical pin\n')
        self.f.git(child, 'add', '--', 'history')
        self.f.git(child, 'commit', '-qm', 'intermediate child pin')
        intermediate = self.f.git(child, 'rev-parse', 'HEAD')
        self.f.git(child, 'switch', '--orphan', 'main')
        (child / 'final').write_text('final pin\n')
        self.f.git(child, 'add', '--', 'final')
        self.f.git(child, 'commit', '-qm', 'final child pin')
        final = self.f.git(child, 'rev-parse', 'HEAD')
        self.f.git(child, 'remote', 'add', 'origin', str(child_remote))
        self.f.git(child, 'push', '-q', '-u', 'origin', 'HEAD:refs/heads/main')
        self.f.git(child_remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        subprocess.run(['git', 'init', '-q', str(root)], env=self.f.env, check=True)
        self.f.git(root, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(root, 'config', 'user.email', 'synthetic@example.invalid')
        (root / 'readme').write_text('baseline\n')
        self.f.git(root, 'add', '--', 'readme')
        self.f.git(root, 'commit', '-qm', 'root baseline')
        root_base = self.f.git(root, 'rev-parse', 'HEAD')
        (root / '.gitmodules').write_text(f'[submodule "child"]\n path = child\n url = {child_remote}\n')
        self.f.git(root, 'add', '--', '.gitmodules')
        self.f.git(root, 'update-index', '--add', '--cacheinfo', f'160000,{intermediate},child')
        self.f.git(root, 'commit', '-qm', 'record intermediate child pin')
        self.f.git(root, 'update-index', '--cacheinfo', f'160000,{final},child')
        self.f.git(root, 'commit', '-qm', 'record final child pin')
        root_candidate = self.f.git(root, 'rev-parse', 'HEAD')
        self.f.git(root, 'remote', 'add', 'origin', str(root_remote))
        self.f.git(root, 'push', '-q', '-u', 'origin', root_base + ':refs/heads/main')
        self.f.git(root_remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        graph = sync_graph.CandidateGraph(root, (
            dict(path='.', candidate=str(root), commit=root_candidate, repo=root),
            dict(path='child', candidate=str(child), commit=final, repo=child),
        ))
        with self.assertRaisesRegex(WikiError, 'historical|unavailable|publication'):
            sync_graph.required_publication(graph, {'.': ('origin', 'main'), 'child': ('origin', 'main')})

    def test_parent_publish_checks_and_observes_child_destinations_first(self):
        self.f.incoming_child_and_root_pin()
        from pisar import sync
        from pisar.operations import Runtime
        from pisar.spaces import Wiki
        wiki = Wiki(self.f.root)
        prepared = sync.pull(wiki, self.f.state)
        sync.manual_review(wiki, self.f.state, prepared['operation_id'])
        sync.accept(wiki, self.f.state, prepared['operation_id'], prepared['revision'])
        sync.apply(wiki, self.f.state, prepared['operation_id'])
        result = sync.publish(wiki, self.f.state, prepared['operation_id'])
        self.assertEqual(result['status'], 'published')
        operation = sync._load(Runtime(self.f.state, self.f.root), prepared['operation_id'])
        self.assertEqual(operation['published_graph'], [
            'work/10-projects/alpha/nested', 'work/10-projects/alpha'])
        self.assertEqual(self.f.git(self.f.root_remote, 'rev-parse', 'refs/heads/main'), operation['candidate'])

    def test_child_push_survives_root_rejection_after_remote_advance(self):
        child = self.f.root / self.f.child_path
        self.f.git(child, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(child, 'config', 'user.email', 'synthetic@example.invalid')
        (child / 'local-only.md').write_text('Unpublished local child history.\n')
        self.f.git(child, 'add', '--', 'local-only.md')
        self.f.git(child, 'commit', '-qm', 'local child work')
        local_child = self.f.git(child, 'rev-parse', 'HEAD')
        self.f.git(self.f.root, 'add', '--', self.f.child_path)
        self.f.git(self.f.root, 'commit', '-qm', 'pin local child work')
        self.f.incoming_child_and_root_pin()
        from pisar import sync
        from pisar.safety import WikiError
        from pisar.spaces import Wiki
        wiki = Wiki(self.f.root)
        prepared = sync.pull(wiki, self.f.state, merge=True)
        sync.manual_review(wiki, self.f.state, prepared['operation_id'])
        sync.accept(wiki, self.f.state, prepared['operation_id'], prepared['revision'])
        sync.apply(wiki, self.f.state, prepared['operation_id'])
        from pisar.operations import Runtime
        operation = sync._load(Runtime(self.f.state, self.f.root), prepared['operation_id'])
        child_candidate = next(row['candidate'] for row in operation['graph'] if row['path'] == self.f.child_path)
        # The local child merge candidate must be published before attempting the root.
        self.assertNotEqual(child_candidate, local_child)
        writer = self.f.base / 'concurrent-root-writer'
        subprocess.run(['git', 'clone', '--no-local', '-q', str(self.f.root_remote), str(writer)],
                       env=self.f.env, check=True)
        self.f.git(writer, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(writer, 'config', 'user.email', 'synthetic@example.invalid')
        (writer / 'concurrent.md').write_text('Concurrent destination advance.\n')
        self.f.git(writer, 'add', '--', 'concurrent.md')
        self.f.git(writer, 'commit', '-qm', 'advance root after review')
        original_publish_children = sync_graph.publish_children
        def publish_then_advance(operation, graph, save, bootstrap_map=None):
            result = original_publish_children(operation, graph, save, bootstrap_map)
            self.f.git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
            return result
        sync_graph.publish_children = publish_then_advance
        try:
            with self.assertRaisesRegex(WikiError, 'remote-advanced'):
                sync.publish(wiki, self.f.state, prepared['operation_id'])
        finally:
            sync_graph.publish_children = original_publish_children
        operation = sync._load(Runtime(self.f.state, self.f.root), prepared['operation_id'])
        self.assertEqual(self.f.git(self.f.child_remote, 'rev-parse', 'refs/heads/main'), child_candidate,
                         (operation.get('published_graph'), [(row['path'], row['candidate'])
                                                             for row in operation['graph']]))
        self.assertEqual(self.f.git(self.f.root_remote, 'rev-parse', 'refs/heads/main'),
                         self.f.git(writer, 'rev-parse', 'HEAD'))

    def test_ambiguous_child_push_success_is_observed_and_root_can_publish(self):
        child = self.f.root / self.f.child_path
        self.f.git(child, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(child, 'config', 'user.email', 'synthetic@example.invalid')
        (child / 'local-only.md').write_text('Unpublished local child history.\n')
        self.f.git(child, 'add', '--', 'local-only.md')
        self.f.git(child, 'commit', '-qm', 'local child work')
        self.f.git(self.f.root, 'add', '--', self.f.child_path)
        self.f.git(self.f.root, 'commit', '-qm', 'pin local child work')
        self.f.incoming_child_and_root_pin()
        from pisar import sync
        from pisar.spaces import Wiki
        wiki = Wiki(self.f.root)
        prepared = sync.pull(wiki, self.f.state, merge=True)
        sync.manual_review(wiki, self.f.state, prepared['operation_id'])
        sync.accept(wiki, self.f.state, prepared['operation_id'], prepared['revision'])
        sync.apply(wiki, self.f.state, prepared['operation_id'])
        real_run = subprocess.run
        def lose_child_push_ack(command, *args, **kwargs):
            result = real_run(command, *args, **kwargs)
            if isinstance(command, (tuple, list)) and 'push' in command and str(child) in command:
                return subprocess.CompletedProcess(command, 1, result.stdout, b'synthetic lost acknowledgement')
            return result
        original_run = sync_graph.subprocess.run
        sync_graph.subprocess.run = lose_child_push_ack
        try:
            result = sync.publish(wiki, self.f.state, prepared['operation_id'])
        finally:
            sync_graph.subprocess.run = original_run
        self.assertEqual(result['status'], 'published')
        self.assertEqual(self.f.git(self.f.root_remote, 'rev-parse', 'refs/heads/main'),
                         result['commit'])
        self.assertEqual(self.f.git(self.f.child_remote, 'rev-parse', 'refs/heads/main'),
                         self.f.git(child, 'rev-parse', 'HEAD'))

    def test_fresh_clone_bootstraps_exact_nested_gitlinks_with_empty_runtime(self):
        root_tip = self.f.git(self.f.root, 'rev-parse', 'HEAD')
        fresh = self.f.base / 'fresh-machine'
        subprocess.run(['git', 'clone', '--no-recurse-submodules', '--', str(self.f.root_remote), str(fresh)],
                       env=self.f.env, check=True, capture_output=True)
        child = fresh / self.f.child_path
        subprocess.run(['git', '-C', str(fresh), '-c', 'protocol.file.allow=always',
                        'submodule', 'update', '--init', '--checkout', '--', self.f.child_path],
                       env=self.f.env, check=True, capture_output=True)
        self.assertEqual(self.f.git(child, 'rev-parse', 'HEAD'), self.f.git(fresh, 'rev-parse', f'HEAD:{self.f.child_path}'))
        subprocess.run(['git', '-C', str(child), 'submodule', 'init', '--', 'nested'],
                       env=self.f.env, check=True, capture_output=True)
        subprocess.run(['git', '-C', str(child), '-c', 'protocol.file.allow=always',
                        'submodule', 'update', '--init', '--checkout', '--', 'nested'],
                       env=self.f.env, check=True, capture_output=True)
        self.assertEqual(self.f.git(child / 'nested', 'rev-parse', 'HEAD'),
                         self.f.git(child, 'rev-parse', 'HEAD:nested'))
        self.assertEqual(self.f.git(fresh, 'rev-parse', 'HEAD'), root_tip)
        self.assertFalse((self.f.base / 'empty-runtime').exists())

    def test_stale_default_submodule_url_fails_fresh_bootstrap(self):
        writer = self.f.base / 'stale-writer'
        subprocess.run(['git', 'clone', '--no-recurse-submodules', '--', str(self.f.root_remote), str(writer)],
                       env=self.f.env, check=True, capture_output=True)
        self.f.git(writer, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(writer, 'config', 'user.email', 'synthetic@example.invalid')
        manifest = writer / '.gitmodules'
        manifest.write_text(manifest.read_text().replace(str(self.f.child_remote), str(self.f.base / 'missing.git')))
        self.f.git(writer, 'add', '--', '.gitmodules')
        self.f.git(writer, 'commit', '-qm', 'stale child source')
        self.f.git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        fresh = self.f.base / 'fresh-stale'
        subprocess.run(['git', 'clone', '--no-recurse-submodules', '--', str(self.f.root_remote), str(fresh)],
                       env=self.f.env, check=True, capture_output=True)
        subprocess.run(['git', '-C', str(fresh), 'submodule', 'init', '--', self.f.child_path],
                       env=self.f.env, check=True, capture_output=True)
        failed = subprocess.run(['git', '-C', str(fresh), '-c', 'protocol.file.allow=always',
                                 'submodule', 'update', '--init', '--checkout', '--', self.f.child_path],
                                env=self.f.env, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)

    def test_explicit_bootstrap_map_overrides_stale_default_source(self):
        writer = self.f.base / 'mapped-writer'
        subprocess.run(['git', 'clone', '--no-recurse-submodules', '--', str(self.f.root_remote), str(writer)],
                       env=self.f.env, check=True, capture_output=True)
        self.f.git(writer, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(writer, 'config', 'user.email', 'synthetic@example.invalid')
        manifest = writer / '.gitmodules'
        manifest.write_text(manifest.read_text().replace(str(self.f.child_remote), str(self.f.base / 'stale.git')))
        self.f.git(writer, 'add', '--', '.gitmodules')
        self.f.git(writer, 'commit', '-qm', 'stale default with external bootstrap map')
        self.f.git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        fresh = self.f.base / 'fresh-mapped'
        subprocess.run(['git', 'clone', '--no-recurse-submodules', '--', str(self.f.root_remote), str(fresh)],
                       env=self.f.env, check=True, capture_output=True)
        subprocess.run(['git', '-C', str(fresh), 'submodule', 'init', '--', self.f.child_path],
                       env=self.f.env, check=True, capture_output=True)
        self.f.git(fresh, 'config', f'submodule.{self.f.child_path}.url', str(self.f.child_remote))
        subprocess.run(['git', '-C', str(fresh), '-c', 'protocol.file.allow=always',
                        'submodule', 'update', '--init', '--checkout', '--', self.f.child_path],
                       env=self.f.env, check=True, capture_output=True)
        child = fresh / self.f.child_path
        subprocess.run(['git', '-C', str(child), 'submodule', 'init', '--', 'nested'],
                       env=self.f.env, check=True, capture_output=True)
        subprocess.run(['git', '-C', str(child), '-c', 'protocol.file.allow=always',
                        'submodule', 'update', '--init', '--checkout', '--', 'nested'],
                       env=self.f.env, check=True, capture_output=True)
        self.assertEqual(self.f.git(child, 'rev-parse', 'HEAD'), self.f.git(fresh, 'rev-parse', f'HEAD:{self.f.child_path}'))
        self.assertEqual(self.f.git(child / 'nested', 'rev-parse', 'HEAD'),
                         self.f.git(child, 'rev-parse', 'HEAD:nested'))

    def test_relative_nested_url_is_resolved_by_git_during_bootstrap(self):
        child_writer = self.f.base / 'relative-child-writer'
        subprocess.run(['git', 'clone', '--no-local', '-q', str(self.f.child_remote), str(child_writer)],
                       env=self.f.env, check=True)
        self.f.git(child_writer, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(child_writer, 'config', 'user.email', 'synthetic@example.invalid')
        manifest = child_writer / '.gitmodules'
        manifest.write_text(manifest.read_text().replace(str(self.f.grandchild_remote), '../grandchild.git'))
        self.f.git(child_writer, 'add', '--', '.gitmodules')
        self.f.git(child_writer, 'commit', '-qm', 'use relative nested source')
        nested_child_tip = self.f.git(child_writer, 'rev-parse', 'HEAD')
        self.f.git(child_writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')

        root_writer = self.f.base / 'relative-root-writer'
        subprocess.run(['git', 'clone', '--no-recurse-submodules', '--', str(self.f.root_remote), str(root_writer)],
                       env=self.f.env, check=True, capture_output=True)
        self.f.git(root_writer, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(root_writer, 'config', 'user.email', 'synthetic@example.invalid')
        subprocess.run(['git', '-C', str(root_writer), '-c', 'protocol.file.allow=always',
                        'submodule', 'update', '--init', '--checkout', '--', self.f.child_path],
                       env=self.f.env, check=True, capture_output=True)
        child = root_writer / self.f.child_path
        self.f.git(child, 'fetch', 'origin', 'main')
        self.f.git(child, 'switch', '--detach', nested_child_tip)
        self.f.git(root_writer, 'add', '--', self.f.child_path)
        self.f.git(root_writer, 'commit', '-qm', 'pin relative nested source')
        self.f.git(root_writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')

        fresh = self.f.base / 'fresh-relative'
        subprocess.run(['git', 'clone', '--no-recurse-submodules', '--', str(self.f.root_remote), str(fresh)],
                       env=self.f.env, check=True, capture_output=True)
        subprocess.run(['git', '-C', str(fresh), 'submodule', 'init', '--', self.f.child_path],
                       env=self.f.env, check=True, capture_output=True)
        subprocess.run(['git', '-C', str(fresh), '-c', 'protocol.file.allow=always',
                        'submodule', 'update', '--init', '--checkout', '--', self.f.child_path],
                       env=self.f.env, check=True, capture_output=True)
        child = fresh / self.f.child_path
        subprocess.run(['git', '-C', str(child), 'submodule', 'init', '--', 'nested'],
                       env=self.f.env, check=True, capture_output=True)
        subprocess.run(['git', '-C', str(child), '-c', 'protocol.file.allow=always',
                        'submodule', 'update', '--init', '--checkout', '--', 'nested'],
                       env=self.f.env, check=True, capture_output=True)
        self.assertEqual(self.f.git(child, 'rev-parse', 'HEAD'), nested_child_tip)
        self.assertEqual(self.f.git(child / 'nested', 'rev-parse', 'HEAD'),
                         self.f.git(child, 'rev-parse', 'HEAD:nested'))

