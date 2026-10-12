"""The Codex side of `pisar sync`: the pisar tool runs the sync commands, and the two file tools are the only way
the agent reads and writes the work tree of a conflicted sync. Confinement is the same as for lesson drafts:
relative paths inside the work tree, no `..`, no `.git`, no symbolic link, hash-guarded writes, and no write to a
`sources` folder (its files are immutable originals; only `pisar sync take` takes a side whole)."""
import json
import os
from pathlib import Path
import subprocess

from pisar import mcp
from .support import Fixture, clean_environ, commit_all, document, git
from .test_codex_mcp import McpClient


class CodexSyncTests(Fixture):
    def setUp(self):
        super().setUp()
        self.env = {**self.env, 'PATH': os.environ['PATH']}
        self.remote = self.base / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(self.remote)], check=True)
        self.branch = git(self.root, 'symbolic-ref', '--short', 'HEAD')
        git(self.root, 'remote', 'add', 'origin', str(self.remote))
        git(self.root, 'push', '-q', 'origin', self.branch)
        other = self.base / 'other'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(other)], check=True, env=clean_environ())
        git(other, 'config', 'user.email', 'synthetic@example.invalid')
        git(other, 'config', 'user.name', 'Synthetic fixture')
        (self.root / 'work/team/alpha/call.md').write_text(document(related=('work/beta',), body='Local wording.'))
        commit_all(self.root)
        (other / 'work/team/alpha/call.md').write_text(document(related=('work/beta',), body='Remote wording.'))
        commit_all(other)
        git(other, 'push', '-q', 'origin', self.branch)
        self.cli('sync', 'fetch')
        self.result = json.loads(self.cli('sync', 'pull', '--merge', ok=False).stdout)
        self.ident = self.result['sync_id']
        self.work = Path(self.result['work'])
        self.client = McpClient(self, ['--root', str(self.root), '--state-dir', str(self.state),
                                       '--ruwana', 'ruwana', 'mcp-serve'])
        self.client.initialize()

    def read(self, path):
        result = self.client.call_with('sync_file_read', {'sync_id': self.ident, 'path': path})
        return result

    def write(self, path, text, digest):
        return self.client.call_with('sync_file_write', {'sync_id': self.ident, 'path': path, 'sha256': digest,
                                                         'text': text})

    def test_the_file_tools_are_offered(self):
        names = [tool['name'] for tool in self.client.request('tools/list')['result']['tools']]
        self.assertIn('sync_file_read', names)
        self.assertIn('sync_file_write', names)

    def test_read_returns_the_conflicted_file_with_its_markers_and_digest(self):
        result = self.read('work/team/alpha/call.md')
        self.assertFalse(result.get('isError'), result)
        value = json.loads(result['content'][0]['text'])
        self.assertIn('<<<<<<<', value['text'])
        self.assertEqual(value['sha256'], mcp.sha256((self.work / 'work/team/alpha/call.md').read_bytes()))

    def test_write_replaces_the_file_only_when_the_digest_matches(self):
        digest = mcp.sha256((self.work / 'work/team/alpha/call.md').read_bytes())
        stale = self.write('work/team/alpha/call.md', 'Stale.\n', '0' * 64)
        self.assertTrue(stale.get('isError'))
        self.assertNotEqual((self.work / 'work/team/alpha/call.md').read_text(), 'Stale.\n')
        ok = self.write('work/team/alpha/call.md', document(related=('work/beta',), body='Both.'), digest)
        self.assertFalse(ok.get('isError'), ok)
        self.assertIn('Both.', (self.work / 'work/team/alpha/call.md').read_text())

    def test_paths_are_confined_to_the_work_tree(self):
        outside = self.base / 'outside.md'
        for path in ('../outside.md', str(outside), '.git', '.git/config', 'work/../../outside.md', ''):
            with self.subTest(path=path):
                self.assertTrue(self.read(path).get('isError'), path)
                self.assertTrue(self.write(path, 'x', mcp.sha256(b'')).get('isError'), path)
        self.assertFalse(outside.exists())

    def test_symbolic_links_are_refused(self):
        target = self.base / 'secret.txt'
        target.write_text('outside\n')
        link = self.work / 'linked.md'
        link.symlink_to(target)
        self.assertTrue(self.read('linked.md').get('isError'))
        self.assertTrue(self.write('linked.md', 'x', mcp.sha256(b'outside\n')).get('isError'))
        self.assertEqual(target.read_text(), 'outside\n')

    def test_sources_are_never_written_by_the_agent(self):
        (self.work / 'work/team/alpha/sources').mkdir(parents=True, exist_ok=True)
        result = self.write('work/team/alpha/sources/transcript.txt', 'Edited.\n', mcp.sha256(b''))
        self.assertTrue(result.get('isError'))
        self.assertIn('take', result['content'][0]['text'])
        self.assertFalse((self.work / 'work/team/alpha/sources/transcript.txt').exists())

    def test_an_unknown_or_finished_sync_is_refused(self):
        result = self.client.call_with('sync_file_read', {'sync_id': 'sync-000000000000', 'path': 'x.md'})
        self.assertTrue(result.get('isError'))
        self.cli('sync', 'abort', '--id', self.ident)
        result = self.client.call_with('sync_file_read', {'sync_id': self.ident, 'path': 'work/team/alpha/call.md'})
        self.assertTrue(result.get('isError'))

    def test_unknown_arguments_are_refused(self):
        result = self.client.call_with('sync_file_write', {'sync_id': self.ident, 'path': 'x.md', 'text': 'x',
                                                           'sha256': mcp.sha256(b''), 'extra': 1})
        self.assertTrue(result.get('isError'))

    def test_the_pisar_tool_runs_sync_commands_and_refuses_global_options(self):
        status = self.client.text('pisar', ['sync', 'status'])
        self.assertFalse(status[1])
        self.assertEqual(json.loads(status[0])['open'][0]['sync_id'], self.ident)
        refused = self.client.text('pisar', ['sync', 'pull', '--root', '/'])
        self.assertTrue(refused[1])
        refused = self.client.text('pisar', ['sync', 'pull', '--remote', 'https://example.invalid/wiki.git'])
        self.assertTrue(refused[1])
