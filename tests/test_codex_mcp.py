"""The pisar MCP server of the Codex agent: its tools, their argument checks and the lesson accept confirmation.

The allowlist guarantees mirror CLAUDE_ALLOWED and CLAUDE_DENIED in agent.py: no bare git or
pisar, no global options, no mv, no shells or interpreters, no push, and accept only after the
user says so.
"""
import json
import os
import subprocess
import unittest

from pisar import mcp
from . import support
from .support import CHECKOUT, Fixture, pisar_command


class McpClient:
    """Drives `pisar mcp-serve` over its stdio, one JSON line per message."""

    def __init__(self, test, args, elicit=None):
        self.test = test
        self.elicit = elicit  # None: no elicitation capability; else the action the user answers with
        self.answer_hook = None  # called with (client, question) instead of the default answer
        self.others = []  # messages for other request ids, received while waiting
        self.asked = []
        self.next = 0
        env = dict(test.env)
        if not support.EXECUTABLE:
            env['PYTHONPATH'] = str(CHECKOUT)
        self.process = subprocess.Popen([*pisar_command(), *args], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        env=env, cwd=test.base)
        test.addCleanup(self.close)

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + '\n')
        self.process.stdin.flush()

    def read(self):
        line = self.process.stdout.readline()
        if not line:
            self.test.fail(f'the server closed its output: {self.process.stderr.read()}')
        return json.loads(line)

    def request(self, method, params=None):
        self.next += 1
        ident = self.next
        self.send({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params or {}})
        while True:
            message = self.read()
            if message.get('method') == 'elicitation/create':
                self.asked.append(message['params']['message'])
                if self.answer_hook:
                    self.answer_hook(self, message)
                else:
                    answer = {'action': self.elicit or 'decline'}
                    self.send({'jsonrpc': '2.0', 'id': message['id'], 'result': answer})
                continue
            if message.get('id') == ident:
                return message
            self.others.append(message)

    def call(self, name, args):
        return self.request('tools/call', {'name': name, 'arguments': {'args': args}})['result']

    def text(self, name, args):
        result = self.call(name, args)
        return result['content'][0]['text'], bool(result.get('isError'))

    def initialize(self):
        capabilities = {'elicitation': {}} if self.elicit is not None else {}
        return self.request('initialize', {'protocolVersion': mcp.PROTOCOL, 'capabilities': capabilities,
                                           'clientInfo': {'name': 'test', 'version': '0'}})

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()


class McpTests(Fixture):
    def setUp(self):
        super().setUp()
        self.env = {**self.env, 'PATH': os.environ['PATH']}

    def server(self, elicit=None):
        args = ['--root', str(self.root), '--state-dir', str(self.state),
                '--ruwana', str(self.base / 'no-such-ruwana'), 'mcp-serve']
        client = McpClient(self, args, elicit)
        client.initialize()
        return client

    def test_handshake_and_exactly_the_three_tools(self):
        client = McpClient(self, ['--root', str(self.root), '--state-dir', str(self.state),
                                  '--ruwana', 'ruwana', 'mcp-serve'])
        init = client.initialize()
        self.assertEqual(init['result']['serverInfo']['name'], 'pisar')
        self.assertEqual(init['result']['capabilities'], {'tools': {}})
        tools = client.request('tools/list')['result']['tools']
        self.assertEqual(sorted(t['name'] for t in tools), ['git', 'lesson_accept', 'lesson_review', 'pisar'])

    def test_unknown_method_is_a_json_rpc_error(self):
        client = self.server()
        self.assertEqual(client.request('no/such')['error']['code'], -32601)

    def test_pisar_runs_allowed_read_commands_in_the_root(self):
        client = self.server()
        text, error = client.text('pisar', ['spaces'])
        self.assertFalse(error)
        self.assertIn('spaces', json.loads(text))

    def test_pisar_refuses_global_options_anywhere(self):
        client = self.server()
        for args in (['--ruwana', '/bin/sh', 'spaces'], ['spaces', '--ruwana', 'x'],
                     ['--root', '/', 'spaces'], ['--state-dir=/', 'spaces'], ['--agent', 'codex']):
            with self.subTest(args=args):
                text, error = client.text('pisar', args)
                self.assertTrue(error)
                self.assertIn('global options are not accepted', text)

    def test_pisar_refuses_bare_pisar_arbitrary_binaries_and_mv(self):
        client = self.server()
        for args in ([], ['mv', 'a', 'b'], ['sh', '-c', 'true'], ['python3', '-c', 'print(1)'],
                     ['git', 'status'], ['lesson'], ['lesson', 'zap'],
                     ['--version']):
            with self.subTest(args=args):
                _, error = client.text('pisar', args)
                self.assertTrue(error)

    def test_pisar_never_runs_lesson_accept_itself(self):
        client = self.server(elicit='accept')
        text, error = client.text('pisar', ['lesson', 'accept', '--batch', 'x'])
        self.assertTrue(error)
        self.assertIn('lesson_accept', text)
        self.assertEqual(client.asked, [])

    def test_accept_without_the_clients_elicitation_capability_is_refused(self):
        client = self.server(elicit=None)
        text, error = client.text('lesson_accept', ['--batch', 'missing'])
        self.assertTrue(error)
        self.assertIn('not confirmed by the user', text)
        self.assertNotIn('unknown lesson batch', text)

    def test_accept_is_refused_unless_the_user_accepts(self):
        for answer in ('decline', 'cancel'):
            with self.subTest(answer=answer):
                client = self.server(elicit=answer)
                text, error = client.text('lesson_accept', ['--batch', 'missing'])
                self.assertTrue(error)
                self.assertIn('not confirmed by the user', text)
                self.assertEqual(len(client.asked), 1)

    def test_accept_runs_only_after_an_explicit_user_accept(self):
        client = self.server(elicit='accept')
        text, error = client.text('lesson_accept', ['--batch', 'missing'])
        self.assertTrue(error)
        self.assertIn('unknown lesson batch', text)  # the real command ran and refused the batch
        self.assertEqual(len(client.asked), 1)
        self.assertIn('lesson accept --batch missing', client.asked[0])

    def test_accept_refuses_global_options(self):
        client = self.server(elicit='accept')
        text, error = client.text('lesson_accept', ['--ruwana', 'x', '--batch', 'b'])
        self.assertTrue(error)
        self.assertEqual(client.asked, [])

    def test_git_refuses_bare_git_aliases_config_push_and_programs(self):
        client = self.server()
        for args in (['push'], ['config', 'alias.x', '!sh'], ['-c', 'alias.x=!sh', 'x'],
                     ['status', '-c', 'core.pager=sh'], ['log', '--git-dir=/tmp/x'],
                     ['diff', '--output=/tmp/out'], ['diff', '--ext-diff'], ['diff', '--no-index', '/etc/hosts'],
                     ['commit', '--upload-pack=sh', '-m', 'x'], ['', ], []):
            with self.subTest(args=args):
                _, error = client.text('git', args)
                self.assertTrue(error)

    def test_git_refuses_abbreviated_and_unlisted_options(self):
        # git accepts unambiguous prefixes of long options, so a blocklist of full spellings is not enough.
        client = self.server()
        for args in (['diff', '--outpu=/tmp/x'], ['log', '--out=/tmp/x'], ['diff', '--ext-d'],
                     ['diff', '--ext-diff'], ['log', '--ext'], ['diff', '--textc'], ['log', '--textconv'],
                     ['diff', '--no-ind', '/etc/hosts'], ['diff', '-O/etc/passwd'], ['log', '-O/etc/passwd'],
                     ['commit', '-F', '/etc/passwd'], ['commit', '--file=/etc/passwd'],
                     ['commit', '--template=/etc/passwd'], ['add', '--pathspec-from-file=/etc/passwd'],
                     ['commit', '-C', 'HEAD'], ['commit', '-c', 'HEAD'], ['status', '--porcelain=v9'],
                     ['status', '-c', 'core.pager=sh'], ['mv', '--exec', 'sh', 'a', 'b'],
                     ['log', '--no-walk=sorted', '--foo'], ['diff', '--no-index'], ['add', '--interactive'],
                     ['add', '-p'], ['status', '--mystery']):
            with self.subTest(args=args):
                _, error = client.text('git', args)
                self.assertTrue(error)

    def test_git_allows_the_listed_read_and_write_options(self):
        client = self.server()
        (self.alpha / 'new.md').write_text('Synthetic.\n')
        for args in (['status', '--short', '--branch'], ['status', '-uno'], ['status', '--untracked-files=all'],
                     ['diff', '--stat'], ['diff', '--cached', '--name-only'], ['log', '-n', '1', '--oneline'],
                     ['log', '-3', '--format=%H'], ['log', '--max-count=1', '--', 'work']):
            with self.subTest(args=args):
                _, error = client.text('git', args)
                self.assertFalse(error)

    def test_elicitation_edge_cases_never_accept_by_accident(self):
        # An answer for another request, an answer carrying an error, a closed connection and a second
        # tools/call while the question is pending must never run the accept.
        def other_id(client, question):
            client.send({'jsonrpc': '2.0', 'id': 'someone-else', 'result': {'action': 'accept'}})
            client.send({'jsonrpc': '2.0', 'id': question['id'], 'result': {'action': 'decline'}})

        def with_error(client, question):
            client.send({'jsonrpc': '2.0', 'id': question['id'], 'error': {'code': 1, 'message': 'x'},
                         'result': {'action': 'accept'}})

        def closed(client, question):
            client.process.stdin.close()

        def nested(client, question):
            client.send({'jsonrpc': '2.0', 'id': 'second', 'method': 'tools/call', 'params': {
                'name': 'lesson_accept', 'arguments': {'args': ['--batch', 'missing']}}})
            client.send({'jsonrpc': '2.0', 'id': question['id'], 'result': {'action': 'decline'}})

        for name, hook in (('other-id', other_id), ('error', with_error), ('eof', closed), ('nested', nested)):
            with self.subTest(case=name):
                client = self.server(elicit='accept')
                client.answer_hook = hook
                text, error = client.text('lesson_accept', ['--batch', 'missing'])
                self.assertTrue(error)
                self.assertIn('not confirmed by the user', text)
                self.assertNotIn('unknown lesson batch', text)
                self.assertEqual(len(client.asked), 1)
                if name == 'nested':
                    second = [m for m in client.others if m.get('id') == 'second'][0]
                    self.assertIn('error', second)
                    self.assertNotIn('unknown lesson batch', json.dumps(second))

    def test_malformed_requests_get_an_error_and_the_server_keeps_serving(self):
        client = self.server()
        for params in ([1, 2], {'name': 'pisar', 'arguments': []}, {'name': 'pisar', 'arguments': {'args': 'x'}}):
            with self.subTest(params=params):
                client.send({'jsonrpc': '2.0', 'id': 50, 'method': 'tools/call', 'params': params})
                answer = client.read()
                self.assertTrue('error' in answer or answer['result'].get('isError'), answer)
                self.assertEqual(client.request('ping')['result'], {})

    def test_git_reads_and_commits_only_inside_the_root(self):
        client = self.server()
        (self.alpha / 'new.md').write_text('Synthetic new note.\n')
        status, error = client.text('git', ['status', '--short'])
        self.assertFalse(error)
        self.assertIn('new.md', status)
        _, error = client.text('git', ['add', 'work/team/alpha/new.md'])
        self.assertFalse(error)
        _, error = client.text('git', ['commit', '-m', 'Add synthetic note'])
        self.assertFalse(error)
        log, _ = client.text('git', ['log', '--oneline', '-1'])
        self.assertIn('Add synthetic note', log)

    def test_git_mv_renames_tracked_files(self):
        client = self.server()
        _, error = client.text('git', ['mv', 'work/team/alpha/call.md', 'work/team/alpha/moved.md'])
        self.assertFalse(error)
        self.assertTrue((self.alpha / 'moved.md').is_file())

    def test_git_cannot_leave_the_root_with_pathspecs(self):
        client = self.server()
        _, error = client.text('git', ['add', '../../../etc/hosts'])
        self.assertTrue(error)

    def test_rule_list_has_no_bare_entries(self):
        for prefix in mcp.PISAR:
            self.assertTrue(prefix)
        self.assertNotIn(('lesson',), mcp.PISAR)
        self.assertNotIn(('lesson', 'accept'), mcp.PISAR)
        self.assertEqual(set(mcp.GIT), {'status', 'diff', 'log', 'add', 'commit', 'mv'})


if __name__ == '__main__':
    unittest.main()
