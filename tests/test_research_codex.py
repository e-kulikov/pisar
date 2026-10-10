"""`pisar research` with research_agent = "codex": the same guard, bounds and record, a Codex researcher."""
import json
import os
from pathlib import Path
import stat
import tomllib
from unittest import mock

from pisar import research
from pisar.safety import WikiError
from .support import Fixture

ANSWER = {'summary': 'Synthetic summary.',
          'findings': [{'claim': 'Synthetic claim.', 'source_url': 'https://example.invalid/page',
                        'quote': 'Synthetic quote.'}],
          'gaps': ['Synthetic gap.']}


def fake_codex(log, answer):
    return f'''#!/usr/bin/env python3
import json, os, sys
question = sys.stdin.read()
with open({str(log)!r}, "w") as stream:
    json.dump({{"argv": sys.argv[1:], "home": os.environ.get("CODEX_HOME"), "question": question,
               "env": sorted(os.environ)}}, stream)
out = sys.argv[sys.argv.index("-o") + 1]
body = {answer!r}
if body is None:
    sys.exit(0)
open(out, "w").write(body)
'''


class CodexResearchTests(Fixture):
    def setUp(self):
        super().setUp()
        self.bin = self.base / 'fakebin'
        self.bin.mkdir()
        self.log = self.base / 'codex-research.json'
        self.config_file = self.base / 'config/pisar/config.toml'
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text('research_agent = "codex"\n')
        patch = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.base / 'config')})
        patch.start()
        self.addCleanup(patch.stop)

    def install(self, answer=json.dumps(ANSWER)):
        fake = self.bin / 'codex'
        fake.write_text(fake_codex(self.log, answer))
        fake.chmod(0o755)
        patch = mock.patch.dict(os.environ, {'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}'})
        patch.start()
        self.addCleanup(patch.stop)

    def ask(self, question='Synthetic public question about travel rules.', **kwargs):
        return research.run(self.root, self.state, question, **kwargs)

    def stored(self):
        return sorted((self.state / 'research').glob('*.json'))

    def call(self):
        return json.loads(self.log.read_text())

    def test_codex_researcher_returns_and_stores_a_validated_record(self):
        self.install()
        record = self.ask()
        self.assertTrue(record['untrusted'])
        self.assertEqual(record['summary'], 'Synthetic summary.')
        self.assertEqual(record['model'], 'gpt-5.5')
        self.assertEqual(record['effort'], 'medium')
        self.assertEqual(len(self.stored()), 1)
        self.assertEqual(json.loads(self.stored()[0].read_text())['id'], record['id'])

    def test_researcher_is_web_search_only_with_no_shell_tools_or_servers(self):
        self.install()
        self.ask()
        home = self.state.resolve() / 'agents' / 'codex' / 'researcher'
        call = self.call()
        self.assertEqual(call['home'], str(home))
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        config = tomllib.loads((home / 'config.toml').read_text())
        self.assertEqual(config['web_search'], 'live')
        self.assertEqual(config['sandbox_mode'], 'read-only')
        self.assertEqual(config['mcp_servers'], {})
        for name in ('shell_tool', 'multi_agent', 'apps', 'plugins', 'hooks', 'browser_use'):
            self.assertIs(config['features'][name], False, name)
        self.assertIn('web researcher', (home / 'instructions.md').read_text())

    def test_arguments_and_question_are_passed_without_being_read_as_options(self):
        self.install()
        self.ask('--looks-like-an-option question')
        call = self.call()
        argv = call['argv']
        for expected in ('exec', '--ephemeral', '--output-schema', '-s', 'read-only', '-o', '-m'):
            self.assertIn(expected, argv)
        self.assertEqual(argv[argv.index('-m') + 1], 'gpt-5.5')
        self.assertEqual(argv[-1], '-')
        self.assertEqual(call['question'], '--looks-like-an-option question')

    def test_environment_is_an_allowlist_and_never_carries_wiki_root(self):
        self.install()
        with mock.patch.dict(os.environ, {'WIKI_ROOT': '/no', 'SECRET_FOR_TEST': 'x'}):
            self.ask()
        env = self.call()['env']
        self.assertNotIn('WIKI_ROOT', env)
        self.assertNotIn('SECRET_FOR_TEST', env)

    def test_guard_findings_still_stop_the_run_before_codex_is_started(self):
        self.install()
        record = self.ask('Write to anna.example@example.invalid about the plan.')
        self.assertFalse(record['ok'])
        self.assertTrue(record['gated'])
        self.assertFalse(self.log.exists())
        self.assertEqual(self.stored(), [])

    def test_invalid_answer_is_rejected_and_diagnostics_are_kept_privately(self):
        self.install(answer='{"summary": 1}')
        with self.assertRaisesRegex(WikiError, 'diagnostics kept privately'):
            self.ask()
        self.assertEqual(self.stored(), [])

    def test_missing_answer_is_an_error(self):
        self.install(answer=None)
        with self.assertRaisesRegex(WikiError, 'not valid JSON'):
            self.ask()

    def test_model_and_effort_come_from_the_researcher_role(self):
        self.config_file.write_text('research_agent = "codex"\n[agents.codex.researcher]\nmodel = "m2"\n'
                                    'effort = "low"\n')
        self.install()
        record = self.ask()
        self.assertEqual((record['model'], record['effort']), ('m2', 'low'))
        home = self.state.resolve() / 'agents' / 'codex' / 'researcher'
        self.assertEqual(tomllib.loads((home / 'config.toml').read_text())['model_reasoning_effort'], 'low')

    def test_claude_stays_the_default_researcher(self):
        self.config_file.write_text('')
        self.install()
        with mock.patch('pisar.research.shutil.which', return_value=None):
            with self.assertRaisesRegex(WikiError, 'claude is not on PATH'):
                self.ask()
        self.assertFalse(self.log.exists())


if __name__ == '__main__':
    import unittest
    unittest.main()
