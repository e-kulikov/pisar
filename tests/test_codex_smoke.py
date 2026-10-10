"""Checks against the real `codex` executable, gated by PISAR_TEST_CODEX (skipped otherwise).

Only configuration commands run here (`codex mcp get`, `codex features list`): they read the
generated CODEX_HOME and never call a model. A run that does call the model is a manual step
documented in the README; it is not part of the suite.
"""
import os
import shutil
import subprocess
import unittest

from pisar import codex_agent
from .support import Fixture

ENABLED = os.environ.get('PISAR_TEST_CODEX') == '1'
SKIP = 'set PISAR_TEST_CODEX=1 to check the generated Codex configuration with the installed codex'


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
        self.assertIn('enabled_tools: pisar, git, lesson_accept, lesson_review, lesson_draft_read, lesson_draft_write', text)
        self.assertIn('mcp-serve', text)
        self.assertIn(f'tool_timeout_sec: {codex_agent.SERVER_TOOL_TIMEOUT}', text)

    def test_real_codex_has_the_shell_and_subagent_features_off(self):
        text = self.codex('features', 'list')
        for name in ('shell_tool', 'multi_agent', 'plugins', 'hooks'):
            line = next(line for line in text.splitlines() if line.split()[:1] == [name])
            self.assertEqual(line.split()[-1], 'false', line)


if __name__ == '__main__':
    unittest.main()
