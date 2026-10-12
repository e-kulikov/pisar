"""Fetched attributes and repository-configured transport programs never run anything (review 20, findings 1 and 2).

The recording filter and the recording upload-pack write a marker when they run. The marker must never exist, and
a refused command must leave HEAD, the index and the working files as they were."""
import json

from . import sync_faults
from .sync_faults import RemoteFixture
from .support import git
from .test_sync_guard import git_file
from .test_codex_mcp import McpClient


class AttributeGuardTests(RemoteFixture):
    def setUp(self):
        super().setUp()
        self.new_world('attributes')
        self.marker = self.base / 'filter-ran'

    def add_filter(self):
        """A recording filter configured in the repository: the guard must refuse before it could run."""
        git(self.root, 'config', 'filter.evil.clean', f'touch {self.marker}; cat')
        git(self.root, 'config', 'filter.evil.smudge', f'touch {self.marker}; cat')

    def publish_attributes(self, text):
        def change(repo):
            (repo / '.gitattributes').write_text(text)
            (repo / 'foo#bar').write_text('remote content\n')
        return self.publish(change)

    def refused_without_moving(self):
        """Fetch and pull are refused (a guard error or an invalid candidate); nothing changed, nothing ran."""
        self.pisar_tool(['sync', 'fetch'])
        before = self.snapshot(status=False)
        p = self.pisar_tool(['sync', 'pull'])
        self.assertNotEqual(p.returncode, 0, p.stdout)
        if p.stdout.strip():
            self.assertEqual(json.loads(p.stdout)['status'], 'invalid', p.stdout)
        self.assertFalse(self.marker.exists(), 'a fetched attribute ran a repository filter')
        self.assertEqual(self.snapshot(status=False), before)

    def test_a_quoted_pattern_with_a_hash_is_refused_before_the_branch_moves(self):
        # The reviewer's case: the '#' is part of a quoted pattern, not a comment, so it is a live filter rule.
        self.add_filter()
        self.publish_attributes('"foo#bar" filter=evil\n')
        self.refused_without_moving()

    def test_every_non_comment_attribute_line_is_refused(self):
        for text in ('*.md filter=evil\n', '[attr]evil filter=evil\n', '"a b" text\n', 'foo\\#bar filter=evil\n',
                     '  *.md eol=crlf\n', 'x # not a comment because of the leading pattern filter=evil\n'):
            with self.subTest(text=text):
                self.publish_attributes(text)
                self.refused_without_moving()

    def test_comment_only_attributes_are_accepted(self):
        self.publish_attributes('# the wiki uses no attributes\n\n   # indented comment\n')
        self.start()
        result = sync_faults.pull_json(self)
        self.assertEqual(result['status'], 'fast-forward')
        self.assertFalse(self.marker.exists())

    def test_the_repositorys_own_info_attributes_refuse_the_sync_and_run_nothing(self):
        git_file(self.root, 'info/attributes').write_text('* filter=evil\n')
        self.publish(lambda repo: (repo / 'work/team/alpha/new.md').write_text('x\n'))
        before = self.snapshot(status=False)  # a test's own `git status` would run the attribute, so not here
        p = self.cli('sync', 'fetch', ok=False)
        self.assertIn('info/attributes', p.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.snapshot(status=False), before)

    def test_the_users_own_attributes_are_never_run_during_a_pull(self):
        (self.root / '.gitattributes').write_text('*.txt filter=evil\n')
        git(self.root, 'add', '.gitattributes')
        git(self.root, 'commit', '-q', '-m', 'the user attributes')
        git(self.root, 'push', '-q', 'origin', self.branch)
        self.publish(lambda repo: (repo / 'work/team/alpha/new.md').write_text('x\n'))
        self.refused_without_moving()  # the attributes are refused as candidate content; no filter ran


class TransportProgramTests(RemoteFixture):
    def setUp(self):
        super().setUp()
        self.marker = self.base / 'upload-ran'
        self.program = self.base / 'uploader'
        self.program.write_text(f'#!/bin/sh\ntouch {self.marker}\nexec git-upload-pack "$@"\n')
        self.program.chmod(0o755)

    def test_a_repository_configured_upload_pack_is_refused_through_the_cli(self):
        git(self.root, 'config', 'remote.origin.uploadpack', str(self.program))
        p = self.cli('sync', 'fetch', ok=False)
        self.assertIn('uploadpack', p.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(git(self.root, 'for-each-ref', 'refs/pisar'), '')

    def test_a_repository_configured_upload_pack_is_refused_through_the_codex_pisar_tool(self):
        git(self.root, 'config', 'remote.origin.uploadpack', str(self.program))
        client = McpClient(self, ['--root', str(self.root), '--state-dir', str(self.state), '--ruwana', 'ruwana',
                                  'mcp-serve'])
        client.initialize()
        text, failed = client.text('pisar', ['sync', 'fetch'])
        self.assertTrue(failed, text)
        self.assertFalse(self.marker.exists())

    def test_other_transport_program_settings_are_refused(self):
        for key, value in (('remote.origin.vcs', 'evil'), ('remote.origin.proxy', str(self.program)),
                           ('remote.origin.receivepack', str(self.program)),
                           ('core.sshcommand', str(self.program)), ('core.gitproxy', str(self.program)),
                           ('url.https://x.invalid/.insteadof', 'https://y.invalid/'), ('protocol.ext.allow', 'always')):
            with self.subTest(key=key):
                git(self.root, 'config', key, value)
                p = self.cli('sync', 'fetch', ok=False)
                self.assertTrue(p.stderr.startswith('pisar: '), p.stderr)
                self.assertFalse(self.marker.exists())
                git(self.root, 'config', '--unset-all', key)

    def test_plain_file_remotes_still_fetch_and_pull(self):
        self.publish(lambda repo: (repo / 'work/team/alpha/new.md').write_text('x\n'))
        self.start()
        self.assertEqual(sync_faults.pull_json(self)['status'], 'fast-forward')


class ConfigurationSourceTests(RemoteFixture):
    """Dotted remote names and includes of the repository's own configuration are guarded too (review 20b, B)."""

    def setUp(self):
        super().setUp()
        self.marker = self.base / 'upload-ran'
        self.program = self.base / 'uploader'
        self.program.write_text(f'#!/bin/sh\ntouch {self.marker}\nexec git-upload-pack "$@"\n')
        self.program.chmod(0o755)

    def test_a_dotted_remote_name_with_an_upload_pack_is_refused_through_the_cli(self):
        git(self.root, 'remote', 'add', 'up.stream', str(self.remote))
        git(self.root, 'config', 'remote.up.stream.uploadpack', str(self.program))
        p = self.cli('sync', 'fetch', '--remote', 'up.stream', ok=False)
        self.assertIn('uploadpack', p.stderr)
        self.assertFalse(self.marker.exists())

    def test_a_dotted_remote_name_with_an_upload_pack_is_refused_through_the_codex_pisar_tool(self):
        git(self.root, 'remote', 'add', 'up.stream', str(self.remote))
        git(self.root, 'config', 'remote.up.stream.uploadpack', str(self.program))
        client = McpClient(self, ['--root', str(self.root), '--state-dir', str(self.state), '--ruwana', 'ruwana',
                                  'mcp-serve'])
        client.initialize()
        text, failed = client.text('pisar', ['sync', 'fetch', '--remote', 'up.stream'])
        self.assertTrue(failed, text)
        self.assertFalse(self.marker.exists())

    def include_upload_pack(self):
        included = self.base / 'included.cfg'
        included.write_text(f'[remote "origin"]\n\tuploadpack = {self.program}\n')
        git(self.root, 'config', 'include.path', str(included))

    def test_an_included_upload_pack_for_origin_is_refused_through_the_cli(self):
        self.include_upload_pack()
        p = self.cli('sync', 'fetch', ok=False)
        self.assertIn('uploadpack', p.stderr)
        self.assertFalse(self.marker.exists())

    def test_an_included_upload_pack_for_origin_is_refused_through_the_codex_pisar_tool(self):
        self.include_upload_pack()
        client = McpClient(self, ['--root', str(self.root), '--state-dir', str(self.state), '--ruwana', 'ruwana',
                                  'mcp-serve'])
        client.initialize()
        text, failed = client.text('pisar', ['sync', 'fetch'])
        self.assertTrue(failed, text)
        self.assertFalse(self.marker.exists())
