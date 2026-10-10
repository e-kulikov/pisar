"""The lesson draft tools of the Codex agent: the only way the agent reads and edits draft.md.

Codex's shell and file-editing tools are not offered, so these tools stand in for the
Edit permission of the Claude agent: the draft of one open batch, never another file.
A write needs the sha256 of the text the agent last read, so a user's own edit is never
overwritten silently.
"""
import json
import os
from pathlib import Path
import unittest

from pisar import mcp
from pisar.safety import sha256
from .support import Fixture
from .test_codex_mcp import McpClient


class DraftTests(Fixture):
    def setUp(self):
        super().setUp()
        self.env = {**self.env, 'PATH': os.environ['PATH']}
        data = self.data('lesson', 'start', '--from', 'work', '--to', 'work/beta', '--title', 'Plan')
        self.batch = data['batch']
        self.draft = Path(data['draft'])
        self.client = McpClient(self, ['--root', str(self.root), '--state-dir', str(self.state),
                                       '--ruwana', 'ruwana', 'mcp-serve'])
        self.client.initialize()

    def read(self):
        result = self.client.call_with('lesson_draft_read', {'batch': self.batch})
        self.assertFalse(result.get('isError'), result)
        return json.loads(result['content'][0]['text'])

    def write(self, text, digest):
        result = self.client.call_with('lesson_draft_write', {'batch': self.batch, 'sha256': digest, 'text': text})
        return result['content'][0]['text'], bool(result.get('isError'))

    def test_read_returns_the_draft_and_its_digest(self):
        self.draft.write_text('Synthetic lesson.\n')
        value = self.read()
        self.assertEqual(value['text'], 'Synthetic lesson.\n')
        self.assertEqual(value['sha256'], sha256(b'Synthetic lesson.\n'))

    def test_write_replaces_the_draft_exactly_when_the_digest_matches(self):
        first = self.read()
        text, error = self.write('Written by the agent.\n', first['sha256'])
        self.assertFalse(error, text)
        self.assertEqual(self.draft.read_bytes(), b'Written by the agent.\n')
        self.assertEqual(json.loads(text)['sha256'], sha256(b'Written by the agent.\n'))

    def test_write_refuses_when_the_user_changed_the_draft_since_it_was_read(self):
        first = self.read()
        self.draft.write_text('The user edited this by hand.\n')
        text, error = self.write('Agent text.\n', first['sha256'])
        self.assertTrue(error)
        self.assertIn('changed since it was read', text)
        self.assertEqual(self.draft.read_bytes(), b'The user edited this by hand.\n')

    def test_write_refuses_a_symlinked_draft(self):
        target = self.base / 'elsewhere.md'
        target.write_text('outside')
        self.draft.unlink()
        self.draft.symlink_to(target)
        digest = sha256(b'outside')
        text, error = self.write('Agent text.\n', digest)
        self.assertTrue(error)
        self.assertEqual(target.read_text(), 'outside')

    def test_accepted_or_unknown_batches_are_refused(self):
        result = self.client.call_with('lesson_draft_read', {'batch': 'no-such-batch'})
        self.assertTrue(result.get('isError'))
        self.assertIn('unknown lesson batch', result['content'][0]['text'])
        for arguments in ({'batch': '../x'}, {'batch': 7}, {'batch': self.batch, 'file': 'x'}, {}):
            with self.subTest(arguments=arguments):
                result = self.client.call_with('lesson_draft_read', arguments)
                self.assertTrue(result.get('isError'), result)

    def test_write_needs_text_and_a_digest_and_limits_its_size(self):
        first = self.read()
        result = self.client.call_with('lesson_draft_write', {'batch': self.batch, 'text': 'x'})
        self.assertTrue(result.get('isError') or 'error' in result, result)
        big = 'x' * (mcp.DRAFT_LIMIT + 1)
        text, error = self.write(big, first['sha256'])
        self.assertTrue(error)
        self.assertIn('too large', text)

    def test_the_draft_tools_are_offered_and_nothing_else_is_writable(self):
        names = {tool['name'] for tool in self.client.request('tools/list')['result']['tools']}
        self.assertIn('lesson_draft_read', names)
        self.assertIn('lesson_draft_write', names)
        self.assertEqual(sorted(names), sorted(mcp.TOOL_NAMES))


if __name__ == '__main__':
    unittest.main()
