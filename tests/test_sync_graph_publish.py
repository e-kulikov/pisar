"""Real-Git publication graph and bootstrap regressions."""
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

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
        self.f.git(child, 'remote', 'add', 'origin', str(child_remote))
        self.f.git(child, 'push', '-q', '-u', 'origin', 'HEAD:refs/heads/main')
        self.f.git(child_remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.f.git(child, 'switch', '--orphan', 'historical')
        (child / 'history').write_text('historical pin\n')
        self.f.git(child, 'add', '--', 'history')
        self.f.git(child, 'commit', '-qm', 'intermediate child pin')
        intermediate = self.f.git(child, 'rev-parse', 'HEAD')
        self.f.git(child, 'switch', '-c', 'main', base_oid)
        (child / 'final').write_text('final pin\n')
        self.f.git(child, 'add', '--', 'final')
        self.f.git(child, 'commit', '-qm', 'final child pin')
        final = self.f.git(child, 'rev-parse', 'HEAD')
        self.f.git(child, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        subprocess.run(['git', 'init', '-q', str(root)], env=self.f.env, check=True)
        self.f.git(root, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(root, 'config', 'user.email', 'synthetic@example.invalid')
        (root / 'readme').write_text('baseline\n')
        (root / '.gitmodules').write_text(f'[submodule "child"]\n path = child\n url = {child_remote}\n')
        self.f.git(root, 'add', '--', 'readme', '.gitmodules')
        self.f.git(root, 'update-index', '--add', '--cacheinfo', f'160000,{base_oid},child')
        self.f.git(root, 'commit', '-qm', 'root baseline with child registration')
        root_base = self.f.git(root, 'rev-parse', 'HEAD')
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
        with self.assertRaisesRegex(WikiError, 'historical|unavailable|publication|topology'):
            sync_graph.required_publication(graph, {'.': ('origin', 'main'), 'child': ('origin', 'main')})

    def test_reverted_historical_submodule_url_change_is_refused(self):
        graph, op = self._candidate_graph()
        root = self.f.root
        manifest = root / '.gitmodules'
        original = manifest.read_bytes()
        changed = original.replace(str(self.f.child_remote).encode(),
                                   str(self.f.base / 'missing-history.git').encode())
        manifest.write_bytes(changed)
        self.f.git(root, 'add', '--', '.gitmodules')
        self.f.git(root, 'commit', '-qm', 'historical broken child URL')
        manifest.write_bytes(original)
        self.f.git(root, 'add', '--', '.gitmodules')
        self.f.git(root, 'commit', '-qm', 'restore child URL')
        candidate = self.f.git(root, 'rev-parse', 'HEAD')
        root_entry = next(item for item in graph.repositories if item['path'] == '.')
        root_entry['commit'] = candidate
        with self.assertRaisesRegex(WikiError, 'topology|URL|bootstrap|historical'):
            sync_graph.required_publication(graph, {
                '.': (op['remote'], op['target_branch']),
                self.f.child_path: ('origin', 'main'),
                f'{self.f.child_path}/nested': ('origin', 'main'),
            })

    def test_fetch_with_changed_bootstrap_map_does_not_reuse_old_operation(self):
        from pisar import sync
        from pisar.operations import Runtime
        from pisar.spaces import Wiki
        first = {'work/10-projects/alpha': {'url': str(self.f.child_remote), 'branch': 'main'}}
        second = {'work/10-projects/alpha': {'url': str(self.f.base / 'other-child.git'), 'branch': 'main'}}
        wiki = Wiki(self.f.root)
        one = sync.fetch(wiki, self.f.state, target=sync.Target(bootstrap_map=first))
        with self.assertRaisesRegex(WikiError, 'unfinished|map|operation'):
            sync.fetch(wiki, self.f.state, target=sync.Target(bootstrap_map=second))
        stored = sync._load(Runtime(self.f.state, self.f.root), one['operation_id'])
        self.assertEqual(stored['bootstrap_map'], first)

    def test_nested_relative_bootstrap_url_uses_mapped_parent_endpoint(self):
        from pisar import sync
        from pisar.spaces import Wiki
        child = self.f.root / self.f.child_path
        self.f.git(child, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(child, 'config', 'user.email', 'synthetic@example.invalid')
        manifest = child / '.gitmodules'
        manifest.write_text(manifest.read_text().replace(str(self.f.grandchild_remote), '../grandchild.git'))
        self.f.git(child, 'add', '--', '.gitmodules')
        self.f.git(child, 'commit', '-qm', 'use relative nested URL')
        self.f.git(child, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.f.git(self.f.root, 'add', '--', self.f.child_path)
        self.f.git(self.f.root, 'commit', '-qm', 'pin relative nested URL')
        self.f.git(self.f.root, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        mirror = self.f.base / 'mirror' / 'child.git'
        mirror.parent.mkdir()
        subprocess.run(['git', 'clone', '--bare', '-q', str(self.f.child_remote), str(mirror)],
                       env=self.f.env, check=True)
        (self.f.root / 'outgoing.md').write_text('outgoing\n')
        self.f.git(self.f.root, 'add', '--', 'outgoing.md')
        self.f.git(self.f.root, 'commit', '-qm', 'outgoing root')
        mapping = {self.f.child_path: {'url': str(mirror), 'branch': 'main'}}
        wiki = Wiki(self.f.root)
        ready = sync.publish_only(wiki, self.f.state, target=sync.Target(bootstrap_map=mapping))
        sync.manual_review(wiki, self.f.state, ready['operation_id'])
        sync.accept(wiki, self.f.state, ready['operation_id'], ready['revision'])
        with self.assertRaisesRegex(WikiError, 'bootstrap|unavailable'):
            sync.publish(wiki, self.f.state, ready['operation_id'])

    def test_rewritten_child_destination_invalidates_partial_publication_receipt(self):
        from pisar import sync
        from pisar.spaces import Wiki
        child = self.f.root / self.f.child_path
        old = self.f.git(child, 'rev-parse', 'HEAD')
        self.f.git(child, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(child, 'config', 'user.email', 'synthetic@example.invalid')
        (child / 'local-published.md').write_text('child C\n')
        self.f.git(child, 'add', '--', 'local-published.md')
        self.f.git(child, 'commit', '-qm', 'child C')
        child_c = self.f.git(child, 'rev-parse', 'HEAD')
        backup = self.f.base / 'bootstrap-child.git'
        subprocess.run(['git', 'clone', '--bare', '-q', str(self.f.child_remote), str(backup)],
                       env=self.f.env, check=True)
        self.f.git(child, 'remote', 'add', 'backup', str(backup))
        self.f.git(child, 'push', '-q', 'backup', 'HEAD:refs/heads/main')
        self.f.git(child, 'fetch', '-q', 'backup', 'main')
        self.f.git(self.f.root, 'add', '--', self.f.child_path)
        self.f.git(self.f.root, 'commit', '-qm', 'root pin C')
        mapping = {self.f.child_path: {'url': str(backup), 'branch': 'main'}}
        wiki = Wiki(self.f.root)
        ready = sync.publish_only(wiki, self.f.state, target=sync.Target(bootstrap_map=mapping))
        sync.manual_review(wiki, self.f.state, ready['operation_id'])
        sync.accept(wiki, self.f.state, ready['operation_id'], ready['revision'])
        real_run = subprocess.run
        def reject_root_push(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and 'push' in argv and '--porcelain' in argv and str(self.f.root) in argv:
                return subprocess.CompletedProcess(argv, 1, b'', b'synthetic root rejection')
            return real_run(argv, *args, **kwargs)
        with patch('pisar.sync_graph.subprocess.run', reject_root_push):
            with self.assertRaises(WikiError):
                sync.publish(wiki, self.f.state, ready['operation_id'])
        self.assertEqual(self.f.git(self.f.child_remote, 'rev-parse', 'refs/heads/main'), child_c)
        child_pushes = []
        def reject_root_and_count_child(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and 'push' in argv and str(child) in argv:
                child_pushes.append(argv)
            return reject_root_push(argv, *args, **kwargs)
        with patch('pisar.sync_graph.subprocess.run', reject_root_and_count_child):
            with self.assertRaises(WikiError):
                sync.publish(wiki, self.f.state, ready['operation_id'])
        self.assertEqual(child_pushes, [])
        self.f.git(self.f.child_remote, 'update-ref', 'refs/heads/main', old)
        result = sync.publish(wiki, self.f.state, ready['operation_id'])
        self.assertEqual(result['status'], 'published')
        self.assertEqual(self.f.git(self.f.child_remote, 'rev-parse', 'refs/heads/main'), child_c)
        self.assertEqual(self.f.git(self.f.root_remote, 'rev-parse', 'refs/heads/main'), ready['candidate'])

    def test_final_snapshot_pin_must_remain_at_selected_nested_destination(self):
        child = self.f.root / self.f.child_path
        nested = child / 'nested'
        old_nested = self.f.git(self.f.grandchild_remote, 'rev-parse', 'refs/heads/main')
        self.f.git(child, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(child, 'config', 'user.email', 'synthetic@example.invalid')
        self.f.git(nested, 'config', 'user.name', 'Synthetic fixture')
        self.f.git(nested, 'config', 'user.email', 'synthetic@example.invalid')
        (nested / 'final-pin.md').write_text('final nested pin\n')
        self.f.git(nested, 'add', '--', 'final-pin.md')
        self.f.git(nested, 'commit', '-qm', 'nested pin G1')
        grandchild_pin = self.f.git(nested, 'rev-parse', 'HEAD')
        self.f.git(nested, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        backup = self.f.base / 'bootstrap-grandchild.git'
        subprocess.run(['git', 'clone', '--bare', '-q', str(self.f.grandchild_remote), str(backup)],
                       env=self.f.env, check=True)
        self.f.git(child, 'add', '--', 'nested')
        self.f.git(child, 'commit', '-qm', 'child B pins nested G1')
        child_pin = self.f.git(child, 'rev-parse', 'HEAD')
        self.f.git(child, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.f.git(self.f.root, 'add', '--', self.f.child_path)
        self.f.git(self.f.root, 'commit', '-qm', 'root C pins child B')
        root_pin = self.f.git(self.f.root, 'rev-parse', 'HEAD')
        self.f.git(self.f.grandchild_remote, 'update-ref', 'refs/heads/main', old_nested)
        # The nested target has been rewound to A; the bootstrap mirror still advertises G1.
        graph = sync_graph.CandidateGraph(self.f.root, (
            dict(path='.', candidate=str(self.f.root), commit=root_pin, repo=self.f.root),
            dict(path=self.f.child_path, candidate=str(child), commit=child_pin, repo=child),
            dict(path=f'{self.f.child_path}/nested', candidate=str(nested), commit=grandchild_pin, repo=nested),
        ))
        destinations = {'.': ('origin', 'main'), self.f.child_path: ('origin', 'main'),
                        f'{self.f.child_path}/nested': ('origin', 'main')}
        with self.assertRaisesRegex(WikiError, 'destination|unavailable|publication'):
            sync_graph.required_publication(graph, destinations,
                {f'{self.f.child_path}/nested': {'url': str(backup), 'branch': 'main'}},
                require_remote_pins=True)

    def test_root_push_stops_if_nested_destination_rewinds_before_final_pass(self):
        from pisar import sync
        from pisar.spaces import Wiki
        child = self.f.root / self.f.child_path
        nested = child / 'nested'
        old_nested = self.f.git(self.f.grandchild_remote, 'rev-parse', 'refs/heads/main')
        for repo in (child, nested):
            self.f.git(repo, 'config', 'user.name', 'Synthetic fixture')
            self.f.git(repo, 'config', 'user.email', 'synthetic@example.invalid')
        (nested / 'new-pin.md').write_text('G1\n')
        self.f.git(nested, 'add', '--', 'new-pin.md')
        self.f.git(nested, 'commit', '-qm', 'nested G1')
        grandchild_pin = self.f.git(nested, 'rev-parse', 'HEAD')
        backup = self.f.base / 'bootstrap-grandchild.git'
        subprocess.run(['git', 'clone', '--bare', '-q', str(self.f.grandchild_remote), str(backup)],
                       env=self.f.env, check=True)
        self.f.git(nested, 'remote', 'add', 'backup', str(backup))
        self.f.git(nested, 'push', '-q', 'backup', 'HEAD:refs/heads/main')
        self.f.git(nested, 'fetch', '-q', 'backup', 'main')
        self.f.git(child, 'add', '--', 'nested')
        self.f.git(child, 'commit', '-qm', 'child B pins G1')
        self.f.git(self.f.root, 'add', '--', self.f.child_path)
        self.f.git(self.f.root, 'commit', '-qm', 'root C pins child B')
        root_baseline = self.f.git(self.f.root_remote, 'rev-parse', 'refs/heads/main')
        mapping = {f'{self.f.child_path}/nested': {'url': str(backup), 'branch': 'main'}}
        wiki = Wiki(self.f.root)
        ready = sync.publish_only(wiki, self.f.state, target=sync.Target(bootstrap_map=mapping))
        sync.manual_review(wiki, self.f.state, ready['operation_id'])
        sync.accept(wiki, self.f.state, ready['operation_id'], ready['revision'])
        original = sync_graph.required_publication
        def rewind_before_final_pass(graph, destinations, bootstrap_map=None, **kwargs):
            if kwargs.get('require_remote_pins'):
                self.f.git(self.f.grandchild_remote, 'update-ref', 'refs/heads/main', old_nested)
            return original(graph, destinations, bootstrap_map, **kwargs)
        with patch.object(sync_graph, 'required_publication', rewind_before_final_pass):
            with self.assertRaisesRegex(WikiError, 'final child pin.*publication destination'):
                sync.publish(wiki, self.f.state, ready['operation_id'])
        self.assertEqual(self.f.git(self.f.grandchild_remote, 'rev-parse', 'refs/heads/main'), old_nested)
        self.assertEqual(self.f.git(self.f.root_remote, 'rev-parse', 'refs/heads/main'), root_baseline)
        self.assertNotEqual(grandchild_pin, old_nested)

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

