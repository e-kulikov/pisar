"""`pisar sync fetch` and `pisar sync status`: read-only with respect to the working tree and HEAD.

Remotes are local bare repositories built inside the temporary base directory; nothing contacts a real remote.
"""
import json
import shutil
import subprocess

from .support import Fixture, clean_environ, commit_all, document, git


class SyncFetchTests(Fixture):
    def setUp(self):
        super().setUp()
        self.remote = self.base / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(self.remote)], check=True)
        self.branch = git(self.root, 'symbolic-ref', '--short', 'HEAD')
        git(self.root, 'remote', 'add', 'origin', str(self.remote))
        git(self.root, 'push', '-q', 'origin', self.branch)
        # The other machine: a second clone that adds a document and pushes it.
        self.other = self.base / 'other'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(self.other)], check=True, env=clean_environ())
        git(self.other, 'config', 'user.email', 'synthetic@example.invalid')
        git(self.other, 'config', 'user.name', 'Synthetic fixture')
        (self.other / 'work/team/alpha/remote-call.md').parent.mkdir(parents=True, exist_ok=True)
        (self.other / 'work/team/alpha/remote-call.md').write_text(document('remote-call', 'work/alpha'))
        commit_all(self.other)
        git(self.other, 'push', '-q', 'origin', self.branch)
        self.remote_head = git(self.other, 'rev-parse', 'HEAD')
        self.ref = f'refs/pisar/sync/origin/{self.branch}'

    def fetch(self, *flags, ok=True):
        return json.loads(self.cli('sync', 'fetch', *flags, ok=ok).stdout)

    def snapshot(self):
        return dict(
            head=git(self.root, 'rev-parse', 'HEAD'),
            refs=git(self.root, 'for-each-ref', '--format=%(refname) %(objectname)'),
            index=git(self.root, 'ls-files', '--stage'),
            status=git(self.root, 'status', '--porcelain=v1', '--untracked-files=all'),
            files=sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*')
                         if '.git' not in p.relative_to(self.root).parts),
        )

    def test_fetch_writes_only_the_private_ref(self):
        before = self.snapshot()
        result = self.fetch()
        after = self.snapshot()
        self.assertEqual(after['head'], before['head'])
        self.assertEqual(after['index'], before['index'])
        self.assertEqual(after['status'], before['status'])
        self.assertEqual(after['files'], before['files'])
        self.assertFalse((self.root / '.git/FETCH_HEAD').exists())
        added = set(after['refs'].splitlines()) - set(before['refs'].splitlines())
        removed = set(before['refs'].splitlines()) - set(after['refs'].splitlines())
        self.assertEqual(added, {f'{self.ref} {self.remote_head}'})
        self.assertEqual(removed, set())
        self.assertEqual(git(self.root, 'rev-parse', self.ref), self.remote_head)
        self.assertEqual(result['commit'], self.remote_head)
        self.assertIsNone(result['previous'])

    def test_fetch_reports_the_incoming_change_by_domain_space_and_document(self):
        result = self.fetch()
        self.assertEqual(result['behind'], 1)
        self.assertEqual(result['ahead'], 0)
        alpha = result['summary']['domains']['work']['spaces']['work/alpha']
        self.assertEqual(alpha['documents'], [dict(path='work/team/alpha/remote-call.md', id='remote-call',
                                                    change='A')])
        self.assertEqual(result['summary']['other'], [])
        self.assertEqual(result['summary']['submodules'], [])

    def test_fetch_again_reports_the_previous_commit(self):
        self.fetch()
        second = self.fetch()
        self.assertEqual(second['previous'], self.remote_head)
        self.assertEqual(second['commit'], self.remote_head)
        self.assertEqual(second['behind'], 1)

    def test_status_is_offline_and_reports_the_fetched_ref(self):
        self.fetch()
        shutil.rmtree(self.remote)
        status = json.loads(self.cli('sync', 'status').stdout)
        self.assertEqual(status['commit'], self.remote_head)
        self.assertEqual(status['behind'], 1)
        self.assertEqual(status['open'], [])

    def test_status_without_a_fetch_says_so(self):
        status = json.loads(self.cli('sync', 'status').stdout)
        self.assertIsNone(status['commit'])
        self.assertIn('pisar sync fetch', status['next'])

    def test_remote_must_be_configured_in_the_repository(self):
        before = self.snapshot()
        p = self.cli('sync', 'fetch', '--remote', 'nowhere', ok=False)
        self.assertIn('nowhere', p.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_urls_paths_and_option_like_names_are_refused_before_git_runs(self):
        marker = self.base / 'ext-ran'
        git(self.root, 'remote', 'add', 'evil', f'ext::sh -c "touch {marker}"')
        for value in ('https://example.invalid/wiki.git', str(self.remote), '--upload-pack=touch', 'ext::sh',
                      '../remote.git'):
            with self.subTest(value=value):
                p = self.cli('sync', 'fetch', f'--remote={value}', ok=False)
                self.assertIn('remote', p.stderr)
        p = self.cli('sync', 'fetch', '--remote', 'evil', ok=False)
        self.assertFalse(marker.exists())
        self.assertTrue(p.stderr.startswith('pisar: '), p.stderr)

    def test_ext_transport_is_refused_even_when_configured(self):
        marker = self.base / 'ext-ran'
        git(self.root, 'config', 'remote.evil.url', f'ext::sh -c "touch {marker}"')
        p = self.cli('sync', 'fetch', '--remote', 'evil', ok=False)
        self.assertFalse(marker.exists())
        self.assertTrue(p.stderr.startswith('pisar: '))

    def test_branch_must_exist_on_the_remote_and_be_a_short_name(self):
        p = self.cli('sync', 'fetch', '--branch', 'missing', ok=False)
        self.assertIn('missing', p.stderr)
        for value in ('-x', 'a..b', 'trail.lock'):
            with self.subTest(value=value):
                p = self.cli('sync', 'fetch', f'--branch={value}', ok=False)
                self.assertIn('branch', p.stderr)

    def test_the_configured_remote_is_used(self):
        other_name = self.base / 'config/pisar/config.toml'
        other_name.parent.mkdir(parents=True, exist_ok=True)
        other_name.write_text('[sync]\nremote = "upstream"\n')
        git(self.root, 'remote', 'add', 'upstream', str(self.remote))
        env = dict(self.env, XDG_CONFIG_HOME=str(self.base / 'config'))
        result = json.loads(self.run_pisar('--root', self.root, '--state-dir', self.state, 'sync', 'fetch',
                                           env=env).stdout)
        self.assertEqual(result['remote'], 'upstream')
        self.assertEqual(result['ref'], f'refs/pisar/sync/upstream/{self.branch}')
        self.assertEqual(result['commit'], self.remote_head)

    def test_detached_head_needs_an_explicit_branch(self):
        git(self.root, 'checkout', '-q', '--detach')
        p = self.cli('sync', 'fetch', ok=False)
        self.assertIn('--branch', p.stderr)
        self.assertEqual(self.fetch('--branch', self.branch)['commit'], self.remote_head)
