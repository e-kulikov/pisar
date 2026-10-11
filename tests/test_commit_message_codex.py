"""The Codex commit-message writer: the same guarantees as the Claude writer, through `codex exec`."""
import json
import os
from pathlib import Path
import stat
import unittest
from unittest import mock

from pisar import commit_message, config, gitops, operations, settings
from pisar.operations import Runtime
from pisar.safety import WikiError
from pisar.spaces import Wiki
from .support import Fixture, git

# A fake `codex`: records its call (argv, environment names, CODEX_HOME, cwd, stdin) into the record
# file, then answers as fake.json says: the final message goes to the file named after -o, as JSON.
FAKE = '''#!/usr/bin/env python3
import json, os, subprocess, sys, time
with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "fake.json")) as stream:
    conf = json.load(stream)
call = {"argv": sys.argv[1:], "env": sorted(os.environ), "codex_home": os.environ.get("CODEX_HOME"),
        "home": os.environ.get("HOME"), "wiki_root": os.environ.get("WIKI_ROOT"), "cwd": os.getcwd(),
        "stdin": sys.stdin.read()}
with open(conf["record"], "a") as stream:
    stream.write(json.dumps(call) + "\\n")
mode = conf["mode"]
if mode == "exit":
    sys.stderr.write("SECRET-STDERR should never be echoed")
    sys.exit(3)
if mode == "sleep":
    time.sleep(60)
if mode == "huge":
    sys.stdout.write("x" * (3 * 1024 * 1024))
    sys.exit(0)
if mode == "orphan":
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
out = sys.argv[sys.argv.index("-o") + 1]
with open(out, "w") as stream:
    stream.write(json.dumps({"message": conf["text"]}))
'''

SUBJECT = 'feat(work): add the launch note'
MESSAGE = f'{SUBJECT}\n\nThe launch note was missing from the work space.'


class CodexWriter(Fixture):
    def setUp(self):
        super().setUp()
        self.bin = self.base / 'codex-bin'
        self.bin.mkdir()
        (self.bin / 'codex').write_text(FAKE)
        (self.bin / 'codex').chmod(stat.S_IRWXU)
        self.record = self.base / 'codex-calls.jsonl'
        self.fake()
        self.path = f'{self.bin}{os.pathsep}{os.environ["PATH"]}'

    def fake(self, mode='ok', text=MESSAGE):
        (self.bin / 'fake.json').write_text(json.dumps(dict(mode=mode, text=text, record=str(self.record))))

    def calls(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def options(self, **changes):
        return {**dict(enabled=True, agent='codex', model='gpt-5.5', effort='medium', timeout=30,
                       prompt_file=None), **changes}

    def compose(self, options=None, changes=None, what='Add the launch note', why='It was missing.'):
        changes = changes or dict(paths=['work/alpha/notes/launch.md'], spaces=['work/alpha'],
                                  diff='+Launch note\n', tree='0' * 40)
        with mock.patch.dict(os.environ, {'PATH': self.path}):
            return commit_message.compose(changes, 'codex-op', dict(what=what, why=why, context=None),
                                          options or self.options(), self.root, self.state)


class Invocation(CodexWriter):
    def test_writer_runs_codex_exec_with_no_tools_read_only_and_no_bypass(self):
        self.compose()
        argv = self.calls()[0]['argv']
        self.assertEqual(argv[0], 'exec')
        for flag in ('--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('-s') + 1], 'read-only')
        self.assertIn('web_search="disabled"', argv)
        self.assertEqual(argv[argv.index('-m') + 1], 'gpt-5.5')
        self.assertIn('model_reasoning_effort="medium"', argv)
        disabled = {argv[i + 1] for i, a in enumerate(argv) if a == '--disable'}
        for feature in ('shell_tool', 'multi_agent', 'apps', 'plugins', 'browser_use', 'hooks', 'view_image'):
            self.assertIn(feature, disabled)
        self.assertEqual(argv[-1], '-')  # the prompt arrives on stdin
        joined = ' '.join(argv)
        for dangerous in ('bypass', 'danger-full-access', 'workspace-write', '--full-auto', 'dangerously'):
            self.assertNotIn(dangerous, joined)

    def test_writer_environment_is_explicit_and_never_carries_wiki_root(self):
        with mock.patch.dict(os.environ, {'WIKI_ROOT': str(self.root), 'PISAR_ROOT': str(self.root),
                                          'OPENAI_API_KEY': 'synthetic'}):
            self.compose()
        call = self.calls()[0]
        self.assertIsNone(call['wiki_root'])
        self.assertNotIn('PISAR_ROOT', call['env'])
        self.assertTrue(Path(call['codex_home']).is_relative_to(self.state))
        self.assertFalse(Path(call['codex_home']).is_relative_to(self.root))
        self.assertTrue(Path(call['home']).is_relative_to(Path(call['codex_home'])))
        self.assertFalse(Path(call['cwd']).is_relative_to(self.root))

    def test_stdin_holds_the_diff_the_explanation_and_the_paths(self):
        self.compose()
        stdin = self.calls()[0]['stdin']
        for part in ('+Launch note', 'Add the launch note', 'It was missing.', 'work/alpha/notes/launch.md'):
            self.assertIn(part, stdin)

    def test_valid_answer_becomes_the_checked_message_with_the_trailer(self):
        self.assertEqual(self.compose(), f'{MESSAGE}\n\nPisar-Operation: codex-op')


class Failures(CodexWriter):
    def test_invalid_answer_is_refused_without_echoing_it(self):
        self.fake(text='Added some notes without a type')
        with self.assertRaisesRegex(WikiError, 'invalid message'):
            self.compose()

    def test_answer_that_is_not_a_message_object_is_refused(self):
        with mock.patch.dict(os.environ, {'PATH': self.path}), \
                mock.patch.object(commit_message, 'run_bounded', return_value=mock.Mock(
                    state='ok', returncode=0, stdout=b'', stderr=b'')):
            with self.assertRaisesRegex(WikiError, 'invalid answer'):
                commit_message.compose(dict(paths=[], spaces=[], diff='', tree='0' * 40), 'codex-op',
                                       dict(what='a', why='b', context=None), self.options(), self.root, self.state)

    def test_failures_fail_closed_with_fixed_messages(self):
        cases = (('exit', {}, 'exited with status 3'),
                 ('sleep', dict(timeout=1), 'did not finish'),
                 ('huge', {}, 'too large'),
                 ('orphan', dict(timeout=30), 'background processes'))
        for mode, extra, expected in cases:
            with self.subTest(mode=mode):
                self.fake(mode=mode)
                with self.assertRaises(WikiError) as caught:
                    self.compose(options=self.options(**extra))
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn('SECRET-STDERR', str(caught.exception))

    def test_missing_codex_is_refused(self):
        with mock.patch.dict(os.environ, {'PATH': str(self.base / 'empty')}), \
                self.assertRaisesRegex(WikiError, 'codex is not on PATH'):
            commit_message.compose(dict(paths=[], spaces=[], diff='', tree='0' * 40), 'codex-op',
                                   dict(what='a', why='b', context=None), self.options(), self.root, self.state)

    def test_a_failed_codex_run_leaves_no_commit_and_no_journal_progress(self):
        config_file = Path(self.env['XDG_CONFIG_HOME']) / 'pisar' / 'config.toml'
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text('[commit_message]\nenabled = true\nagent = "codex"\n')
        self.fake(mode='exit')
        plan = self.base / 'plan.json'
        source = self.external()
        import hashlib
        plan.write_text(json.dumps(dict(
            schema_version=1, operation_id='codex-failing', space_id='work/alpha',
            source=dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
            meeting=dict(id='codex-meeting', title='Launch', body='Agreed to launch.'), tasks=[], pages=[])))
        before = git(self.root, 'rev-parse', 'HEAD')
        env = {**self.env, 'PATH': self.path}
        self.env = env
        failed = self.cli('--ruwana', 'ruwana', 'save', '--plan', plan, '--what', 'a', '--why', 'b', ok=False)
        self.assertNotIn('SECRET-STDERR', failed.stderr)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), before)
        journal = Runtime(self.state, self.root).load('codex-failing')
        self.assertEqual(journal['commits'], [])
        self.assertIsNone(journal['pending'])


class Retries(CodexWriter):
    def test_a_retry_reuses_the_message_stored_for_the_same_tree(self):
        import hashlib
        source = self.external()
        plan = self.base / 'retry.json'
        plan.write_text(json.dumps(dict(
            schema_version=1, operation_id='codex-retry', space_id='work/alpha',
            source=dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
            meeting=dict(id='codex-retry-meeting', title='Launch', body='Agreed to launch.'), tasks=[], pages=[])))
        real_run = gitops.run
        armed = [True]

        def run(repo, *args, **kwargs):
            if args and args[0] == 'update-ref' and armed[0]:
                armed[0] = False
                raise WikiError('interrupted between commit-tree and update-ref')
            return real_run(repo, *args, **kwargs)
        self.fake(text='feat(work): first note\n\nWritten first.')
        with mock.patch.dict(os.environ, {'PATH': self.path, 'HOME': self.env['HOME']}), \
                mock.patch.object(settings, 'commit_message', return_value=self.options()), \
                mock.patch.object(gitops, 'run', run), \
                commit_message.request('Record the launch call', 'It was agreed.'):
            with self.assertRaises(WikiError):
                operations.save(Wiki(self.root), self.state, plan)
            self.fake(text='feat(work): second note\n\nWritten second.')  # a retry must not use this one
            operations.save(Wiki(self.root), self.state, plan)
        subjects = git(self.root, 'log', '--format=%s').splitlines()
        # The first commit kept the message stored before the interruption; the second commit is new and got its own.
        self.assertEqual(subjects[:2], ['feat(work): second note', 'feat(work): first note'])
        self.assertEqual(len(self.calls()), 2)  # one run for the first commit, one for the second; none for the retry


class Configuration(Fixture):
    def config_file(self, text):
        file = self.base / 'writer.toml'
        file.write_text(text)
        return file

    def test_codex_is_an_accepted_writer_and_others_are_refused_naming_the_supported_ones(self):
        self.assertTrue(config.load(self.config_file('[commit_message]\nagent = "codex"\n'))['commit_message'])
        with self.assertRaisesRegex(WikiError, 'supported: claude, codex'):
            config.load(self.config_file('[commit_message]\nagent = "gemini"\n'))

    def test_codex_writer_defaults_are_codex_models_never_the_claude_names(self):
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.base / 'none')}):
            options = settings.commit_message()
            self.assertEqual(options['model'], 'sonnet')  # claude's default is unchanged
            self.assertEqual(options['effort'], 'medium')
        self.write_codex()
        options = settings.commit_message()
        self.assertEqual(options['agent'], 'codex')
        self.assertEqual(options['model'], settings.ROLE_DEFAULTS['codex']['researcher']['model'])
        self.assertEqual(options['effort'], settings.ROLE_DEFAULTS['codex']['researcher']['effort'])
        self.assertNotIn(options['model'], {'sonnet', 'opus', 'haiku'})

    def test_explicit_codex_model_and_effort_are_kept(self):
        self.write_codex(extra='model = "gpt-6.1-sol"\neffort = "high"\n')
        options = settings.commit_message()
        self.assertEqual((options['model'], options['effort']), ('gpt-6.1-sol', 'high'))

    def write_codex(self, extra=''):
        file = Path(self.env['XDG_CONFIG_HOME']) / 'pisar' / 'config.toml'
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(f'[commit_message]\nenabled = true\nagent = "codex"\n{extra}')
        self.patch = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(Path(self.env['XDG_CONFIG_HOME']))})
        self.patch.start()
        self.addCleanup(self.patch.stop)


if __name__ == '__main__':
    unittest.main()
