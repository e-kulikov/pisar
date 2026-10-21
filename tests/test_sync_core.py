"""Real-Git regression coverage for the delivery-I candidate primitives."""
import subprocess
import tempfile
from pathlib import Path
import unittest

from .support import clean_environ, init_repo, commit_all, domain, space, document
from pisar import operations, sync, sync_git
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
        sync.manual_review(wiki, self.state, fetched['operation_id'])
        sync.accept(wiki, self.state, fetched['operation_id'], result['revision'])
        self.assertEqual(sync.apply(wiki, self.state, fetched['operation_id'])['head'], result['candidate'])


if __name__ == '__main__':
    unittest.main()
