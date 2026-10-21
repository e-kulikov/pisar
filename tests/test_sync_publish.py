"""Publication scope and fast-forward checks against temporary bare remotes."""
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from pisar import sync
from pisar.safety import WikiError
from pisar.safety import WikiError
from pisar.spaces import Wiki
from .support import clean_environ, commit_all, document, domain, init_repo, space


class SyncPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pisar-sync-publish-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root, self.state = self.base / 'wiki', self.base / 'runtime'
        init_repo(self.root)
        domain(self.root, 'work')
        location = space(self.root, 'work/10-projects/alpha', 'alpha')
        (location / 'note.md').write_text(document('note', 'work/alpha'))
        commit_all(self.root)
        self.env = clean_environ(HOME=str(self.base / 'home'),
                                 XDG_CONFIG_HOME=str(self.base / 'config'),
                                 XDG_DATA_HOME=str(self.base / 'data'))
        Path(self.env['HOME']).mkdir()
        self.remote = self.base / 'origin.git'
        self.git(['init', '--bare', '-q', str(self.remote)])
        self.git(['-C', str(self.root), 'remote', 'add', 'origin', str(self.remote)])
        self.git(['-C', str(self.root), 'push', '-q', '-u', 'origin', 'HEAD:refs/heads/main'])
        self.git(['-C', str(self.remote), 'symbolic-ref', 'HEAD', 'refs/heads/main'])
        self.writer = self.base / 'remote-writer'
        self.git(['clone', '--no-local', '-q', str(self.remote), str(self.writer)])
        self.git(['-C', str(self.writer), 'config', 'user.name', 'Synthetic fixture'])
        self.git(['-C', str(self.writer), 'config', 'user.email', 'synthetic@example.invalid'])

    def git(self, argv):
        return subprocess.run(['git', *argv], env=self.env, text=True, capture_output=True, check=True).stdout.strip()

    def test_remote_advance_after_apply_is_refused_without_another_push(self):
        # Both participants change from their shared base, so the accepted candidate is a merge.
        (self.root / 'local.txt').write_text('local side\n')
        commit_all(self.root)
        (self.writer / 'remote.txt').write_text('remote side\n')
        self.git(['-C', str(self.writer), 'add', 'remote.txt'])
        self.git(['-C', str(self.writer), 'commit', '-qm', 'remote side'])
        self.git(['-C', str(self.writer), 'push', '-q', 'origin', 'HEAD:refs/heads/main'])
        remote_t = self.git(['-C', str(self.writer), 'rev-parse', 'HEAD'])

        wiki = Wiki(self.root)
        fetched = sync.fetch(wiki, self.state)
        prepared = sync.prepare(wiki, self.state, fetched['operation_id'], merge=True)
        sync.manual_review(wiki, self.state, fetched['operation_id'])
        sync.accept(wiki, self.state, fetched['operation_id'], prepared['revision'])
        sync.apply(wiki, self.state, fetched['operation_id'])

        (self.writer / 'later.txt').write_text('later remote change\n')
        self.git(['-C', str(self.writer), 'add', 'later.txt'])
        self.git(['-C', str(self.writer), 'commit', '-qm', 'later remote change'])
        self.git(['-C', str(self.writer), 'push', '-q', 'origin', 'HEAD:refs/heads/main'])
        advanced = self.git(['-C', str(self.writer), 'rev-parse', 'HEAD'])
        self.assertNotEqual(remote_t, advanced)

        with self.assertRaisesRegex(WikiError, 'remote-advanced'):
            sync.publish(wiki, self.state, fetched['operation_id'])
        self.assertEqual(self.git(['-C', str(self.remote), 'rev-parse', 'refs/heads/main']), advanced)

    def test_publish_only_reuses_identical_pending_reservation(self):
        (self.root / 'local.txt').write_text('local\n')
        commit_all(self.root)
        wiki = Wiki(self.root)
        first = sync.publish_only(wiki, self.state)
        second = sync.publish_only(wiki, self.state)
        self.assertEqual(first['operation_id'], second['operation_id'])

    def test_close_releases_reservation_and_retains_candidate(self):
        (self.root / 'local.txt').write_text('local\n')
        commit_all(self.root)
        wiki = Wiki(self.root)
        first = sync.publish_only(wiki, self.state)
        self.assertTrue(sync.close(wiki, self.state, first['operation_id'])['candidate_retained'])
        second = sync.publish_only(wiki, self.state)
        self.assertNotEqual(first['operation_id'], second['operation_id'])
        first_candidate = sync.inspect(wiki, self.state, first['operation_id'])['candidate_worktree']
        self.assertTrue(Path(first_candidate).is_dir())

    def test_retry_after_remote_descendant_is_already_published(self):
        wiki = Wiki(self.root)
        prepared = sync.publish_only(wiki, self.state)
        sync.manual_review(wiki, self.state, prepared['operation_id'])
        sync.accept(wiki, self.state, prepared['operation_id'], prepared['revision'])
        sync.publish(wiki, self.state, prepared['operation_id'])
        (self.writer / 'later.txt').write_text('later\n')
        self.git(['-C', str(self.writer), 'add', 'later.txt'])
        self.git(['-C', str(self.writer), 'commit', '-qm', 'later'])
        self.git(['-C', str(self.writer), 'push', '-q', 'origin', 'HEAD:refs/heads/main'])
        before = self.git(['-C', str(self.remote), 'rev-parse', 'refs/heads/main'])
        result = sync.publish(wiki, self.state, prepared['operation_id'])
        self.assertTrue(result['already_reachable'])
        self.assertEqual(self.git(['-C', str(self.remote), 'rev-parse', 'refs/heads/main']), before)

    def test_publish_only_task_history_then_revert_is_blocked(self):
        (self.root / '.ruwana/tasks').mkdir(parents=True)
        task = self.root / '.ruwana/tasks/one.md'
        task.write_text('temporary task\n')
        commit_all(self.root)
        self.git(['-C', str(self.root), 'rm', '.ruwana/tasks/one.md'])
        self.git(['-C', str(self.root), 'commit', '-qm', 'revert task'])
        wiki = Wiki(self.root)
        with self.assertRaisesRegex(WikiError, 'task-validation-unavailable'):
            sync.publish_only(wiki, self.state)

    def test_transport_error_after_remote_descendant_is_observed_as_success(self):
        (self.root / 'local.txt').write_text('local candidate\n')
        commit_all(self.root)
        wiki = Wiki(self.root)
        prepared = sync.publish_only(wiki, self.state)
        sync.manual_review(wiki, self.state, prepared['operation_id'])
        sync.accept(wiki, self.state, prepared['operation_id'], prepared['revision'])
        real_run = subprocess.run
        intercepted = False

        def run(argv, *args, **kwargs):
            nonlocal intercepted
            if not intercepted and 'push' in argv:
                intercepted = True
                real_run(argv, *args, **kwargs)
                self.git(['-C', str(self.writer), 'fetch', '-q', 'origin'])
                self.git(['-C', str(self.writer), 'merge', '--ff-only', 'FETCH_HEAD'])
                (self.writer / 'after-candidate.txt').write_text('descendant\n')
                self.git(['-C', str(self.writer), 'add', 'after-candidate.txt'])
                self.git(['-C', str(self.writer), 'commit', '-qm', 'descendant'])
                self.git(['-C', str(self.writer), 'push', '-q', 'origin', 'HEAD:refs/heads/main'])
                return subprocess.CompletedProcess(argv, 1, b'', b'transport lost')
            return real_run(argv, *args, **kwargs)

        with mock.patch('pisar.sync.subprocess.run', side_effect=run):
            result = sync.publish(wiki, self.state, prepared['operation_id'])
        self.assertTrue(result['observed_after_error'])


if __name__ == '__main__':
    unittest.main()
