"""The `pisar sync` command line: its global options, operands and actions are fixed; nothing else is accepted."""
from .support import Fixture


class SyncCommandTests(Fixture):
    def refused(self, *args):
        p = self.run_pisar('--root', self.root, '--state-dir', self.state, 'sync', *args, ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, '')
        return p

    def test_there_is_no_push_action(self):
        p = self.refused('push')
        self.assertIn('invalid choice', p.stderr)

    def test_global_options_are_not_accepted_after_the_action(self):
        for args in (('pull', '--ruwana', '/bin/sh'), ('pull', '--root', '/'), ('fetch', '--state-dir', '/tmp'),
                     ('status', '--agent', 'claude')):
            with self.subTest(args=args):
                self.refused(*args)

    def test_repositories_outside_the_root_and_option_like_operands_are_refused(self):
        for args in (('fetch', '--repo', '../elsewhere'), ('pull', '--repo', '/etc'), ('status', '--repo', '.git'),
                     ('fetch', '--repo', 'work/alpha'), ('fetch', '--remote', '--upload-pack=x')):
            with self.subTest(args=args):
                self.refused(*args)

    def test_sync_ids_and_paths_of_resolution_are_checked(self):
        for args in (('resolve', '--id', '../../x'), ('abort', '--id', 'sync-'), ('take', '--id', '/etc',
                     '--path', 'x', '--side', 'ours'), ('take', '--id', 'sync-000000000000', '--path', 'x',
                     '--side', 'both')):
            with self.subTest(args=args):
                self.refused(*args)

    def test_a_missing_sync_is_reported_not_created(self):
        p = self.refused('abort', '--id', 'sync-000000000000')
        self.assertIn('no sync', p.stderr)
        self.assertFalse((self.state).exists() and any(self.state.rglob('sync-*.json')))

    def test_status_without_a_remote_names_the_configured_one(self):
        p = self.refused('status', '--remote', 'nowhere')
        self.assertIn('nowhere', p.stderr)
