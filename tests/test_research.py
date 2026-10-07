import json
import os
from pathlib import Path
import stat
import unittest

from .support import Fixture

# A fake `claude`: records how it was called and prints canned output chosen by
# FAKE_CLAUDE_MODE. No network is ever used.
FAKE = '''#!/usr/bin/env python3
import json, os, sys
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
call = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ), "stdin": sys.stdin.read()}
with open(os.environ["FAKE_CLAUDE_OUT"], "w") as stream:
    json.dump(call, stream)
RESULT = json.loads(os.environ.get("FAKE_CLAUDE_RESULT", "null"))
if mode == "exit":
    sys.stderr.write("boom: not logged in\\n")
    sys.exit(3)
if mode == "malformed":
    print(json.dumps({"type": "result", "result": "not json at all"}))
elif mode == "text":
    print(json.dumps({"type": "result", "result": "```json\\n" + json.dumps(RESULT) + "\\n```"}))
elif mode == "garbage":
    print("<<<not an envelope>>>")
else:
    print(json.dumps({"type": "result", "result": "done", "structured_output": RESULT}))
'''

GOOD = {'summary': 'Teams use a short review.',
        'findings': [{'claim': 'Reviews take a day.', 'source_url': 'https://example.org/a',
                      'quote': 'a review takes one day'}],
        'gaps': ['No data for small teams.']}
QUESTION = 'How do teams run a short launch review?'


class ResearchTests(Fixture):
    def setUp(self):
        super().setUp()
        self.bin = self.base / 'fakebin'
        self.bin.mkdir()
        fake = self.bin / 'claude'
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.out = self.base / 'claude-call.json'
        self.fake_env = {**self.env, 'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}',
                         'FAKE_CLAUDE_OUT': str(self.out), 'FAKE_CLAUDE_RESULT': json.dumps(GOOD),
                         'PISAR_RUWANA_BIN': '/must/not/leak', 'WIKI_ROOT': '/must/not/leak'}

    def research(self, *args, mode='ok', ok=True, env=None):
        env = {**(env or self.fake_env), 'FAKE_CLAUDE_MODE': mode}
        return self.run_pisar('--root', self.root, '--state-dir', self.state, 'research', *args,
                              env=env, ok=ok)

    def call(self):
        return json.loads(self.out.read_text())

    def stored(self):
        return sorted((self.state / 'research').glob('*.json'))

    def config(self, text):
        path = self.base / 'config' / 'pisar' / 'config.toml'
        path.parent.mkdir(parents=True)
        path.write_text(text)

    # --- result and storage -------------------------------------------------

    def test_prints_and_stores_the_record_outside_git(self):
        p = self.research(QUESTION)
        printed = json.loads(p.stdout)
        files = self.stored()
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text())
        self.assertEqual({k: v for k, v in printed.items() if k != 'guard'}, record)
        self.assertEqual(files[0].name, f'{record["id"]}.json')
        self.assertEqual(set(record), {'schema_version', 'id', 'question', 'model', 'effort', 'retrieved_at',
                                       'untrusted', 'confirm_outbound', 'summary', 'findings', 'gaps'})
        self.assertEqual(record['schema_version'], 1)
        self.assertIs(record['untrusted'], True)
        self.assertIs(record['confirm_outbound'], False)
        self.assertEqual(record['question'], QUESTION)
        self.assertEqual(record['summary'], GOOD['summary'])
        self.assertEqual(record['findings'], GOOD['findings'])
        self.assertEqual(record['gaps'], GOOD['gaps'])
        self.assertTrue(record['retrieved_at'].endswith('Z'))
        self.assertTrue(files[0].resolve().is_relative_to(self.state.resolve()))

    def test_storage_is_private(self):
        self.research(QUESTION)
        self.assertEqual(stat.S_IMODE((self.state / 'research').stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.stored()[0].stat().st_mode), 0o600)

    def test_each_run_gets_its_own_id(self):
        self.research(QUESTION)
        self.research(QUESTION)
        self.assertEqual(len(self.stored()), 2)

    def test_result_text_with_a_code_fence_is_accepted(self):
        p = self.research(QUESTION, mode='text')
        self.assertEqual(json.loads(p.stdout)['summary'], GOOD['summary'])

    def test_output_says_the_result_is_data_not_instructions(self):
        record = json.loads(self.research(QUESTION).stdout)
        self.assertIs(record['untrusted'], True)
        self.assertIn('data', record['guard']['notice'].lower())

    def test_long_quotes_are_capped(self):
        long = {**GOOD, 'findings': [{**GOOD['findings'][0], 'quote': 'x' * 1000}]}
        env = {**self.fake_env, 'FAKE_CLAUDE_RESULT': json.dumps(long)}
        record = json.loads(self.research(QUESTION, env=env).stdout)
        quote = record['findings'][0]['quote']
        self.assertEqual(len(quote), 300)
        self.assertEqual(json.loads(self.stored()[0].read_text())['findings'][0]['quote'], quote)

    # --- strict validation --------------------------------------------------

    def rejects(self, result, message):
        before = set((self.state / 'research').glob('*.raw*')) if (self.state / 'research').exists() else set()
        env = {**self.fake_env, 'FAKE_CLAUDE_RESULT': json.dumps(result)}
        p = self.research(QUESTION, env=env, ok=False)
        self.assertIn(message, p.stderr)
        self.assertEqual(self.stored(), [])
        new = set((self.state / 'research').glob('*.raw*')) - before
        self.assertEqual(len(new), 1, 'raw output kept for diagnosis')

    def test_malformed_output_is_rejected_and_raw_output_kept(self):
        p = self.research(QUESTION, mode='malformed', ok=False)
        self.assertIn('not valid JSON', p.stderr)
        self.assertEqual(self.stored(), [])
        raw = list((self.state / 'research').glob('*.raw*'))
        self.assertEqual(len(raw), 1)
        self.assertIn('not json at all', raw[0].read_text())
        self.assertEqual(stat.S_IMODE(raw[0].stat().st_mode), 0o600)

    def test_output_that_is_not_an_envelope_is_rejected(self):
        p = self.research(QUESTION, mode='garbage', ok=False)
        self.assertIn('not valid JSON', p.stderr)
        self.assertEqual(self.stored(), [])

    def test_schema_violations_are_rejected(self):
        self.rejects({**GOOD, 'summary': ''}, 'summary')
        self.rejects({**GOOD, 'summary': 7}, 'summary')
        self.rejects({k: v for k, v in GOOD.items() if k != 'gaps'}, 'gaps')
        self.rejects({**GOOD, 'gaps': 'none'}, 'gaps')
        self.rejects({**GOOD, 'gaps': [1]}, 'gaps')
        self.rejects({**GOOD, 'extra': 1}, 'extra')
        self.rejects({**GOOD, 'findings': {}}, 'findings')
        bad = dict(GOOD['findings'][0])
        self.rejects({**GOOD, 'findings': [{**bad, 'claim': ''}]}, 'claim')
        self.rejects({**GOOD, 'findings': [{**bad, 'quote': None}]}, 'quote')
        self.rejects({**GOOD, 'findings': [{**bad, 'extra': 1}]}, 'extra')
        self.rejects({**GOOD, 'findings': [{**bad, 'source_url': 'file:///etc/passwd'}]}, 'source_url')
        self.rejects({**GOOD, 'findings': [{**bad, 'source_url': 'ftp://example.org/'}]}, 'source_url')
        self.rejects({**GOOD, 'findings': [{k: v for k, v in bad.items() if k != 'quote'}]}, 'quote')

    def test_no_result_at_all_is_rejected(self):
        env = {**self.fake_env, 'FAKE_CLAUDE_RESULT': 'null'}
        self.research(QUESTION, env=env, ok=False)
        self.assertEqual(self.stored(), [])

    def test_nonzero_exit_is_reported_and_nothing_stored(self):
        p = self.research(QUESTION, mode='exit', ok=False)
        self.assertIn('claude exited with status 3', p.stderr)
        self.assertIn('not logged in', p.stderr)
        self.assertEqual(self.stored(), [])

    def test_missing_binary_is_a_clear_error(self):
        env = {**self.fake_env, 'PATH': str(self.base / 'empty')}
        (self.base / 'empty').mkdir()
        p = self.research(QUESTION, env=env, ok=False)
        self.assertIn('claude is not on PATH', p.stderr)
        self.assertEqual(self.stored(), [])

    def test_empty_question_is_refused_before_any_call(self):
        self.research('   ', ok=False)
        self.assertFalse(self.out.exists())

    # --- guard gate ---------------------------------------------------------

    def test_findings_stop_the_run_before_any_network_use(self):
        p = self.research('What is the launch plan for alpha at https://example.org/x?', ok=False)
        report = json.loads(p.stdout)
        self.assertIs(report['ok'], False)
        self.assertTrue(report['findings'])
        self.assertIn('--confirm-outbound', report['message'])
        categories = {f['category'] for f in report['findings']}
        self.assertTrue({'url', 'term'} <= categories, categories)
        self.assertFalse(self.out.exists(), 'claude must not be started')
        self.assertEqual(self.stored(), [])
        self.assertFalse((self.state / 'agents').exists())

    def test_terms_of_every_domain_are_checked(self):
        for text in ('Notes about alpha', 'Notes about home'):
            self.research(text, ok=False)
        self.assertFalse(self.out.exists())

    def test_confirmed_findings_are_sent_recorded_and_printed(self):
        question = 'What is the launch plan for alpha?'
        p = self.research(question, '--confirm-outbound')
        printed = json.loads(p.stdout)
        self.assertIs(printed['confirm_outbound'], True)
        self.assertTrue(printed['guard']['findings'])
        self.assertEqual(self.call()['stdin'], question)
        self.assertIs(json.loads(self.stored()[0].read_text())['confirm_outbound'], True)

    def test_confirm_flag_without_findings_is_still_recorded(self):
        self.research(QUESTION, '--confirm-outbound')
        self.assertIs(json.loads(self.stored()[0].read_text())['confirm_outbound'], True)

    def test_from_must_name_a_known_domain(self):
        self.research(QUESTION, '--from', 'work')
        self.research(QUESTION, '--from', 'nowhere', ok=False)

    # --- isolation ----------------------------------------------------------

    def test_subprocess_is_isolated(self):
        self.research(QUESTION)
        call = self.call()
        argv = call['argv']
        self.assertEqual(call['stdin'], QUESTION)
        self.assertIn('-p', argv)
        self.assertIn('--tools=WebSearch,WebFetch', argv)
        start = argv.index('--allowedTools') + 1
        self.assertEqual(argv[start:start + 2], ['WebSearch', 'WebFetch'])
        self.assertIn('--strict-mcp-config', argv)
        self.assertNotIn('--mcp-config', argv)
        self.assertIn('--no-session-persistence', argv)
        self.assertNotIn('--plugin-dir', argv)
        self.assertIn('--system-prompt', argv)
        cwd = Path(call['cwd']).resolve()
        self.assertNotIn(cwd, (self.root.resolve(), self.state.resolve(), Path.cwd().resolve()))
        self.assertFalse(cwd.is_relative_to(self.root.resolve()))
        self.assertFalse(cwd.is_relative_to(self.state.resolve()))
        self.assertFalse(cwd.exists(), 'temporary working directory is removed afterwards')
        env = call['env']
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(self.state.resolve() / 'agents' / 'claude'))
        self.assertFalse([k for k in env if k.startswith('PISAR_') or k == 'WIKI_ROOT'])

    def test_system_prompt_is_a_researcher_that_distrusts_pages(self):
        self.research(QUESTION)
        argv = self.call()['argv']
        prompt = argv[argv.index('--system-prompt') + 1]
        self.assertIn('untrusted', prompt)
        self.assertIn('WebSearch', prompt)
        for tool in ('Bash', 'Write', 'Edit', 'Glob'):
            self.assertNotIn(tool, prompt)

    def test_config_dir_is_the_launchers_and_private(self):
        self.research(QUESTION)
        config = self.state / 'agents' / 'claude'
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o700)

    def test_config_dir_symlink_is_refused(self):
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        (self.state / 'agents').parent.mkdir(parents=True, exist_ok=True)
        (self.state / 'agents').symlink_to(elsewhere)
        p = self.research(QUESTION, ok=False)
        self.assertIn('symlink', p.stderr)
        self.assertFalse(self.out.exists())

    def test_config_dir_inside_git_is_refused(self):
        state = self.root / 'state'
        p = self.run_pisar('--root', self.root, '--state-dir', state, 'research', QUESTION,
                           env={**self.fake_env, 'FAKE_CLAUDE_MODE': 'ok'}, ok=False)
        self.assertFalse(self.out.exists())
        self.assertTrue(p.stderr)

    # --- model and effort precedence ---------------------------------------

    def option(self, argv, flag):
        return argv[argv.index(flag) + 1] if flag in argv else None

    def test_defaults_are_sonnet_medium(self):
        record = json.loads(self.research(QUESTION).stdout)
        argv = self.call()['argv']
        self.assertEqual((self.option(argv, '--model'), self.option(argv, '--effort')), ('sonnet', 'medium'))
        self.assertEqual((record['model'], record['effort']), ('sonnet', 'medium'))

    def test_config_researcher_beats_the_default(self):
        self.config('[agents.claude.researcher]\nmodel = "opus"\neffort = "high"\n')
        record = json.loads(self.research(QUESTION).stdout)
        argv = self.call()['argv']
        self.assertEqual((self.option(argv, '--model'), self.option(argv, '--effort')), ('opus', 'high'))
        self.assertEqual((record['model'], record['effort']), ('opus', 'high'))

    def test_the_main_session_settings_do_not_apply_to_research(self):
        self.config('[agents.claude]\nmodel = "haiku"\neffort = "low"\n')
        self.research(QUESTION)
        argv = self.call()['argv']
        self.assertEqual((self.option(argv, '--model'), self.option(argv, '--effort')), ('sonnet', 'medium'))

    def test_flags_beat_the_config_and_are_passed_verbatim(self):
        self.config('[agents.claude.researcher]\nmodel = "opus"\neffort = "high"\n')
        record = json.loads(self.research(QUESTION, '--model', 'any-model-9', '--effort', 'xhigh').stdout)
        argv = self.call()['argv']
        self.assertEqual((self.option(argv, '--model'), self.option(argv, '--effort')), ('any-model-9', 'xhigh'))
        self.assertEqual((record['model'], record['effort']), ('any-model-9', 'xhigh'))

    def test_a_flag_overrides_only_its_own_setting(self):
        self.config('[agents.claude.researcher]\nmodel = "opus"\neffort = "high"\n')
        self.research(QUESTION, '--effort', 'low')
        argv = self.call()['argv']
        self.assertEqual((self.option(argv, '--model'), self.option(argv, '--effort')), ('opus', 'low'))

    def test_question_starting_with_a_dash_is_data(self):
        self.research('--model evil how do teams plan?')
        call = self.call()
        self.assertEqual(call['stdin'], '--model evil how do teams plan?')
        self.assertNotIn('evil', call['argv'])


if __name__ == '__main__':
    unittest.main()
