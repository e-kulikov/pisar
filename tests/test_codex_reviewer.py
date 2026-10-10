"""The Codex lesson reviewer: a tool-less, read-only `codex exec` run whose answer is recorded by lesson.review."""
import json
import os
from pathlib import Path
from unittest import mock

from pisar import codex_reviewer, lesson
from pisar.safety import WikiError
from .support import Fixture

LESSON_TEXT = 'Synthetic general lesson: check the plan before the call.\n'


def fake_reviewer(mode, log):
    return f'''#!/usr/bin/env python3
import json, os, re, sys
prompt = sys.stdin.read()
with open({str(log)!r}, "w") as stream:
    json.dump({{"argv": sys.argv[1:], "home": os.environ.get("CODEX_HOME"),
               "env_home": os.environ.get("HOME"), "prompt": prompt,
               "env": sorted(os.environ)}}, stream)
mode = {mode!r}
if mode == "fail":
    sys.exit(3)
out = sys.argv[sys.argv.index("-o") + 1]
if mode == "bad-json":
    open(out, "w").write("not json")
    sys.exit(0)
revision, digest = re.search(r"Revision (\\S+), sha256 ([0-9a-f]+)\\.", prompt).groups()
if mode == "stale":
    revision = "r99"
answer = {{"schema_version": 1, "revision": revision, "sha256": digest, "verdict": "concerns",
          "findings": [{{"tier": "warn", "category": "date", "excerpt": "Synthetic",
                        "comment": "Synthetic comment", "suggestion": ""}}],
          "reviewer": {{"model": "ignored"}}}}
open(out, "w").write(json.dumps(answer))
'''


class CodexReviewerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.bin = self.base / 'fakebin'
        self.bin.mkdir()
        self.log = self.base / 'reviewer-call.json'
        self.config_file = self.base / 'config/pisar/config.toml'
        self.patches = [mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.base / 'config')})]
        self.patches[0].start()
        self.addCleanup(self.patches[0].stop)
        self.batch = None

    def install(self, mode):
        fake = self.bin / 'codex'
        fake.write_text(fake_reviewer(mode, self.log))
        fake.chmod(0o755)
        patch = mock.patch.dict(os.environ, {'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}'})
        patch.start()
        self.addCleanup(patch.stop)

    def start_batch(self):
        data = self.data('lesson', 'start', '--from', 'work', '--to', 'work/beta', '--title', 'Plan')
        self.batch = data['batch']
        draft = Path(data['draft'])
        draft.write_text(LESSON_TEXT)
        check = self.data('lesson', 'check', '--batch', self.batch)
        return check

    def review(self):
        return codex_reviewer.review(self.root, self.state, self.batch)

    def journal_events(self):
        path = self.state / 'lessons' / self.batch / 'journal.json'
        return [e['event'] for e in json.loads(path.read_text())['events']]

    def call(self):
        return json.loads(self.log.read_text())

    def test_reviewer_runs_codex_exec_read_only_with_the_reviewer_model_and_records_the_review(self):
        self.install('ok')
        check = self.start_batch()
        result = self.review()
        self.assertEqual(result['verdict'], 'concerns')
        self.assertEqual(result['revision'], check['revision'])
        self.assertIn('review', self.journal_events())
        call = self.call()
        argv = call['argv']
        for expected in ('exec', '--ephemeral', '--skip-git-repo-check', '-s', 'read-only', '--output-schema', '-o'):
            self.assertIn(expected, argv)
        self.assertEqual(argv[argv.index('-m') + 1], 'gpt-6.1-sol')
        self.assertEqual(argv[-1], '-')
        self.assertIn(LESSON_TEXT.strip(), call['prompt'])
        self.assertIn(f'sha256 {check["sha256"]}', call['prompt'])

    def test_reviewer_shares_the_login_and_ignores_user_configuration_but_not_its_confinement(self):
        self.install('ok')
        self.start_batch()
        self.review()
        main = self.state.resolve() / 'agents' / 'codex'
        call = self.call()
        self.assertEqual(call['home'], str(main), 'the reviewer must read the login of the main session')
        self.assertEqual(call['env_home'], str(main / 'home'))
        argv = call['argv']
        for flag in ('--ignore-user-config', '--ignore-rules', '--ephemeral'):
            self.assertIn(flag, argv)
        for setting in ('web_search="disabled"', 'approval_policy="never"'):
            self.assertIn(setting, argv)
        self.assertIn('-s', argv)
        self.assertEqual(argv[argv.index('-s') + 1], 'read-only')
        pairs = list(zip(argv, argv[1:]))
        for feature in ('shell_tool', 'multi_agent', 'apps', 'plugins', 'hooks'):
            self.assertIn(('--disable', feature), pairs)
        setting = next(a for a in argv if a.startswith('model_instructions_file='))
        instructions = Path(json.loads(setting.split('=', 1)[1]))
        self.assertIn('You review a draft note', instructions.read_text())
        self.assertNotIn('WIKI_ROOT', call['env'])

    def test_reviewer_model_and_effort_come_from_the_config(self):
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text('[agents.codex.reviewer]\nmodel = "my-reviewer"\neffort = "low"\n')
        self.install('ok')
        self.start_batch()
        self.review()
        argv = self.call()['argv']
        self.assertEqual(argv[argv.index('-m') + 1], 'my-reviewer')
        self.assertIn('model_reasoning_effort="low"', argv)
        event = json.loads((self.state / 'lessons' / self.batch / 'journal.json').read_text())['events'][-1]
        self.assertEqual(event['reviewer'], {'model': 'my-reviewer'})

    def test_invalid_answer_records_nothing(self):
        self.install('bad-json')
        self.start_batch()
        with self.assertRaisesRegex(WikiError, 'not valid JSON'):
            self.review()
        self.assertNotIn('review', self.journal_events())

    def test_failed_reviewer_records_nothing(self):
        self.install('fail')
        self.start_batch()
        with self.assertRaisesRegex(WikiError, 'reviewer failed'):
            self.review()
        self.assertNotIn('review', self.journal_events())

    def test_answer_for_another_revision_is_refused(self):
        self.install('stale')
        self.start_batch()
        with self.assertRaisesRegex(WikiError, 'does not cover the current revision'):
            self.review()
        self.assertNotIn('review', self.journal_events())

    def test_missing_codex_is_a_clear_error(self):
        self.start_batch()
        with mock.patch.dict(os.environ, {'PATH': str(self.bin)}):
            with self.assertRaisesRegex(WikiError, 'codex is not on PATH'):
                self.review()

    def test_reviewer_environment_is_an_allowlist(self):
        self.install('ok')
        self.start_batch()
        with mock.patch.dict(os.environ, {'SECRET_FOR_TEST': 'x', 'WIKI_ROOT': '/no'}):
            self.review()
        env = self.call()['env']
        self.assertNotIn('SECRET_FOR_TEST', env)
        self.assertNotIn('WIKI_ROOT', env)

    def test_the_lesson_review_tool_of_the_server_records_the_review(self):
        from .test_codex_mcp import McpClient
        self.install('ok')
        self.start_batch()
        self.env = {**self.env, 'PATH': f'{self.bin}{os.pathsep}{self.env["PATH"]}'}
        client = McpClient(self, ['--root', str(self.root), '--state-dir', str(self.state),
                                  '--ruwana', 'ruwana', 'mcp-serve'])
        client.initialize()
        text, error = client.text('lesson_review', ['--batch', self.batch])
        self.assertFalse(error, text)
        self.assertEqual(json.loads(text)['verdict'], 'concerns')
        self.assertIn('review', self.journal_events())
        text, error = client.text('lesson_review', ['--batch', self.batch, '--file', 'x'])
        self.assertTrue(error)
        self.assertIn('arguments are', text)

    def test_schema_describes_exactly_the_review_keys(self):
        self.assertEqual(set(codex_reviewer.SCHEMA['properties']), lesson.REVIEW_KEYS)
        self.assertEqual(set(codex_reviewer.SCHEMA['required']), lesson.REVIEW_KEYS)
        finding = codex_reviewer.SCHEMA['properties']['findings']['items']
        self.assertEqual(set(finding['required']), {*lesson.REVIEW_REQUIRED, 'suggestion'})


if __name__ == '__main__':
    import unittest
    unittest.main()
