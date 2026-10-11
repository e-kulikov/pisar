"""Commit messages: explained by --what/--why, optionally written by a tool-less writer agent."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from argparse import Namespace
from pisar import commit_message, config, gitops, operations, settings
from pisar.operations import Runtime
from pisar.safety import WikiError
from pisar.spaces import Wiki
from .support import Fixture, git
from .test_spacecmd import SpaceCommandFixture

# A fake `claude`: records its call (argv, environment names, working directory, stdin) into the
# file named by fake.json, then behaves as FAKE_MODE says. The writer output is in fake.json too.
FAKE = '''#!/usr/bin/env python3
import json, os, subprocess, sys, time
with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "fake.json")) as stream:
    conf = json.load(stream)
call = {"argv": sys.argv[1:], "env": sorted(os.environ), "cwd": os.getcwd(),
        "wiki_root": os.environ.get("WIKI_ROOT"), "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
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
    print(conf["text"])
    sys.exit(0)
print(conf["text"])
'''

SUBJECT = 'feat(work): add the launch note'
MESSAGE = f'{SUBJECT}\n\nThe launch note was missing from the work space.'


class Writer(Fixture):
    """A fake writer on PATH; the record file lists every call the writer received."""

    def setUp(self):
        super().setUp()
        self.bin = self.base / 'writer-bin'
        self.bin.mkdir()
        (self.bin / 'claude').write_text(FAKE)
        (self.bin / 'claude').chmod(stat.S_IRWXU)
        self.record = self.base / 'writer-calls.jsonl'
        self.fake(mode='ok', text=MESSAGE)
        self.path = f'{self.bin}{os.pathsep}{os.environ["PATH"]}'

    def fake(self, mode='ok', text=MESSAGE):
        (self.bin / 'fake.json').write_text(json.dumps(dict(mode=mode, text=text, record=str(self.record))))

    def calls(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def options(self, **changes):
        return {**dict(enabled=True, agent='claude', model='sonnet', effort='medium', timeout=30,
                       prompt_file=None), **changes}

    def compose(self, operation='writer-op', what='Add the launch note', why='It was missing.',
                options=None, changes=None):
        changes = changes or dict(paths=['work/alpha/notes/launch.md'], spaces=['work/alpha'],
                                  diff='+Launch note\n', tree='0' * 40)
        explanation = dict(what=what, why=why, context=None)
        with mock.patch.dict(os.environ, {'PATH': self.path}):
            return commit_message.compose(changes, operation, explanation, options or self.options(),
                                          self.root, self.state)


class Deterministic(Writer):
    def test_disabled_message_is_the_subject_alone_and_runs_no_executable(self):
        message = self.compose(options=self.options(enabled=False), what=None, why=None)
        self.assertEqual(message, 'wiki: writer-op')
        self.assertEqual(self.calls(), [])

    def test_disabled_message_carries_what_and_why_verbatim(self):
        message = self.compose(options=self.options(enabled=False))
        self.assertEqual(message, 'wiki: writer-op\n\nAdd the launch note\n\nIt was missing.')
        self.assertEqual(self.calls(), [])

    def test_enabled_writer_without_what_or_why_is_refused(self):
        for what, why in ((None, 'why'), ('what', None), ('  ', 'why')):
            with self.subTest(what=what, why=why), self.assertRaisesRegex(WikiError, '--what'):
                self.compose(what=what, why=why)
        self.assertEqual(self.calls(), [])

    def test_require_refuses_when_enabled_and_missing(self):
        with mock.patch.object(settings, 'commit_message', return_value=self.options()):
            with self.assertRaisesRegex(WikiError, '--what'):
                commit_message.require(None, 'why')
            commit_message.require('what', 'why')


class Writing(Writer):
    def test_enabled_writer_message_is_checked_and_gets_the_operation_trailer(self):
        message = self.compose()
        self.assertEqual(message, f'{MESSAGE}\n\nPisar-Operation: writer-op')

    def test_writer_gets_no_tools_and_isolated_configuration_and_its_input_is_the_change(self):
        with mock.patch.dict(os.environ, {'WIKI_ROOT': str(self.root), 'PISAR_ROOT': str(self.root)}):
            self.compose()
        call, = self.calls()
        argv = call['argv']
        self.assertIn('-p', argv)
        self.assertIn('--tools=', argv)
        for flag in ('--safe-mode', '--strict-mcp-config', '--no-session-persistence'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--model') + 1], 'sonnet')
        self.assertEqual(argv[argv.index('--effort') + 1], 'medium')
        self.assertNotIn('WIKI_ROOT', call['env'])
        self.assertNotIn('PISAR_ROOT', call['env'])
        self.assertIsNone(call['wiki_root'])
        self.assertNotIn(str(self.root), call['cwd'])
        self.assertFalse(Path(call['cwd']).is_relative_to(self.root))
        self.assertIn('+Launch note', call['stdin'])
        self.assertIn('Add the launch note', call['stdin'])
        self.assertIn('It was missing.', call['stdin'])
        self.assertIn('work/alpha/notes/launch.md', call['stdin'])

    def test_writer_configuration_directory_is_under_the_state_directory(self):
        self.compose()
        call, = self.calls()
        config_dir = Path(call['config_dir'])
        self.assertTrue(config_dir.is_relative_to(self.state))
        self.assertFalse(config_dir.is_relative_to(self.root))

    def test_invalid_writer_output_is_refused_without_echoing_it(self):
        bad = {
            'not a conventional subject': 'Added some notes\n\nBody.',
            'subject too long': 'feat(work): ' + 'x' * 80,
            'body too long': f'{SUBJECT}\n\n' + 'y' * 2500,
            'absolute local path': f'{SUBJECT}\n\nSaved to /home/alice/private/notes.md.',
            'secret-like value': f'{SUBJECT}\n\napi_key = sk-live-0123456789abcdef0123',
            'trailer injected by the model': f'{SUBJECT}\n\nPisar-Operation: someone-else',
            'empty': '   \n',
            'second line not blank': f'{SUBJECT}\nBody without a blank line.',
        }
        for name, text in bad.items():
            with self.subTest(name=name):
                self.fake(text=text)
                with self.assertRaises(WikiError) as caught:
                    self.compose()
                self.assertNotIn('alice', str(caught.exception))
                self.assertNotIn('sk-live', str(caught.exception))

    def test_a_fenced_answer_is_accepted_after_unwrapping(self):
        self.fake(text=f'```\n{MESSAGE}\n```')
        self.assertEqual(self.compose(), f'{MESSAGE}\n\nPisar-Operation: writer-op')

    def test_writer_failures_fail_closed_with_fixed_messages(self):
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

    def test_missing_executable_is_refused(self):
        with mock.patch.dict(os.environ, {'PATH': str(self.base / 'empty')}), \
                self.assertRaisesRegex(WikiError, 'claude is not on PATH'):
            commit_message.compose(dict(paths=[], spaces=[], diff='', tree='0' * 40),
                                   'writer-op', dict(what='a', why='b', context=None), self.options(),
                                   self.root, self.state)


class Validation(unittest.TestCase):
    def config_file(self, text):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        file = directory / 'config.toml'
        file.write_text(text)
        return file

    def test_config_table_rejects_bad_values(self):
        cases = {
            'enabled = "yes"': 'commit_message.enabled',
            'timeout = 1': 'commit_message.timeout',
            'timeout = 99999': 'commit_message.timeout',
            'agent = "gemini"': 'commit_message.agent',
            'prompt_file = "relative.md"': 'commit_message.prompt_file',
            'colour = "red"': 'commit_message.colour',
        }
        for line, key in cases.items():
            with self.subTest(line=line):
                file = self.config_file(f'[commit_message]\n{line}\n')
                with self.assertRaisesRegex(WikiError, key):
                    config.load(file)

    def test_writer_agent_is_validated_against_the_writer_set_not_the_agents_list(self):
        # Another agent added to AGENTS (for its own options) must not become a valid writer.
        with mock.patch.object(config, 'AGENTS', ('claude', 'codex', 'gemini')):
            file = self.config_file('[commit_message]\nagent = "gemini"\n')
            with self.assertRaisesRegex(WikiError, 'supported: claude, codex'):
                config.load(file)

    def test_every_writer_agent_has_a_backend_and_every_backend_is_a_writer_agent(self):
        self.assertEqual(set(config.COMMIT_MESSAGE_AGENTS), set(commit_message.BACKENDS))

    def test_valid_commit_message_table_loads(self):
        file = self.config_file('[commit_message]\nenabled = true\nagent = "claude"\nmodel = "sonnet"\n'
                        'effort = "low"\ntimeout = 60\nprompt_file = "/abs/prompt.md"\n')
        self.assertTrue(config.load(file)['commit_message']['enabled'])

    def test_show_reports_the_writer_settings_with_sources(self):
        shown = settings.show({})['settings']
        self.assertEqual(shown['commit_message.enabled'], dict(value=False, source='default'))
        self.assertEqual(shown['commit_message.timeout'], dict(value=120, source='default'))


class Commits(Writer):
    """The commands that commit, through the real CLI and with the writer enabled in the config file."""

    def setUp(self):
        super().setUp()
        self.config_file = Path(self.env['XDG_CONFIG_HOME']) / 'pisar' / 'config.toml'
        self.env = {**self.env, 'PATH': self.path}

    def enable(self, text='[commit_message]\nenabled = true\n'):
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(text)

    def plan(self, operation):
        source = self.external()
        path = self.base / f'{operation}.json'
        path.write_text(json.dumps(dict(
            schema_version=1, operation_id=operation, space_id='work/alpha',
            source=dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
            meeting=dict(id=f'{operation}-meeting', title='Launch', body='Agreed to launch.'),
            tasks=[], pages=[])))
        return path

    def test_enabled_save_without_what_is_refused_before_any_write(self):
        self.enable()
        plan = self.plan('no-what')
        before = git(self.root, 'rev-parse', 'HEAD')
        failed = self.cli('--ruwana', 'ruwana', 'save', '--plan', plan, ok=False)
        self.assertIn('--what', failed.stderr)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), before)
        self.assertEqual(self.calls(), [])
        self.assertIsNone(Runtime(self.state, self.root).load('no-what'))

    def test_enabled_save_commits_the_writer_message_with_the_trailer(self):
        self.enable()
        plan = self.plan('written')
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan, '--what', 'Record the launch call',
                 '--why', 'The team agreed to launch.')
        subjects = git(self.root, 'log', '--format=%s').splitlines()
        self.assertEqual(subjects[0], SUBJECT)
        self.assertIn('Pisar-Operation: written', git(self.root, 'log', '-1', '--format=%B'))
        self.assertTrue(self.calls())
        self.assertTrue(self.data('check')['ok'])

    def test_writer_failure_leaves_no_commit_and_no_journal_progress(self):
        self.enable()
        self.fake(mode='exit')
        plan = self.plan('failing')
        before = git(self.root, 'rev-parse', 'HEAD')
        failed = self.cli('--ruwana', 'ruwana', 'save', '--plan', plan, '--what', 'a', '--why', 'b', ok=False)
        self.assertNotIn('SECRET-STDERR', failed.stderr)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), before)
        journal = Runtime(self.state, self.root).load('failing')
        self.assertEqual(journal['commits'], [])
        self.assertIsNone(journal['pending'])
        self.assertNotIn('messages', journal)

    def test_retry_reuses_the_message_stored_for_the_same_commit(self):
        self.enable()
        counter = self.base / 'counter'
        self.fake(mode='ok', text=MESSAGE)
        plan = self.plan('stored')
        explanation = dict(what='Record the launch call', why='It was agreed.', context=None)
        calls = iter([f'feat(work): note {n}\n\nWritten call {n}.' for n in (1, 2, 3)])
        generated = []

        def compose(changes, operation, explained, options, root, base):
            text = next(calls)
            generated.append(text.splitlines()[0])
            return f'{text}\n\nPisar-Operation: {operation}'
        real_run = gitops.run
        armed = [True]

        def run(repo, *args, **kwargs):
            if args and args[0] == 'update-ref' and armed[0]:
                armed[0] = False
                raise WikiError('interrupted between commit-tree and update-ref')
            return real_run(repo, *args, **kwargs)
        with mock.patch.dict(os.environ, {'PATH': self.path, 'HOME': self.env['HOME']}), \
                mock.patch.object(commit_message, 'compose', compose), \
                mock.patch.object(settings, 'commit_message', return_value=self.options()), \
                mock.patch.object(gitops, 'run', run), commit_message.request(**explanation):
            with self.assertRaises(WikiError):
                operations.save(Wiki(self.root), self.state, plan)
            operations.save(Wiki(self.root), self.state, plan)
        self.assertEqual(generated, ['feat(work): note 1', 'feat(work): note 2'])
        subjects = git(self.root, 'log', '--format=%s').splitlines()
        self.assertEqual(subjects[:2], ['feat(work): note 2', 'feat(work): note 1'])
        self.assertEqual(len(Runtime(self.state, self.root).load('stored')['commits']), 2)

    def test_disabled_by_default_no_writer_runs_and_the_subject_is_kept(self):
        plan = self.plan('plain')
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan)
        self.assertEqual(self.calls(), [])
        self.assertEqual(git(self.root, 'log', '-1', '--format=%s'), 'wiki: plain')

    def test_disabled_save_adds_the_explanation_verbatim_and_keeps_the_subject(self):
        plan = self.plan('explained')
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan, '--what', 'Record the launch call',
                 '--why', 'The team agreed to launch.')
        self.assertEqual(self.calls(), [])
        self.assertEqual(git(self.root, 'log', '-1', '--format=%B').split('\n')[0], 'wiki: explained')
        self.assertIn('Record the launch call\n\nThe team agreed to launch.', git(self.root, 'log', '-1', '--format=%B'))

    def test_whitespace_only_explanation_is_refused_before_any_write(self):
        plan = self.plan('blank')
        before = git(self.root, 'rev-parse', 'HEAD')
        failed = self.cli('--ruwana', 'ruwana', 'save', '--plan', plan, '--what', '   ', '--why', 'b', ok=False)
        self.assertIn('--what must not be empty', failed.stderr)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), before)

    def test_context_and_context_file_are_mutually_exclusive(self):
        plan = self.plan('both')
        summary = self.base / 'summary.txt'
        summary.write_text('Drafted first.')
        failed = self.cli('--ruwana', 'ruwana', 'save', '--plan', plan, '--what', 'a', '--why', 'b',
                          '--context', 'x', '--context-file', str(summary), ok=False)
        self.assertIn('not allowed with', failed.stderr)

    def test_space_create_is_refused_without_what_when_enabled(self):
        self.enable()
        failed = self.cli('space', 'create', '--domain', 'work', '--kind', 'project', '--title', 'Launch Plan',
                          ok=False)
        self.assertIn('--what', failed.stderr)
        self.assertFalse((self.root / 'work/10-projects/launch-plan').exists())


if __name__ == '__main__':
    unittest.main()


class CommittingCommands(unittest.TestCase):
    def leaves(self):
        """(command, action) of every parser that takes --what, read from the real command line."""
        from argparse import _SubParsersAction
        from pisar import cli
        found = set()
        top = cli.parser()
        for action in top._actions:
            if not isinstance(action, _SubParsersAction):
                continue
            for command, parser in action.choices.items():
                if any('--what' in a.option_strings for a in parser._actions):
                    found.add((command, None))
                for sub in parser._actions:
                    if isinstance(sub, _SubParsersAction):
                        for name, leaf in sub.choices.items():
                            if any('--what' in a.option_strings for a in leaf._actions):
                                found.add((command, name))
        return found

    def test_the_explanation_flags_are_exactly_on_the_committing_commands(self):
        self.assertEqual(self.leaves(), set(commit_message.COMMITTING))

    def test_every_committing_command_refuses_a_missing_explanation_when_enabled(self):
        for command, action in commit_message.COMMITTING:
            with self.subTest(command=command, action=action), \
                    mock.patch.object(settings, 'commit_message', return_value=dict(
                        config.COMMIT_MESSAGE_DEFAULTS, enabled=True)):
                args = Namespace(command=command, action=action, what=None, why=None, context=None,
                                 context_file=None)
                with self.assertRaisesRegex(WikiError, '--what'):
                    commit_message.explain(args)


class Explanations(Writer):
    def args(self, **values):
        base = dict(command='save', action=None, what='Record the call', why='It was agreed.', context=None,
                    context_file=None)
        return Namespace(**{**base, **values})

    def enabled(self):
        return mock.patch.object(settings, 'commit_message', return_value=self.options())

    def test_whitespace_only_flags_are_refused(self):
        with self.assertRaisesRegex(WikiError, '--what must not be empty'):
            commit_message.explain(self.args(what='   '))

    def test_context_text_reaches_the_writer_input(self):
        self.compose_with = None
        with mock.patch.dict(os.environ, {'PATH': self.path}):
            commit_message.compose(dict(paths=['a.md'], spaces=[], diff='', tree='0' * 40), 'writer-op',
                                   dict(what='a', why='b', context='Drafted the note first.'),
                                   self.options(), self.root, self.state)
        self.assertIn('Drafted the note first.', self.calls()[-1]['stdin'])

    def test_context_is_bounded_utf8_and_a_regular_file(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        big = directory / 'big.txt'
        big.write_text('x' * 9000)
        binary = directory / 'binary.txt'
        binary.write_bytes(b'\xff\xfe\x00bad')
        link = directory / 'link.txt'
        link.symlink_to(big)
        with self.enabled():
            for path, reason in ((big, 'too long'), (binary, 'not UTF-8'), (link, 'not a readable file'),
                                 (directory / 'missing.txt', 'not a readable file')):
                with self.subTest(path=path.name), self.assertRaisesRegex(WikiError, reason):
                    commit_message.explain(self.args(context=None, context_file=str(path)))

    def test_a_context_file_under_a_symlinked_directory_is_refused(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        real = directory / 'real'
        real.mkdir()
        (real / 'context.txt').write_text('Drafted the note first.')
        (directory / 'link').symlink_to(real, target_is_directory=True)
        with self.enabled(), self.assertRaisesRegex(WikiError, 'not a readable file'):
            commit_message.explain(self.args(context=None, context_file=str(directory / 'link' / 'context.txt')))

    def test_context_is_ignored_and_unchecked_while_the_writer_is_disabled(self):
        with mock.patch.object(settings, 'commit_message', return_value=self.options(enabled=False)):
            token = commit_message.explain(self.args(context=None, context_file='/no/such/file'))
            commit_message.reset(token)
        self.assertEqual(commit_message.current()['context'], None)

    def test_the_absolute_path_forms_are_refused_and_relative_ones_accepted(self):
        for text in ('see /etc', 'in /root', 'file:///home/x/y', 'path:/home/x/y', 'at /home/user',
                     'C:\\Users\\x', 'moved ~/notes.md'):
            with self.subTest(text=text), self.assertRaisesRegex(WikiError, 'absolute local path'):
                commit_message.plain(text, '--why', 200)
        for text in ('work/alpha/notes/launch.md', 'see https://example.com/docs', 'Q1/2026 review',
                     'and/or the plan'):
            with self.subTest(text=text):
                self.assertEqual(commit_message.plain(text, '--why', 200), text)


class StoredMessages(Writer):
    def test_a_different_tree_gets_a_new_message_and_the_stored_one_is_not_reused(self):
        runtime = Runtime(self.state, self.root)
        journal = dict(root=str(self.root), operation_id='stored-tree', commits=[], heads={}, artifacts=[], status='incomplete',
                       pending=None)
        runtime.store(journal)
        texts = iter(['feat(work): first\n\nOne.', 'feat(work): second\n\nTwo.'])
        calls = []

        def compose(changes, operation, explained, options, root, base):
            calls.append(changes['tree'])
            return f'{next(texts)}\n\nPisar-Operation: {operation}'
        with mock.patch.object(settings, 'commit_message', return_value=self.options()), \
                mock.patch.object(gitops, 'diff_text', return_value='+x\n'), \
                mock.patch.object(commit_message, 'compose', compose):
            write = operations.commit_writer(Wiki(self.root), runtime, journal, self.root)
            first = write(dict(paths=['work/alpha/a.md'], old='1' * 40, tree='a' * 40))
            again = write(dict(paths=['work/alpha/a.md'], old='1' * 40, tree='a' * 40))
            changed = write(dict(paths=['work/alpha/a.md'], old='1' * 40, tree='b' * 40))
        self.assertEqual(first, again)
        self.assertEqual(calls, ['a' * 40, 'b' * 40])
        self.assertIn('second', changed)
        self.assertEqual(runtime.load('stored-tree')['messages'][str(self.root)]['tree'], 'b' * 40)


class MultiRepoWriter(Writer, SpaceCommandFixture):
    """The writer fails for the parent after the child was committed and checkpointed."""

    def test_a_writer_failure_for_the_parent_keeps_the_child_commit_and_the_rerun_adds_no_duplicate(self):
        from pisar import spacecmd
        bare = self.origin()
        config_file = Path(self.env['XDG_CONFIG_HOME']) / 'pisar' / 'config.toml'
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text('[commit_message]\nenabled = true\n')
        self.env = {**self.env, 'PATH': self.path}  # the fake writer, not a real claude
        real = commit_message.compose
        seen = []

        def compose(changes, operation, explained, options, root, base):
            seen.append(changes['paths'])
            if len(seen) == 2:
                raise WikiError('the commit message writer exited with status 3; nothing was committed')
            return real(changes, operation, explained, options, root, base)
        head = git(self.root, 'rev-parse', 'HEAD')
        with mock.patch.dict(os.environ, {'PATH': self.path, 'HOME': self.env['HOME']}), \
                mock.patch.object(commit_message, 'compose', compose), \
                commit_message.request('Add the acme domain', 'The team works with Acme.'):
            with self.assertRaises(WikiError):
                spacecmd.domain_add(Wiki(self.root), self.state, 'acme', 'Acme', str(bare), 'para')
        child = self.root / 'acme'
        child_commits = git(child, 'rev-list', '--count', 'HEAD')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(int(child_commits), 2)  # the seed commit and the one the first attempt made
        self.cli('--ruwana', 'ruwana', 'domain', 'add', '--id', 'acme', '--title', 'Acme',
                 '--repo', str(bare), '--what', 'Add the acme domain', '--why', 'The team works with Acme.')
        self.assertEqual(git(child, 'rev-list', '--count', 'HEAD'), child_commits)  # no duplicate child commit
        self.assertNotEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertTrue(self.data('check')['ok'])
