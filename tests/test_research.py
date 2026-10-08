import json
import os
from pathlib import Path
import stat
import sys
import time
import unittest
from unittest import mock

from pisar import research

from .support import Fixture

# A fake `claude`: records how it was called and prints canned output chosen by
# FAKE_CLAUDE_MODE. No network is ever used.
FAKE = '''#!/usr/bin/env python3
import json, os, sys
# The child environment is an allowlist, so the fake is configured by a file beside it.
with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "fake.json")) as stream:
    conf = json.load(stream)
mode = conf["mode"]
call = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ), "stdin": sys.stdin.read()}
with open(conf["out"], "w") as stream:
    json.dump(call, stream)
RESULT = json.loads(conf["result"])
# Like the real claude (verified live), hooks of the configuration directory
# run at session start unless --safe-mode disables all customisations.
settings = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", ""), "settings.json")
if "--safe-mode" not in sys.argv and os.path.isfile(settings):
    import subprocess
    with open(settings) as stream:
        for group in json.load(stream).get("hooks", {}).get("SessionStart", []):
            for hook in group["hooks"]:
                subprocess.run(hook["command"], shell=True)
if mode == "orphan":
    import subprocess
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    print(json.dumps({"type": "result", "result": "done", "structured_output": RESULT}))
    sys.stdout.flush()
    sys.exit(0)
if mode == "sleep":
    import time
    time.sleep(60)
if mode == "huge":
    sys.stdout.write("x" * (3 * 1024 * 1024))
    sys.exit(0)
if mode == "noisy":
    sys.stderr.write("e" * (1024 * 1024))
    sys.exit(4)
if mode == "iserror":
    print(json.dumps({"type": "result", "is_error": True, "result": "SECRET-RAW-RESPONSE"}))
    sys.exit(0)
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
                         'FAKE_CLAUDE_RESULT': json.dumps(GOOD),
                         'PISAR_RUWANA_BIN': '/must/not/leak', 'WIKI_ROOT': '/must/not/leak'}

    def fake(self, mode='ok', result=None):
        (self.bin / 'fake.json').write_text(json.dumps(
            {'mode': mode, 'out': str(self.out), 'result': result or json.dumps(GOOD)}))

    def research(self, *args, mode='ok', ok=True, env=None):
        env = env or self.fake_env
        self.fake(mode, env.get('FAKE_CLAUDE_RESULT'))
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
        self.assertNotIn('not logged in', p.stderr)
        self.assertEqual(self.stored(), [])
        self.assertIn('not logged in', self.diagnostics('stderr').read_text())

    def test_missing_binary_is_a_clear_error(self):
        # Only a python3 (for the zipapp's shebang), no claude.
        (self.base / 'empty').mkdir()
        (self.base / 'empty' / 'python3').symlink_to(sys.executable)
        env = {**self.fake_env, 'PATH': str(self.base / 'empty')}
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
        self.fake()
        p = self.run_pisar('--root', self.root, '--state-dir', state, 'research', QUESTION,
                           env=self.fake_env, ok=False)
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

    # --- review fixes -------------------------------------------------------

    def diagnostics(self, kind):
        name = {'stdout': '*.raw.txt', 'stderr': '*.stderr.txt'}[kind]
        found = list((self.state / 'research').glob(name))
        self.assertEqual(len(found), 1, found)
        self.assertEqual(stat.S_IMODE(found[0].stat().st_mode), 0o600)
        return found[0]

    def in_process(self, **patches):
        """research.run in this process with the fake claude on PATH (for patched limits)."""
        self.fake()
        self.enterContext(mock.patch.dict(os.environ, self.fake_env, clear=True))
        for name, value in patches.items():
            self.enterContext(mock.patch.object(research, name, value))
        return lambda **kw: research.run(self.root, self.state, QUESTION, **kw)

    def test_hooks_and_customisations_of_the_shared_config_do_not_run(self):
        config = self.state / 'agents' / 'claude'
        config.mkdir(parents=True)
        marker = self.base / 'hook-ran'
        (config / 'settings.json').write_text(json.dumps({'hooks': {'SessionStart': [
            {'hooks': [{'type': 'command', 'command': f'touch {marker}'}]}]}}))
        self.research(QUESTION)
        self.assertIn('--safe-mode', self.call()['argv'])
        self.assertFalse(marker.exists(), 'a SessionStart hook of the shared config ran')

    PRIVATE = {'GITHUB_TOKEN': 'x1', 'AWS_SECRET_ACCESS_KEY': 'x2', 'MY_PASSWORD': 'x3', 'SSH_AUTH_SOCK': '/s',
               'OPENAI_API_KEY': 'x4', 'XDG_DATA_HOME_SECRET': 'x5', 'PISAR_ROOT': '/r', 'WIKI_ROOT': '/w'}
    PASSED = {'HTTP_PROXY': 'p1', 'HTTPS_PROXY': 'p2', 'NO_PROXY': 'p3', 'http_proxy': 'p4',
              'https_proxy': 'p5', 'no_proxy': 'p6', 'SSL_CERT_FILE': 'c1', 'SSL_CERT_DIR': 'c2',
              'NODE_EXTRA_CA_CERTS': 'c3', 'REQUESTS_CA_BUNDLE': 'c4', 'CURL_CA_BUNDLE': 'c5',
              'ANTHROPIC_API_KEY': 'a1', 'ANTHROPIC_BASE_URL': 'a2', 'CLAUDE_CODE_USE_FOUNDRY': '1',
              'LANG': 'en_US.UTF-8', 'LC_ALL': 'C.UTF-8', 'LC_TIME': 'C', 'TERM': 'xterm',
              'HOME': '/home/someone', 'USER': 'someone', 'LOGNAME': 'someone'}

    def test_only_an_allowlist_of_environment_variables_reaches_claude(self):
        env = {**self.fake_env, **self.PRIVATE, **self.PASSED}
        self.research(QUESTION, env=env)
        child = self.call()['env']
        for name in self.PRIVATE:
            self.assertNotIn(name, child)
        for name, value in self.PASSED.items():
            self.assertEqual(child.get(name), value, name)
        self.assertIn(str(self.bin), child['PATH'])
        self.assertEqual(child['CLAUDE_CONFIG_DIR'], str(self.state.resolve() / 'agents' / 'claude'))

    def test_unrelated_variables_with_vetted_prefixes_do_not_reach_claude(self):
        unrelated = {'ANTHROPIC_INTERNAL_CLIENT_NOTE': 'n1', 'CLAUDE_CODE_PRIVATE_NOTE': 'n2',
                     'LC_PRIVATE': 'ok-locale-like'}  # LC_* is locale by definition and stays
        self.research(QUESTION, env={**self.fake_env, **unrelated})
        child = self.call()['env']
        self.assertNotIn('ANTHROPIC_INTERNAL_CLIENT_NOTE', child)
        self.assertNotIn('CLAUDE_CODE_PRIVATE_NOTE', child)

    def test_vetted_authentication_and_provider_variables_reach_claude(self):
        vetted = {'ANTHROPIC_API_KEY': '1', 'ANTHROPIC_AUTH_TOKEN': '2', 'ANTHROPIC_BASE_URL': '3',
                  'ANTHROPIC_CUSTOM_HEADERS': '4', 'ANTHROPIC_MODEL': '5', 'CLAUDE_CODE_OAUTH_TOKEN': '6',
                  'CLAUDE_CODE_USE_BEDROCK': '', 'CLAUDE_CODE_USE_VERTEX': '', 'CLAUDE_CODE_USE_FOUNDRY': ''}
        self.research(QUESTION, env={**self.fake_env, **vetted})
        child = self.call()['env']
        for name, value in vetted.items():
            self.assertEqual(child.get(name), value, name)

    def test_cloud_credentials_pass_only_for_their_selected_provider(self):
        cloud = {'AWS_ACCESS_KEY_ID': 'a', 'AWS_SECRET_ACCESS_KEY': 'b', 'AWS_REGION': 'r',
                 'GOOGLE_APPLICATION_CREDENTIALS': '/g', 'CLOUD_ML_REGION': 'm', 'ANTHROPIC_FOUNDRY_API_KEY': 'f'}
        self.research(QUESTION, env={**self.fake_env, **cloud})
        self.assertFalse(set(cloud) & set(self.call()['env']))
        self.research(QUESTION, env={**self.fake_env, **cloud, 'CLAUDE_CODE_USE_BEDROCK': '1'})
        child = self.call()['env']
        self.assertEqual({k: child.get(k) for k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_REGION')},
                         {'AWS_ACCESS_KEY_ID': 'a', 'AWS_SECRET_ACCESS_KEY': 'b', 'AWS_REGION': 'r'})
        self.assertNotIn('GOOGLE_APPLICATION_CREDENTIALS', child)
        self.assertNotIn('ANTHROPIC_FOUNDRY_API_KEY', child)
        self.research(QUESTION, env={**self.fake_env, **cloud, 'CLAUDE_CODE_USE_VERTEX': '1'})
        child = self.call()['env']
        self.assertEqual(child['GOOGLE_APPLICATION_CREDENTIALS'], '/g')
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', child)

    # --- one deadline for the whole subprocess ------------------------------

    def bounded(self, code, question='q', timeout=0.5):
        with mock.patch.object(research, 'TIMEOUT', timeout):
            started = time.monotonic()
            done = research.run_bounded([sys.executable, '-c', code], question, str(self.base), dict(os.environ))
        return done, time.monotonic() - started

    def test_the_deadline_covers_writing_a_question_the_child_does_not_read(self):
        done, took = self.bounded('import time; time.sleep(30)', question='я' * (4 * 1024 * 1024))
        self.assertEqual(done.state, 'timeout')
        self.assertLess(took, 5)

    def test_a_descendant_holding_the_pipes_is_never_reported_as_ok(self):
        code = ('import subprocess, sys; '
                'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
                'print(p.pid, flush=True)')
        done, took = self.bounded(code, timeout=60)
        self.assertEqual(done.state, 'orphan')
        self.assertLess(took, 15)
        self.assertGone(int(done.stdout.split()[0]))

    def test_a_quiet_descendant_is_stopped_but_the_run_is_ok(self):
        code = ('import subprocess, sys; '
                'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], '
                'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); '
                'print(p.pid, flush=True)')
        done, took = self.bounded(code, timeout=60)
        self.assertEqual(done.state, 'ok')
        self.assertLess(took, 5)
        self.assertGone(int(done.stdout.split()[0]))

    def assertGone(self, pid):
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(pid, 9)
        self.fail('a descendant of the child is still alive')

    def test_an_orphaned_run_is_a_failure_with_diagnostics(self):
        run = self.in_process(TIMEOUT=60)
        self.fake('orphan')
        with self.assertRaisesRegex(research.WikiError, 'background process') as caught:
            run()
        self.assertIn('diagnostics kept', str(caught.exception))
        self.assertEqual(self.stored(), [])

    def test_the_size_limit_applies_to_the_exact_bytes_written(self):
        run = self.in_process()
        run()
        file = self.stored()[0]
        size = file.stat().st_size
        compact = len(json.dumps(json.loads(file.read_text()), ensure_ascii=False).encode())
        # The written (indented) file is larger than the compact JSON: the limit must follow the file.
        self.assertLess(compact, size)
        file.unlink()
        with mock.patch.object(research, 'RECORD_LIMIT', size):
            run()
        self.assertEqual([f.stat().st_size for f in self.stored()], [size])
        self.stored()[0].unlink()
        with mock.patch.object(research, 'RECORD_LIMIT', size - 1):
            with self.assertRaisesRegex(research.WikiError, 'record is too large'):
                run()
        self.assertEqual(self.stored(), [])

    LOCALE = ('LC_ALL', 'LC_CTYPE', 'LC_NUMERIC', 'LC_TIME', 'LC_COLLATE', 'LC_MONETARY', 'LC_MESSAGES',
              'LC_PAPER', 'LC_NAME', 'LC_ADDRESS', 'LC_TELEPHONE', 'LC_MEASUREMENT', 'LC_IDENTIFICATION',
              'LANG', 'LANGUAGE')

    def test_only_the_named_locale_variables_pass_and_no_prefix_is_matched(self):
        listed = {name: 'C.UTF-8' for name in self.LOCALE}
        unrelated = {'LC_PRIVATE_CLIENT_NOTE': 'synthetic-client-secret', 'LC_X': 'x',
                     'ANTHROPIC_INTERNAL_CLIENT_NOTE': 'n', 'CLAUDE_CODE_PRIVATE_NOTE': 'n'}
        self.research(QUESTION, env={**self.fake_env, **listed, **unrelated})
        child = self.call()['env']
        for name, value in listed.items():
            self.assertEqual(child.get(name), value, name)
        for name in unrelated:
            self.assertNotIn(name, child)

    def test_no_prefix_rule_remains_in_the_environment_allowlist(self):
        self.assertFalse(hasattr(research, 'ENV_PREFIXES'))

    # --- malformed domain markers fail closed -------------------------------

    TYPO = 'schema_version = 1\nid = "{0}"\ntitle = "T"\n[sensitive]\nterm = ["CedarSecret"]\n'

    def test_a_malformed_domain_marker_prevents_the_launch(self):
        (self.root / 'work' / '.domain.toml').write_text(self.TYPO.format('work'))
        p = self.research('How can CedarSecret improve?', ok=False)
        self.assertFalse(self.out.exists(), 'claude must not be started')
        self.assertIn('work/.domain.toml', p.stderr)
        self.assertIn('unknown key in [sensitive]: term', p.stderr)
        self.assertEqual(self.stored(), [])

    def test_a_malformed_other_domain_also_blocks_outbound_research(self):
        (self.root / 'personal' / '.domain.toml').write_text(self.TYPO.format('personal'))
        p = self.research(QUESTION, '--from', 'work', ok=False)
        self.assertFalse(self.out.exists())
        self.assertIn('personal/.domain.toml', p.stderr)

    def test_a_malformed_marker_blocks_even_with_confirm_outbound(self):
        (self.root / 'work' / '.domain.toml').write_text(self.TYPO.format('work'))
        self.research(QUESTION, '--confirm-outbound', ok=False)
        self.assertFalse(self.out.exists())

    def tmpdir_case(self, tmp, via_environment):
        tmp.mkdir(exist_ok=True)
        env = {**self.fake_env, 'TMPDIR': str(tmp)}
        if via_environment:
            env.update(PISAR_ROOT=str(self.root), PISAR_STATE_DIR=str(self.state))
            self.fake()
            self.run_pisar('research', QUESTION, env=env)
        else:
            self.research(QUESTION, env=env)
        cwd = Path(self.call()['cwd']).resolve()
        for forbidden in (self.root.resolve(), self.state.resolve()):
            self.assertFalse(cwd.is_relative_to(forbidden), f'{cwd} inside {forbidden}')

    def test_a_tmpdir_inside_the_root_or_the_state_dir_is_not_trusted(self):
        for via_environment in (False, True):
            self.tmpdir_case(self.root / 'tmp', via_environment)
            self.tmpdir_case(self.state / 'tmp', via_environment)
            self.tmpdir_case(self.root, via_environment)

    def test_a_tmpdir_inside_a_git_repository_is_not_trusted(self):
        other = self.base / 'other-repo'
        (other / 'tmp').mkdir(parents=True)
        import subprocess
        subprocess.run(['git', '-C', str(other), 'init', '-q'], check=True)
        self.research(QUESTION, env={**self.fake_env, 'TMPDIR': str(other / 'tmp')})
        self.assertFalse(Path(self.call()['cwd']).resolve().is_relative_to(other.resolve()))

    def test_a_valid_tmpdir_is_used(self):
        tmp = self.base / 'scratch'
        tmp.mkdir()
        self.research(QUESTION, env={**self.fake_env, 'TMPDIR': str(tmp)})
        self.assertTrue(Path(self.call()['cwd']).resolve().is_relative_to(tmp.resolve()))

    def test_without_any_usable_temp_directory_research_refuses(self):
        run = self.in_process(FALLBACKS=())
        with mock.patch.dict(os.environ, {'TMPDIR': str(self.root)}):
            with self.assertRaisesRegex(research.WikiError, 'temporary directory'):
                run()
        self.assertFalse(self.out.exists())

    def test_oversized_output_is_rejected_and_bounded(self):
        p = self.research(QUESTION, mode='huge', ok=False)
        self.assertIn('too large', p.stderr)
        self.assertEqual(self.stored(), [])
        self.assertLessEqual(self.diagnostics('stdout').stat().st_size, research.DIAGNOSTIC_LIMIT)

    def test_oversized_stderr_is_bounded_and_not_printed(self):
        p = self.research(QUESTION, mode='noisy', ok=False)
        self.assertLess(len(p.stderr), 1000)
        self.assertLessEqual(self.diagnostics('stderr').stat().st_size, research.DIAGNOSTIC_LIMIT)

    def test_string_and_count_limits_are_enforced(self):
        good = GOOD['findings'][0]
        self.rejects({**GOOD, 'summary': 'x' * (research.SUMMARY_LIMIT + 1)}, 'too long')
        self.rejects({**GOOD, 'findings': [{**good, 'claim': 'x' * (research.CLAIM_LIMIT + 1)}]}, 'too long')
        self.rejects({**GOOD, 'findings': [{**good, 'source_url': 'https://e.org/' + 'x' * research.URL_LIMIT}]},
                     'too long')
        self.rejects({**GOOD, 'gaps': ['x' * (research.GAP_LIMIT + 1)]}, 'too long')
        self.rejects({**GOOD, 'findings': [good] * (research.FINDINGS_LIMIT + 1)}, 'too many')
        self.rejects({**GOOD, 'gaps': ['g'] * (research.GAPS_LIMIT + 1)}, 'too many')

    def test_the_whole_record_is_size_limited(self):
        big = {'claim': 'c' * research.CLAIM_LIMIT, 'quote': 'q', 'source_url': 'https://e.org/' + 'u' * 1900}
        self.rejects({**GOOD, 'findings': [big] * research.FINDINGS_LIMIT,
                      'gaps': ['g' * research.GAP_LIMIT] * research.GAPS_LIMIT}, 'record is too large')

    def test_unexpected_field_names_are_sanitised_in_errors(self):
        p = self.research(QUESTION, env={**self.fake_env, 'FAKE_CLAUDE_RESULT': json.dumps(
            {**GOOD, 'bad\nname ' + 'z' * 200: 1})}, ok=False)
        self.assertLess(len(p.stderr), 600)
        self.assertNotIn('zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz', p.stderr)

    def test_error_envelope_keeps_diagnostics_but_prints_no_response_text(self):
        p = self.research(QUESTION, mode='iserror', ok=False)
        self.assertNotIn('SECRET-RAW-RESPONSE', p.stderr)
        self.assertIn(str(self.diagnostics('stdout')), p.stderr)
        self.assertIn('SECRET-RAW-RESPONSE', self.diagnostics('stdout').read_text())
        self.assertEqual(self.stored(), [])

    def test_nonzero_exit_keeps_diagnostics_and_names_the_artifact(self):
        p = self.research(QUESTION, mode='exit', ok=False)
        self.assertIn(str(self.diagnostics('stderr')), p.stderr)

    def test_timeout_keeps_partial_diagnostics_and_kills_the_child(self):
        run = self.in_process(TIMEOUT=1)
        self.fake('sleep')
        with self.assertRaisesRegex(research.WikiError, 'did not finish within 1 seconds') as caught:
            run()
        self.assertIn('diagnostics kept', str(caught.exception))
        self.assertEqual(self.stored(), [])
        self.assertTrue(list((self.state / 'research').glob('*.stderr.txt')) or
                        list((self.state / 'research').glob('*.raw.txt')))


if __name__ == '__main__':
    unittest.main()
