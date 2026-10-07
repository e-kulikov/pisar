import json
import os
from pathlib import Path
import stat
import sys
import unittest

from .support import CHECKOUT, Fixture, clean_environ

FAKE = '''#!/usr/bin/env python3
import json, os, sys
out = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}
with open(os.environ["FAKE_CLAUDE_OUT"], "w") as stream:
    json.dump(out, stream)
'''


def values(argv, flag):
    """Arguments following FLAG up to the next option."""
    start = argv.index(flag) + 1
    stop = next((i for i in range(start, len(argv)) if argv[i].startswith('--')), len(argv))
    return argv[start:stop]


class AgentTests(Fixture):
    def setUp(self):
        super().setUp()
        self.bin = self.base / 'fakebin'
        self.bin.mkdir()
        fake = self.bin / 'claude'
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.out = self.base / 'claude-call.json'
        self.agent_env = {**self.env, 'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}',
                          'FAKE_CLAUDE_OUT': str(self.out), 'WIKI_ROOT': '/must/not/leak',
                          'CLAUDE_CONFIG_DIR': str(self.base / 'foreign-config')}

    def launch(self, *args, env=None, ok=True, state=None):
        p = self.run_pisar('--root', self.root, '--state-dir', state or self.state, '--agent', 'claude',
                           *args, env=env or self.agent_env, ok=ok)
        return p, (json.loads(self.out.read_text()) if self.out.exists() else None)

    def test_starts_claude_in_the_root_with_an_isolated_config_in_the_state_dir(self):
        _, call = self.launch()
        self.assertEqual(Path(call['cwd']).resolve(), self.root.resolve())
        config = self.state.resolve() / 'agents' / 'claude'
        self.assertEqual(call['env']['CLAUDE_CONFIG_DIR'], str(config))
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o700)

    def test_child_uses_the_same_settings_and_never_inherits_wiki_root(self):
        _, call = self.launch()
        env = call['env']
        self.assertEqual(env['PISAR_ROOT'], str(self.root.resolve()))
        self.assertEqual(env['PISAR_STATE_DIR'], str(self.state.resolve()))
        self.assertNotIn('WIKI_ROOT', env)

    def test_system_prompt_is_the_packaged_text_and_not_a_file(self):
        _, call = self.launch()
        argv = call['argv']
        prompt = (CHECKOUT / 'pisar/agent-prompt.md').read_text(encoding='utf-8')
        self.assertEqual(argv[argv.index('--system-prompt') + 1], prompt)
        self.assertNotIn('--system-prompt-file', argv)

    def test_tools_are_minimal_and_mcp_is_disabled(self):
        _, call = self.launch()
        argv = call['argv']
        self.assertIn('--tools=Read,Write,Edit,Glob,Grep,Bash', argv)
        self.assertIn('--strict-mcp-config', argv)

    def test_mcp_config_is_passed_only_when_the_root_has_one(self):
        _, call = self.launch()
        self.assertNotIn('--mcp-config', call['argv'])
        self.assertIn('--strict-mcp-config', call['argv'])
        config = self.root / '.mcp.json'
        config.write_text('{"mcpServers": {}}')
        _, call = self.launch()
        argv = call['argv']
        self.assertEqual(values(argv, '--mcp-config'), [str(config.resolve())])
        self.assertLess(argv.index('--mcp-config'), argv.index('--strict-mcp-config'))

    def test_mcp_config_must_be_a_regular_file(self):
        target = self.base / 'elsewhere.json'
        target.write_text('{}')
        (self.root / '.mcp.json').symlink_to(target)
        _, call = self.launch()
        self.assertNotIn('--mcp-config', call['argv'])

    def test_bash_allowlist_has_no_mv_no_bare_git_no_bare_pisar(self):
        _, call = self.launch()
        allowed = values(call['argv'], '--allowedTools')
        for rule in ('Bash(git status*)', 'Bash(git diff*)', 'Bash(git log*)', 'Bash(git add *)',
                     'Bash(git commit *)', 'Bash(git mv *)', 'Bash(pisar capture *)',
                     'Bash(pisar spaces*)', 'Bash(pisar save *)', 'Bash(pisar triage *)'):
            self.assertIn(rule, allowed)
        for rule in allowed:
            self.assertTrue(rule.startswith(('Bash(git ', 'Bash(pisar ', 'Edit(//')), rule)
            self.assertNotIn(rule, ('Bash(git *)', 'Bash(pisar *)', 'Bash(mv *)', 'Bash(*)', 'Bash'))
            self.assertFalse(rule.startswith('Bash(mv'), rule)
        self.assertFalse([r for r in allowed if 'pisar --' in r], 'global flags would allow --ruwana')

    def test_domain_space_guard_config_lesson_and_research_commands_are_allowed(self):
        _, call = self.launch()
        allowed = values(call['argv'], '--allowedTools')
        for rule in ('Bash(pisar domain *)', 'Bash(pisar space *)', 'Bash(pisar guard *)',
                     'Bash(pisar config *)', 'Bash(pisar research *)',
                     'Bash(pisar lesson start *)', 'Bash(pisar lesson check *)',
                     'Bash(pisar lesson review *)', 'Bash(pisar lesson show *)',
                     'Bash(pisar lesson discard *)'):
            self.assertIn(rule, allowed)
        self.assertNotIn('Bash(pisar lesson *)', allowed)
        self.assertFalse([r for r in allowed if 'lesson accept' in r])

    def test_lesson_accept_asks_through_settings_and_is_not_allowed(self):
        _, call = self.launch()
        argv = call['argv']
        settings = json.loads(argv[argv.index('--settings') + 1])
        self.assertEqual(settings, {'permissions': {'ask': ['Bash(pisar lesson accept *)']}})

    def test_only_the_lesson_workspace_is_writable_by_absolute_path(self):
        _, call = self.launch()
        edits = [r for r in values(call['argv'], '--allowedTools') if r.startswith('Edit(')]
        self.assertEqual(edits, [f'Edit(/{self.state.resolve()}/lessons/**)'])
        self.assertTrue(edits[0].startswith('Edit(//'))
        self.assertNotIn('--add-dir', call['argv'])

    def test_bundled_plugin_is_extracted_and_passed_with_plugin_dir(self):
        from pisar import __version__
        _, call = self.launch()
        directory = self.state.resolve() / 'agents' / 'claude' / 'plugin' / __version__
        self.assertEqual(values(call['argv'], '--plugin-dir'), [str(directory)])
        self.assertTrue((directory / '.claude-plugin' / 'plugin.json').is_file())
        self.assertTrue((directory / 'skills' / 'lessons' / 'SKILL.md').is_file())

    def test_reviewer_agent_takes_model_and_effort_from_the_config(self):
        from pisar import __version__
        directory = self.state.resolve() / 'agents' / 'claude' / 'plugin' / __version__
        reviewer = directory / 'agents' / 'lesson-reviewer.md'
        self.launch()
        self.assertIn('model: opus\neffort: high\n', reviewer.read_text())
        self.write_config('[agents.claude.reviewer]\nmodel = "m1"\neffort = "low"\n')
        self.launch()
        self.assertIn('model: m1\neffort: low\n', reviewer.read_text())

    def test_relaunch_reuses_the_extracted_plugin_without_rewriting_it(self):
        from pisar import __version__
        self.launch()
        manifest = self.state.resolve() / 'agents/claude/plugin' / __version__ / '.claude-plugin/plugin.json'
        before = manifest.stat().st_mtime_ns
        self.launch()
        self.assertEqual(manifest.stat().st_mtime_ns, before)

    def test_plugin_dir_comes_before_the_system_prompt_and_after_caller_arguments(self):
        _, call = self.launch('--', '-p', 'hi')
        argv = call['argv']
        self.assertEqual(argv[:2], ['-p', 'hi'])
        self.assertLess(argv.index('--plugin-dir'), argv.index('--system-prompt'))

    def test_push_and_git_internals_are_denied(self):
        _, call = self.launch()
        denied = values(call['argv'], '--disallowedTools')
        self.assertIn('Bash(git push*)', denied)
        self.assertIn('Edit(.git/**)', denied)

    def test_arguments_after_double_dash_go_first_so_variadic_flags_keep_them(self):
        _, call = self.launch('--', '-c', 'continue the review')
        argv = call['argv']
        self.assertEqual(argv[:2], ['-c', 'continue the review'])
        self.assertLess(argv.index('--system-prompt'), argv.index('--tools=Read,Write,Edit,Glob,Grep,Bash'))

    def test_config_dir_is_reused_and_state_overrides_come_from_the_flag(self):
        self.launch()
        marker = self.state / 'agents' / 'claude' / 'keep.txt'
        marker.write_text('login')
        self.launch()
        self.assertEqual(marker.read_text(), 'login')

    def test_missing_claude_is_a_clear_error(self):
        only_python = self.base / 'only-python'
        only_python.mkdir()
        (only_python / 'python3').symlink_to(sys.executable)
        env = {**self.agent_env, 'PATH': str(only_python)}
        p = self.run_pisar('--root', self.root, '--state-dir', self.state, '--agent', 'claude',
                           env=env, ok=False)
        self.assertIn('pisar:', p.stderr)
        self.assertIn('claude', p.stderr)
        self.assertFalse(self.out.exists())

    def test_missing_root_is_refused_before_starting_the_agent(self):
        p = self.run_pisar('--root', self.base / 'nothing', '--state-dir', self.state, '--agent', 'claude',
                           env=self.agent_env, ok=False)
        self.assertIn('pisar:', p.stderr)
        self.assertFalse(self.out.exists())

    def test_state_dir_inside_a_git_repository_is_refused(self):
        p = self.run_pisar('--root', self.root, '--state-dir', self.root / 'state', '--agent', 'claude',
                           env=self.agent_env, ok=False)
        self.assertIn('pisar:', p.stderr)
        self.assertFalse(self.out.exists())
        self.assertFalse((self.root / 'state').exists())

    def test_agent_cannot_be_combined_with_a_command_or_unknown_agent(self):
        p = self.run_pisar('--root', self.root, '--agent', 'claude', 'spaces', env=self.agent_env, ok=False)
        self.assertEqual(p.returncode, 2)
        p = self.run_pisar('--root', self.root, '--agent', 'nope', env=self.agent_env, ok=False)
        self.assertEqual(p.returncode, 2)
        self.assertFalse(self.out.exists())

    # Settings from the config file

    def write_config(self, text):
        path = self.base / 'config/pisar/config.toml'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def bare(self, *args, ok=True):
        p = self.run_pisar('--root', self.root, '--state-dir', self.state, '--agent', *args,
                           env=self.agent_env, ok=ok)
        return p, (json.loads(self.out.read_text()) if self.out.exists() else None)

    def test_agent_without_a_value_uses_the_configured_default(self):
        self.write_config('default_agent = "claude"\n')
        _, call = self.bare()
        self.assertIn('--system-prompt', call['argv'])
        self.assertEqual(Path(call['cwd']).resolve(), self.root.resolve())

    def test_agent_without_a_value_keeps_arguments_after_double_dash(self):
        self.write_config('default_agent = "claude"\n')
        _, call = self.bare('--', '-c', 'continue the review')
        self.assertEqual(call['argv'][:2], ['-c', 'continue the review'])

    def test_agent_without_a_value_or_a_default_is_an_error(self):
        p, call = self.bare(ok=False)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertTrue(p.stderr.startswith('pisar: '), p.stderr)
        self.assertIn('default_agent', p.stderr)
        self.assertIsNone(call)

    def test_model_and_effort_are_passed_only_when_configured(self):
        _, call = self.launch()
        self.assertNotIn('--model', call['argv'])
        self.assertNotIn('--effort', call['argv'])
        self.write_config('[agents.claude]\neffort = "low"\n')
        _, call = self.launch()
        self.assertNotIn('--model', call['argv'])
        self.assertEqual(values(call['argv'], '--effort'), ['low'])
        self.write_config('[agents.claude]\nmodel = "any-model[1m]"\neffort = "max"\n')
        _, call = self.launch('--', '-p', 'hello')
        argv = call['argv']
        self.assertEqual(argv[:2], ['-p', 'hello'])
        self.assertEqual(values(argv, '--model'), ['any-model[1m]'])
        self.assertEqual(values(argv, '--effort'), ['max'])

    def test_subagent_settings_do_not_reach_the_main_session(self):
        self.write_config('[agents.claude.reviewer]\nmodel = "opus"\neffort = "high"\n'
                          '[agents.claude.researcher]\nmodel = "sonnet"\n')
        _, call = self.launch()
        self.assertNotIn('--model', call['argv'])
        self.assertNotIn('--effort', call['argv'])

    def test_explicit_agent_arguments_beat_the_config_file(self):
        self.write_config('[agents.claude]\nmodel = "config-model"\neffort = "low"\n')
        _, call = self.launch('--', '--model', 'flag-model', '--effort=high')
        argv = call['argv']
        self.assertEqual(argv.count('--model'), 1)
        self.assertEqual(values(argv, '--model'), ['flag-model'])
        self.assertNotIn('--effort', argv)
        self.assertIn('--effort=high', argv)
        self.assertNotIn('config-model', argv)

    def test_invalid_config_file_stops_the_launch(self):
        self.write_config('[agents.claude]\nmodel = 3\n')
        p, call = self.launch(ok=False)
        self.assertIn('agents.claude.model', p.stderr)
        self.assertIsNone(call)

    def test_without_agent_or_command_it_still_fails_with_usage(self):
        p = self.run_pisar(env=self.agent_env, ok=False)
        self.assertEqual(p.returncode, 2)
        p = self.run_pisar('--', 'x', env=self.agent_env, ok=False)
        self.assertEqual(p.returncode, 2)


if __name__ == '__main__':
    unittest.main()
