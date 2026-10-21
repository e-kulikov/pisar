"""The pisar MCP server of the Codex agent: its tools, their argument checks and the lesson accept confirmation.

The allowlist guarantees mirror CLAUDE_ALLOWED and CLAUDE_DENIED in agent.py: no bare git or
pisar, no global options, no mv, no shells or interpreters, no push, and accept only after the
user says so.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from pisar import mcp
from . import support
from .support import CHECKOUT, Fixture, commit_all, pisar_command


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

    def call_with(self, name, arguments):
        return self.request('tools/call', {'name': name, 'arguments': arguments})['result']

    def text(self, name, args):
        result = self.call(name, args)
        return result['content'][0]['text'], bool(result.get('isError'))

    def text_with(self, name, arguments):
        result = self.call_with(name, arguments)
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


class LintTests(unittest.TestCase):
    def test_only_the_shared_git_helpers_start_git_or_any_subprocess_with_a_shell(self):
        """Every subprocess of mcp.py is started by one of the allowed functions, and never through a shell."""
        import ast
        import pisar
        tree = ast.parse(Path(pisar.__file__).with_name('mcp.py').read_text(encoding='utf-8'))
        allowed = {'_git_query', 'capture'}  # the query helper, and the final command of the tool
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def enclosing(node):
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return node.name
            return '<module>'
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = ast.unparse(node.func)
                if func.startswith('subprocess.') or func in ('os.system', 'os.popen') or func.startswith('os.exec') \
                        or func.startswith('os.spawn'):
                    self.assertIn(enclosing(node), allowed, f'{func} in {enclosing(node)}')
                    self.assertFalse(any(k.arg == 'shell' and not (isinstance(k.value, ast.Constant)
                                                                   and k.value.value is False)
                                         for k in node.keywords), func)
        self.assertNotIn('shell=True', ast.unparse(tree))


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

    def test_handshake_and_declared_tools(self):
        client = McpClient(self, ['--root', str(self.root), '--state-dir', str(self.state),
                                  '--ruwana', 'ruwana', 'mcp-serve'])
        init = client.initialize()
        self.assertEqual(init['result']['serverInfo']['name'], 'pisar')
        self.assertEqual(init['result']['capabilities'], {'tools': {}})
        tools = client.request('tools/list')['result']['tools']
        self.assertEqual(sorted(t['name'] for t in tools), sorted(mcp.TOOL_NAMES))
        self.assertIn('sync_accept', mcp.TOOL_NAMES)
        self.assertIn('sync_apply', mcp.TOOL_NAMES)
        self.assertIn('sync_publish', mcp.TOOL_NAMES)

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

    def test_sync_owner_decisions_are_not_available_through_generic_pisar(self):
        from pisar import mcp
        for args in (['sync', 'accept', '--operation', 'x', '--revision', 'y'],
                     ['sync', 'apply', '--operation', 'x'], ['sync', 'publish', '--operation', 'x']):
            with self.subTest(args=args), self.assertRaises(mcp.Refused):
                mcp.check_pisar(args)

    def test_sync_accept_decline_never_writes_a_decision(self):
        client = self.server(elicit='decline')
        text, error = client.text_with('sync_accept', {'operation': 'sync-example', 'revision': 'digest'})
        self.assertTrue(error)
        self.assertIn('not confirmed by the user', text)
        self.assertEqual(len(client.asked), 1)
        self.assertFalse(self.state.exists())

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
        self.assertIn('this client cannot ask for confirmation', text)
        self.assertNotIn('unknown lesson batch', text)
        self.assertEqual(client.asked, [])

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

    def test_a_non_object_json_line_is_an_error_and_is_not_end_of_input(self):
        # `null` decodes to None: it must not be taken for the end of the connection.
        import io
        from pisar import mcp as module
        out = io.StringIO()
        request = '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        server = module.Server(self.root, self.state, 'ruwana', io.StringIO('null\n[1]\n' + request), out, {})
        server.serve()
        answers = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([a.get('error', {}).get('code') for a in answers[:2]], [-32600, -32600])
        self.assertEqual(answers[2], {'jsonrpc': '2.0', 'id': 1, 'result': {}})

    def test_malformed_requests_get_an_error_and_the_server_keeps_serving(self):
        client = self.server()
        for params in ([1, 2], {'name': 'pisar', 'arguments': []}, {'name': 'pisar', 'arguments': {'args': 'x'}}):
            with self.subTest(params=params):
                client.send({'jsonrpc': '2.0', 'id': 50, 'method': 'tools/call', 'params': params})
                answer = client.read()
                self.assertTrue('error' in answer or answer['result'].get('isError'), answer)
                self.assertEqual(client.request('ping')['result'], {})

    def test_git_never_reads_files_outside_the_root(self):
        # Two existing files outside the root: Git would switch to no-index mode for them.
        first, second = self.base / 'outside-one.txt', self.base / 'outside-two.txt'
        first.write_text('SENTINEL-ONE\n')
        second.write_text('SENTINEL-TWO\n')
        client = self.server()
        for args in (['diff', str(first), str(second)], ['diff', '--', str(first)],
                     ['log', str(first)], ['add', str(first)], ['status', '../outside-one.txt']):
            with self.subTest(args=args):
                text, error = client.text('git', args)
                self.assertTrue(error)
                self.assertNotIn('SENTINEL', text)

    def test_git_still_takes_revisions_and_paths_inside_the_root(self):
        client = self.server()
        (self.alpha / 'call.md').write_text('Changed synthetic text.\n')
        for args in (['diff', 'HEAD', '--', 'work/team/alpha/call.md'], ['diff', 'HEAD..HEAD', '--', 'work'],
                     ['log', '-1', 'HEAD'], ['diff', '--stat', 'HEAD', 'work/team/alpha/call.md'],
                     ['mv', 'work/team/alpha/call.md', 'work/team/alpha/call2.md'], ['status', '--short']):
            with self.subTest(args=args):
                text, error = client.text('git', args)
                self.assertFalse(error, text)
        self.assertTrue((self.alpha / 'call2.md').is_file())

    def test_validated_diff_output_equals_direct_git_for_revision_notation(self):
        # Revision-set notation must stay revisions, not become paths behind `--`.
        (self.alpha / 'call.md').write_text('Second synthetic version.\n')
        commit_all(self.root)
        (self.alpha / 'call.md').write_text('Third synthetic version.\n')
        commit_all(self.root)
        (self.alpha / 'call.md').write_text('Uncommitted synthetic version.\n')  # so the path diff is not empty
        from pisar import mcp as module
        cases = [['HEAD^!'], ['HEAD^@'], ['HEAD~1..HEAD'], ['HEAD~1...HEAD'], ['^HEAD~1', 'HEAD'],
                 ['work/team/alpha/call.md']]
        for operands in cases:
            with self.subTest(operands=operands):
                direct = subprocess.run(['git', '-C', str(self.root), 'diff', *operands],
                                        capture_output=True, text=True, check=True).stdout
                validated = module.check_git(['diff', *operands], self.root)
                run = subprocess.run(['git', '-C', str(self.root), *validated],
                                     capture_output=True, text=True, check=True).stdout
                self.assertTrue(direct, 'the direct diff must not be empty in this fixture')
                self.assertEqual(run, direct)

    def test_an_operand_that_is_neither_a_revision_nor_a_path_is_refused(self):
        from pisar import mcp as module
        with self.assertRaisesRegex(module.Refused, 'not a revision or an existing path'):
            module.check_git(['diff', 'no-such-revision-or-file'], self.root)
        with self.assertRaisesRegex(module.Refused, 'not a revision or an existing path'):
            module.check_git(['log', 'HEAD~99'], self.root)

    def custom_metadata_submodule(self):
        """A registered submodule whose git dir sits in the root, outside any `.git` component."""
        module = self.add_submodule()
        meta = self.root / 'module-meta'
        shutil.move(str(self.root / '.git' / 'modules' / 'work' / 'module'), str(meta))
        (module / '.git').write_text(f'gitdir: {meta}\n')
        config = meta / 'config'  # core.worktree was relative to the old place: make it absolute
        config.write_text(''.join(f'\tworktree = {module}\n' if 'worktree' in line else line
                                  for line in config.read_text().splitlines(True)))
        self.assertEqual(Path(subprocess.run(['git', '-C', str(module), 'rev-parse', '--absolute-git-dir'],
                                             capture_output=True, text=True, check=True).stdout.strip()).resolve(),
                         meta.resolve())
        return module, meta

    def test_custom_submodule_metadata_is_refused_for_add_and_mv_in_every_spelling(self):
        from pisar import mcp as module_
        module, meta = self.custom_metadata_submodule()
        (self.root / 'alias').symlink_to(meta)
        (self.root / 'note.md').write_text('Synthetic.\n')
        spellings = ['module-meta/config', 'module-meta', str(meta / 'config'), f'{self.root}/module-meta/config',
                     'work/module/../../module-meta/config', './module-meta/HEAD', 'alias/config', 'alias']
        for spelling in spellings:
            with self.subTest(spelling=spelling):
                with self.assertRaises(module_.Refused):
                    module_.check_git(['add', spelling], self.root)
                with self.assertRaises(module_.Refused):
                    module_.check_git(['mv', 'note.md', spelling], self.root)
                with self.assertRaises(module_.Refused):
                    module_.check_git(['mv', spelling, 'note.md'], self.root)

    def test_ordinary_submodule_paths_stay_allowed(self):
        from pisar import mcp as module_
        module, meta = self.custom_metadata_submodule()
        (module / 'inside.md').write_text('Synthetic file of the submodule.\n')
        for args in (['add', 'work/module'], ['add', 'work/module/inside.md'], ['status', '--', 'work/module'],
                     ['diff', '--', 'work/module/inside.md']):
            with self.subTest(args=args):
                module_.check_git(args, self.root)

    def test_a_bare_repository_layout_inside_the_root_is_refused(self):
        from pisar import mcp as module_
        bare = self.root / 'bare-like'
        (bare / 'objects').mkdir(parents=True)
        (bare / 'refs').mkdir()
        (bare / 'HEAD').write_text('ref: refs/heads/main\n')
        for args in (['add', 'bare-like/HEAD'], ['add', 'bare-like/refs/x'], ['mv', 'bare-like/HEAD', 'note.md']):
            with self.subTest(args=args), self.assertRaises(module_.Refused):
                module_.check_git(args, self.root)
        module_.check_git(['add', 'work/team/alpha/call.md'], self.root)

    def recording_fsmonitor(self, repository):
        """Make REPOSITORY run a harmless recording fsmonitor program, which writes a marker file."""
        marker = self.base / f'fsmonitor-marker-{len(list(self.base.glob("fsmonitor-marker-*")))}'
        program = self.base / f'fsmonitor-{marker.name}.sh'
        program.write_text(f'#!/bin/sh\ntouch {marker}\nexit 0\n')
        program.chmod(0o755)
        subprocess.run(['git', '-C', str(repository), 'config', 'core.fsmonitor', str(program)], check=True)
        return marker

    def test_validation_never_runs_a_configured_fsmonitor_in_root_or_submodule(self):
        # Validation reads the index (submodule enumeration included); a repository's own fsmonitor
        # program must never run for it, in the root or in an initialised submodule.
        from pisar import mcp as module_
        module, _ = self.custom_metadata_submodule()
        root_marker = self.recording_fsmonitor(self.root)
        module_marker = self.recording_fsmonitor(module)
        for args in (['status', '--short'], ['diff', 'HEAD'], ['log', '-1']):
            with self.subTest(args=args):
                module_.check_git(args, self.root)
        self.assertFalse(root_marker.exists(), 'validation ran the root fsmonitor')
        self.assertFalse(module_marker.exists(), 'validation ran the submodule fsmonitor')

    def test_the_real_tool_never_runs_a_configured_fsmonitor(self):
        module, _ = self.custom_metadata_submodule()
        root_marker = self.recording_fsmonitor(self.root)
        module_marker = self.recording_fsmonitor(module)
        client = self.server()
        for args in (['status', '--short'], ['diff', '--stat']):
            with self.subTest(args=args):
                _, error = client.text('git', args)
                self.assertFalse(error)
        self.assertFalse(root_marker.exists())
        self.assertFalse(module_marker.exists())

    def test_git_never_reads_through_a_symlink_to_an_outside_file(self):
        outside = self.base / 'outside-link.txt'
        outside.write_text('SENTINEL-LINK\n')
        (self.root / 'link.txt').symlink_to(outside)
        client = self.server()
        text, error = client.text('git', ['diff', '--no-index', 'link.txt', 'work/team/alpha/call.md'])
        self.assertTrue(error)
        self.assertNotIn('SENTINEL', text)
        text, error = client.text('git', ['add', 'link.txt'])
        self.assertTrue(error)

    def test_git_refuses_git_administrative_paths_for_every_subcommand(self):
        config = (self.root / '.git' / 'config').read_bytes()
        client = self.server()
        for args in (['mv', 'work/team/alpha/call.md', '.git/config'], ['mv', '-f', 'work/team/alpha/call.md', '.git/config'],
                     ['add', '.git/config'], ['status', '.git'], ['log', '--', '.git/HEAD'],
                     ['diff', '--', '.git/config'], ['mv', '.git/config', 'work/x.md'],
                     ['add', self.root.as_posix() + '/.git/config'], ['add', './work/../.git/config'],
                     ['add', ':(literal).git/config'], ['add', ':/.git/config']):
            with self.subTest(args=args):
                text, error = client.text('git', args)
                self.assertTrue(error, text)
        self.assertEqual((self.root / '.git' / 'config').read_bytes(), config)

    def test_git_refuses_symlinks_whose_target_is_git_administrative(self):
        (self.root / 'alias').symlink_to(self.root / '.git' / 'config')
        client = self.server()
        for args in (['add', 'alias'], ['mv', 'alias', 'work/moved.md'], ['mv', 'work/team/alpha/call.md', 'alias']):
            with self.subTest(args=args):
                _, error = client.text('git', args)
                self.assertTrue(error)

    def test_the_full_clean_config_attack_is_stopped_at_the_git_mv(self):
        # The complete chain: a lesson draft with chosen text is captured as a tracked file, which is then
        # moved onto .git/config and used to run a filter. Every step must be refused or harmless.
        marker = self.base / 'MARKER-OUTSIDE-ROOT'
        before = (self.root / '.git' / 'config').read_bytes()
        client = self.server()
        start = client.text('pisar', ['lesson', 'start', '--from', 'work', '--to', 'work/beta', '--title', 'T'])[0]
        batch = json.loads(start)['batch']
        draft = Path(json.loads(start)['draft'])
        payload = f'[core]\n\tattributesFile = {draft}\n[filter "escape"]\n\tclean = touch {marker}; cat\n'
        check = client.call_with('lesson_draft_read', {'batch': batch})
        digest = json.loads(check['content'][0]['text'])['sha256']
        result = client.call_with('lesson_draft_write', {'batch': batch, 'sha256': digest, 'text': payload})
        self.assertFalse(result.get('isError'), result)
        _, error = client.text('pisar', ['capture', '--space', 'work/beta', '--id', 'escape-config', '--source', str(draft)])
        self.assertFalse(error)
        tracked = next(self.root.glob('work/team/beta/**/escape-config/original'))
        moved, error = client.text('git', ['mv', '-f', str(tracked.relative_to(self.root)), '.git/config'])
        self.assertTrue(error, moved)
        self.assertEqual((self.root / '.git' / 'config').read_bytes(), before)
        self.assertFalse(marker.exists())

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
