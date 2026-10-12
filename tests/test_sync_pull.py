"""`pisar sync pull`: fast-forward, refusals, validation of a fetched candidate. Remotes are local bare repositories."""
import json
import subprocess

from .support import Fixture, clean_environ, commit_all, document, git


class SyncPullTests(Fixture):
    def setUp(self):
        super().setUp()
        self.remote = self.base / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(self.remote)], check=True)
        self.branch = git(self.root, 'symbolic-ref', '--short', 'HEAD')
        git(self.root, 'remote', 'add', 'origin', str(self.remote))
        git(self.root, 'push', '-q', 'origin', self.branch)
        self.other = self.base / 'other'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(self.other)], check=True, env=clean_environ())
        self.identify(self.other)

    def identify(self, repo):
        git(repo, 'config', 'user.email', 'synthetic@example.invalid')
        git(repo, 'config', 'user.name', 'Synthetic fixture')

    def publish(self, change, message='remote change'):
        """Apply CHANGE(path) in the other clone, commit and push it: the remote moves."""
        change(self.other)
        commit_all(self.other)
        git(self.other, 'commit', '-q', '--amend', '-m', message)
        git(self.other, 'push', '-q', '-f', 'origin', self.branch)
        return git(self.other, 'rev-parse', 'HEAD')

    def add_document(self, name='remote-call', body='Remote discussion.', owner='work/alpha'):
        def change(repo):
            path = repo / 'work/team/alpha' / f'{name}.md'
            path.write_text(document(name, owner, body=body))
        return self.publish(change)

    def fetch_and_pull(self, *flags, ok=True):
        self.cli('sync', 'fetch')
        return json.loads(self.cli('sync', 'pull', *flags, ok=ok).stdout)

    def snapshot(self):
        return dict(
            head=git(self.root, 'rev-parse', 'HEAD'),
            refs=git(self.root, 'for-each-ref', '--format=%(refname) %(objectname)'),
            index=git(self.root, 'ls-files', '--stage'),
            status=git(self.root, 'status', '--porcelain=v1', '--untracked-files=all'),
            files={p.relative_to(self.root).as_posix(): p.read_bytes()
                   for p in self.root.rglob('*') if p.is_file() and '.git' not in p.relative_to(self.root).parts},
        )

    def test_fast_forward_moves_the_branch_and_writes_the_files(self):
        remote_head = self.add_document()
        result = self.fetch_and_pull()
        self.assertEqual((result['ok'], result['status']), (True, 'fast-forward'))
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), remote_head)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual((self.alpha / 'remote-call.md').read_text(), document('remote-call', 'work/alpha',
                                                                              body='Remote discussion.'))
        self.assertTrue(self.data('check')['ok'])
        self.assertEqual(self.data('read', 'wiki:work/alpha:remote-call')['metadata']['id'], 'remote-call')

    def test_a_second_pull_is_up_to_date_and_creates_no_journal(self):
        self.add_document()
        self.fetch_and_pull()
        before = self.snapshot()
        again = json.loads(self.cli('sync', 'pull').stdout)
        self.assertEqual(again['status'], 'up-to-date')
        self.assertEqual(self.snapshot(), before)

    def test_dirty_tree_is_refused_and_nothing_changes(self):
        self.add_document()
        self.cli('sync', 'fetch')
        (self.root / 'notes.txt').write_text('mine\n')
        (self.alpha / 'call.md').write_text('edited by hand\n')
        before = self.snapshot()
        p = self.cli('sync', 'pull', ok=False)
        self.assertIn('notes.txt', p.stderr + p.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_staged_edit_is_refused_and_kept(self):
        self.add_document()
        self.cli('sync', 'fetch')
        (self.root / 'staged.txt').write_text('staged\n')
        git(self.root, 'add', 'staged.txt')
        before = self.snapshot()
        self.cli('sync', 'pull', ok=False)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(git(self.root, 'diff', '--cached', '--name-only'), 'staged.txt')

    def test_a_diverged_history_is_refused_without_merge(self):
        self.add_document()
        (self.alpha / 'local.md').write_text(document('local-note', 'work/alpha', body='Local.'))
        commit_all(self.root)
        self.cli('sync', 'fetch')
        before = self.snapshot()
        result = json.loads(self.cli('sync', 'pull', ok=False).stdout)
        self.assertEqual(result['status'], 'diverged')
        self.assertIn('--merge', result['next'])
        self.assertEqual(self.snapshot(), before)

    def test_dry_run_reports_and_writes_nothing(self):
        self.add_document()
        self.cli('sync', 'fetch')
        before = self.snapshot()
        result = json.loads(self.cli('sync', 'pull', '--dry-run').stdout)
        self.assertEqual(result['status'], 'would-fast-forward')
        self.assertIn('work/team/alpha/remote-call.md', result['changes'])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.state / 'roots').exists() and any((self.state / 'roots').rglob('*.json')))

    def test_a_remote_that_breaks_validation_is_reported_and_not_written(self):
        def duplicate(repo):
            (repo / 'work/team/alpha/call-copy.md').write_text(document('call-one', 'work/alpha', body='Same id.'))
        self.publish(duplicate)
        self.cli('sync', 'fetch')
        before = self.snapshot()
        result = json.loads(self.cli('sync', 'pull', ok=False).stdout)
        self.assertEqual(result['status'], 'invalid')
        self.assertTrue(any('duplicate document id' in p for p in result['problems']), result['problems'])
        self.assertEqual(self.snapshot(), before)

    def test_undeclared_record_type_from_the_remote_is_reported(self):
        def undeclared(repo):
            (repo / 'work/team/alpha/odd.md').write_text(
                '+++\nschema_version = 1\nid = "odd-one"\ntype = "not-declared"\ntitle = "Odd"\n'
                'space_ids = ["work/alpha"]\nsources = []\n+++\n\nBody.\n')
        self.publish(undeclared)
        self.cli('sync', 'fetch')
        result = json.loads(self.cli('sync', 'pull', ok=False).stdout)
        self.assertEqual(result['status'], 'invalid')
        self.assertTrue(any('not-declared' in p for p in result['problems']), result['problems'])

    def test_cross_domain_reference_from_the_remote_is_reported(self):
        def cross(repo):
            (repo / 'work/team/alpha/leak.md').write_text(document('leak', 'work/alpha', related=('personal/home',)))
        self.publish(cross)
        self.cli('sync', 'fetch')
        result = json.loads(self.cli('sync', 'pull', ok=False).stdout)
        self.assertEqual(result['status'], 'invalid')
        self.assertTrue(result['problems'], result)

    def test_a_remote_gitattributes_with_a_filter_is_refused_and_no_program_runs(self):
        marker = self.base / 'filter-ran'
        git(self.root, 'config', 'filter.evil.smudge', f'touch {marker}')
        git(self.root, 'config', 'filter.evil.clean', f'touch {marker}')

        def attributes(repo):
            (repo / '.gitattributes').write_text('*.md filter=evil\n')
        self.publish(attributes)
        self.cli('sync', 'fetch')
        result = json.loads(self.cli('sync', 'pull', ok=False).stdout)
        self.assertEqual(result['status'], 'invalid')
        self.assertTrue(any('attributes' in p for p in result['problems']), result['problems'])
        self.assertFalse(marker.exists())

    def test_a_remote_merge_driver_is_never_run(self):
        marker = self.base / 'driver-ran'
        git(self.root, 'config', 'merge.evil.driver', f'touch {marker}; exit 1')

        def attributes(repo):
            (repo / '.gitattributes').write_text('*.md merge=evil\n')
            (repo / 'work/team/alpha/call.md').write_text('Changed remotely.\n')
        self.publish(attributes)
        (self.alpha / 'call.md').write_text(document(related=('work/beta',), body='Changed locally.'))
        commit_all(self.root)
        self.cli('sync', 'fetch')
        self.cli('sync', 'pull', '--merge', ok=False)
        self.assertFalse(marker.exists())

    def test_a_remote_symlink_is_refused(self):
        def link(repo):
            (repo / 'work/team/alpha/link.md').symlink_to('call.md')
        self.publish(link)
        self.cli('sync', 'fetch')
        result = json.loads(self.cli('sync', 'pull', ok=False).stdout)
        self.assertEqual(result['status'], 'invalid')
        self.assertTrue(any('symbolic' in p for p in result['problems']), result['problems'])

    def test_status_after_pull_reports_nothing_to_do(self):
        self.add_document()
        self.fetch_and_pull()
        status = json.loads(self.cli('sync', 'status').stdout)
        self.assertEqual((status['ahead'], status['behind']), (0, 0))
        self.assertEqual(status['open'], [])
