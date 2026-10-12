"""Checks against the real `codex` executable, gated by PISAR_TEST_CODEX (skipped otherwise).

Only configuration commands run here (`codex mcp get`, `codex features list`): they read the
generated CODEX_HOME and never call a model. A run that does call the model is a manual step
documented in the README; it is not part of the suite.
"""
import http.server
import json
import os
import shutil
import subprocess
import threading
import unittest
from pathlib import Path

from pisar import codex_agent
from .support import Fixture, pisar_command

ENABLED = os.environ.get('PISAR_TEST_CODEX') == '1'
SKIP = 'set PISAR_TEST_CODEX=1 to check the generated Codex configuration with the installed codex'


class Capture:
    """A loopback server that records the first request Codex sends and then fails it."""

    def __init__(self):
        self.bodies = []
        capture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                capture.bodies.append(self.rfile.read(int(self.headers.get('content-length', 0))))
                self.send_response(500)
                self.send_header('content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"capture"}}')

            def log_message(self, *args):
                pass
        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def tools(self):
        body = json.loads(self.bodies[0])
        names = set()
        for tool in body.get('tools', []):
            names.add(tool.get('name') or tool.get('type') or '')
            names.update(t.get('name', '') for t in tool.get('tools', []) if isinstance(t, dict))
        return names


PROVIDER = ['-c', 'model_provider="loopback"', '-c', 'model="loopback"',
            '-c', 'model_providers.loopback.name="Loopback"', '-c', 'model_providers.loopback.wire_api="responses"',
            '-c', 'model_providers.loopback.requires_openai_auth=false',
            '-c', 'model_providers.loopback.request_max_retries=0',
            '-c', 'model_providers.loopback.stream_max_retries=0']


@unittest.skipUnless(ENABLED, SKIP)
class CodexSurfaceTests(Fixture):
    """The tool surface Codex offers with the generated configuration, against a loopback provider."""

    def setUp(self):
        super().setUp()
        self.executable = shutil.which('codex')
        if self.executable is None:
            self.fail('PISAR_TEST_CODEX=1 but codex is not on PATH')
        self.home = self.state / 'agents' / 'codex'
        self.home.mkdir(parents=True, mode=0o700)
        (self.home / 'home').mkdir(mode=0o700)
        codex_agent.write_config(self.home, self.root, self.state, shutil.which('ruwana') or 'ruwana', None, {})
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.env = {**os.environ, 'CODEX_HOME': str(self.home), 'HOME': str(self.home / 'home')}
        self.env.pop('OPENAI_API_KEY', None)

    def run_exec(self):
        subprocess.run([self.executable, 'exec', '--skip-git-repo-check', '-C', str(self.root), *PROVIDER,
                        '-c', f'model_providers.loopback.base_url="{self.capture.url}"', 'probe'],
                       env=self.env, capture_output=True, text=True, timeout=120)
        self.assertTrue(self.capture.bodies, 'codex sent no request to the loopback provider')

    def test_the_main_session_offers_no_shell_web_search_or_image_tools(self):
        self.run_exec()
        names = self.capture.tools()
        for forbidden in ('exec_command', 'write_stdin', 'shell', 'web_search', 'view_image', 'apply_patch'):
            self.assertNotIn(forbidden, names, sorted(names))
        self.assertTrue(any('pisar' in name for name in names), sorted(names))

    def test_refused_caller_arguments_never_reach_codex(self):
        env = {**self.env, 'PATH': f'{Path(self.executable).parent}{os.pathsep}{os.environ["PATH"]}',
               'XDG_CONFIG_HOME': str(self.base / 'config')}
        for extra in (['exec', '--ignore-user-config', '--ignore-rules', 'probe'], ['--oss'], ['-s', 'danger-full-access']):
            with self.subTest(extra=extra):
                done = subprocess.run([*pisar_command(), '--root', str(self.root), '--state-dir', str(self.state),
                                       '--agent', 'codex', '--', *extra], env=env, capture_output=True, text=True,
                                      timeout=120)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn('cannot be passed to the agent', done.stderr)
        self.assertEqual(self.capture.bodies, [])


@unittest.skipUnless(ENABLED, SKIP)
class CodexSmokeTests(Fixture):
    def setUp(self):
        super().setUp()
        self.executable = shutil.which('codex')
        if self.executable is None:
            self.fail('PISAR_TEST_CODEX=1 but codex is not on PATH')
        self.home = self.state / 'agents' / 'codex'
        self.home.mkdir(parents=True, mode=0o700)
        (self.home / 'home').mkdir(mode=0o700)
        codex_agent.write_config(self.home, self.root, self.state, shutil.which('ruwana') or 'ruwana', 'high', {})
        self.env = {**os.environ, 'CODEX_HOME': str(self.home), 'HOME': str(self.home / 'home')}

    def codex(self, *args):
        done = subprocess.run([self.executable, *args], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def test_real_codex_reads_the_pisar_server_and_its_tools(self):
        text = self.codex('mcp', 'get', 'pisar')
        self.assertIn('enabled_tools: pisar, git, lesson_accept, lesson_review, lesson_draft_read, lesson_draft_write, '
                      'sync_file_read, sync_file_write', text)
        self.assertIn('mcp-serve', text)
        self.assertIn(f'tool_timeout_sec: {codex_agent.SERVER_TOOL_TIMEOUT}', text)

    def test_real_codex_has_the_shell_and_subagent_features_off(self):
        text = self.codex('features', 'list')
        for name in ('shell_tool', 'multi_agent', 'plugins', 'hooks'):
            line = next(line for line in text.splitlines() if line.split()[:1] == [name])
            self.assertEqual(line.split()[-1], 'false', line)


if __name__ == '__main__':
    unittest.main()
