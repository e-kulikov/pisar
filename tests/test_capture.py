import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from .support import Fixture, commit_all, git


class CaptureTests(Fixture):
    def capture(self, source=None, ident='capture-one', space='alpha', **kwargs):
        return self.data('capture', '--space', space, '--id', ident,
                         '--source', source or self.external(), **kwargs)

    def test_capture_preserves_original_commits_explicit_files_and_retries(self):
        source = self.external()
        before = git(self.root, 'rev-parse', 'HEAD')
        result = self.capture(source)
        self.assertEqual(source.read_text(), 'Synthetic transcript: agreed to launch.')
        self.assertEqual((self.alpha / 'sources/captures/capture-one/original').read_bytes(), source.read_bytes())
        self.assertEqual(result['status'], 'complete')
        self.assertNotEqual(git(self.root, 'rev-parse', 'HEAD'), before)
        paths = git(self.root, 'show', '--format=', '--name-only', 'HEAD').splitlines()
        self.assertEqual(set(paths), {'work/team/alpha/sources/captures/capture-one/original',
                                      'work/team/alpha/inbox/capture-one.md'})
        head = git(self.root, 'rev-parse', 'HEAD')
        self.capture(source)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(self.data('read', 'wiki:alpha:capture-one')['metadata']['type'], 'source')
        pending = self.data('inventory', '--space', 'alpha')['documents']
        self.assertEqual(next(d['metadata']['ingest_status'] for d in pending if d['reference'] == 'wiki:alpha:capture-one'), 'pending')

    def test_changed_original_and_explicit_hash_mismatch_are_conflicts(self):
        source = self.external()
        self.capture(source)
        source.write_text('changed')
        self.cli('capture', '--space', 'alpha', '--id', 'capture-one', '--source', source, ok=False)
        self.cli('capture', '--space', 'alpha', '--id', 'new-one', '--source', source,
                 '--sha256', '0' * 64, ok=False)
        self.assertFalse((self.alpha / 'sources/captures/new-one').exists())

    def test_runtime_is_rejected_inside_any_repo_or_via_symlink(self):
        source = self.external()
        for runtime in (self.root / 'runtime', self.base / 'outside-link'):
            if runtime.name == 'outside-link':
                runtime.symlink_to(self.root, target_is_directory=True)
            with self.subTest(runtime=runtime):
                self.cli('capture', '--space', 'alpha', '--id', 'bad-runtime', '--source', source,
                         state=runtime, ok=False)
        self.assertFalse((self.alpha / 'sources/captures').exists())

    def test_dirty_and_staged_files_refused_without_loss(self):
        source = self.external()
        dirty = self.root / 'unrelated.txt'
        dirty.write_text('keep exactly')
        for staged in (False, True):
            if staged:
                git(self.root, 'add', '--', 'unrelated.txt')
            self.cli('capture', '--space', 'alpha', '--id', 'dirty', '--source', source, ok=False)
            self.assertEqual(dirty.read_text(), 'keep exactly')
            self.assertFalse((self.alpha / 'sources/captures/dirty').exists())

    def test_capture_rejects_id_traversal_symlink_and_existing_target(self):
        source = self.external()
        self.cli('capture', '--space', 'alpha', '--id', '../../escape', '--source', source, ok=False)
        (self.alpha / 'sources').symlink_to(self.base, target_is_directory=True)
        commit_all(self.root)
        self.cli('capture', '--space', 'alpha', '--id', 'escape', '--source', source, ok=False)
        self.assertFalse((self.base / 'captures').exists())
        (self.alpha / 'sources').unlink()
        (self.alpha / 'sources/captures/existing').mkdir(parents=True)
        (self.alpha / 'sources/captures/existing/original').write_text('keep')
        commit_all(self.root)
        self.cli('capture', '--space', 'alpha', '--id', 'existing', '--source', source, ok=False)
        self.assertEqual((self.alpha / 'sources/captures/existing/original').read_text(), 'keep')

    def test_capture_in_submodule_commits_child_then_parent_gitlink(self):
        module = self.add_submodule()
        source = self.external()
        child_before = git(module, 'rev-parse', 'HEAD')
        parent_before = git(self.root, 'rev-parse', 'HEAD')
        result = self.capture(source, space='module')
        self.assertNotEqual(git(module, 'rev-parse', 'HEAD'), child_before)
        self.assertNotEqual(git(self.root, 'rev-parse', 'HEAD'), parent_before)
        self.assertEqual(git(self.root, 'show', '--format=', '--name-only', 'HEAD'), 'work/module')
        self.assertEqual(len(result['commits']), 2)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertIn(result['operation_id'], git(module, 'log', '-1', '--format=%s'))
        self.assertIn(result['operation_id'], git(self.root, 'log', '-1', '--format=%s'))

    def test_partial_commit_failure_is_journaled_and_retry_recovers(self):
        # Inject only Git commit failure; actual writes, staging, journal and retry are real.
        from pisar.operations import capture
        from pisar.spaces import Wiki
        from pisar.safety import WikiError
        source = self.external()
        with patch('pisar.gitops.commit', side_effect=WikiError('synthetic commit failure')):
            with self.assertRaises(WikiError):
                capture(Wiki(self.root), self.state, 'alpha', 'recovery', source)
        self.assertTrue((self.alpha / 'sources/captures/recovery/original').exists())
        manifests = list(self.state.rglob('operations/*.json'))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text())['status'], 'incomplete')
        self.capture(source, ident='recovery')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_busy_lock_prevents_concurrent_write(self):
        import fcntl
        from pisar.operations import Runtime
        runtime = Runtime(self.state, self.root)
        with runtime.lock():
            p = self.cli('capture', '--space', 'alpha', '--id', 'busy', '--source', self.external(), ok=False)
        self.assertIn('lock', p.stderr.lower())
        self.assertFalse((self.alpha / 'sources/captures/busy').exists())

    def test_capture_adopts_only_selected_untracked_project_inbox_source(self):
        inbox = self.alpha / 'inbox'
        inbox.mkdir()
        source = inbox / 'manual-transcript.txt'
        source.write_text('Manually dropped synthetic transcript')
        self.capture(source, ident='manual-one')
        self.assertEqual(source.read_text(), 'Manually dropped synthetic transcript')
        self.assertIn('work/team/alpha/inbox/manual-transcript.txt',
                      git(self.root, 'show', '--format=', '--name-only', 'HEAD'))
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        another = inbox / 'next.txt'
        another.write_text('next intended source')
        unrelated = inbox / 'unrelated.txt'
        unrelated.write_text('preserve other pending drop')
        self.cli('capture', '--space', 'alpha', '--id', 'next-one', '--source', another, ok=False)
        self.assertEqual(unrelated.read_text(), 'preserve other pending drop')
        self.assertFalse((inbox / 'next-one.md').exists())

    def test_changed_submodule_head_after_partial_operation_is_not_overwritten(self):
        from pisar import gitops
        from pisar.operations import capture
        from pisar.spaces import Wiki
        from pisar.safety import WikiError
        module = self.add_submodule()
        source = self.external()
        original_commit = gitops.commit

        def fail_parent(repo, paths, operation):
            if repo == self.root:
                raise WikiError('synthetic parent Git failure')
            return original_commit(repo, paths, operation)

        with patch.object(gitops, 'commit', fail_parent):
            with self.assertRaises(WikiError):
                capture(Wiki(self.root), self.state, 'module', 'head-conflict', source)
        user_file = module / 'user-work.txt'
        user_file.write_text('independent user work')
        commit_all(module)
        head = git(module, 'rev-parse', 'HEAD')
        self.cli('capture', '--space', 'module', '--id', 'head-conflict', '--source', source, ok=False)
        self.assertEqual(git(module, 'rev-parse', 'HEAD'), head)
        self.assertEqual(user_file.read_text(), 'independent user work')

    def test_git_environment_cannot_redirect_index_writes(self):
        other_index = self.base / 'user-index'
        self.env['GIT_INDEX_FILE'] = str(other_index)
        self.capture()
        self.assertFalse(other_index.exists())

    def test_capture_rejects_cross_domain_sources_before_destination_writes(self):
        head = git(self.root, 'rev-parse', 'HEAD')
        for source, target in ((self.personal / 'note.md', 'alpha'), (self.alpha / 'call.md', 'home')):
            with self.subTest(target=target):
                failed = self.cli('capture', '--space', target, '--id', 'wrong-domain', '--source', source, ok=False)
                self.assertIn('source scope', failed.stderr)
                self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
                self.assertFalse(list(self.root.rglob('wrong-domain.md')))

    def test_descriptor_commits_basename_and_hash_but_keeps_full_path_external(self):
        source = self.external()
        self.capture(source)
        descriptor = self.alpha / 'inbox/capture-one.md'
        self.assertNotIn(str(source), descriptor.read_text())
        meta = self.data('read', 'wiki:alpha:capture-one')['metadata']
        self.assertEqual(meta['original_name'], 'transcript.txt')
        self.assertEqual(meta['source_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        journal = next(self.state.rglob('operations/capture-alpha-capture-one.json'))
        self.assertEqual(json.loads(journal.read_text())['source_path'], str(source))
