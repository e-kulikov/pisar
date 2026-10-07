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
            self.assertTrue(rule.startswith(('Bash(git ', 'Bash(pisar ')), rule)
            self.assertNotIn(rule, ('Bash(git *)', 'Bash(pisar *)', 'Bash(mv *)', 'Bash(*)', 'Bash'))
            self.assertFalse(rule.startswith('Bash(mv'), rule)
        self.assertFalse([r for r in allowed if 'pisar --' in r], 'global flags would allow --ruwana')

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

    def test_without_agent_or_command_it_still_fails_with_usage(self):
        p = self.run_pisar(env=self.agent_env, ok=False)
        self.assertEqual(p.returncode, 2)
        p = self.run_pisar('--', 'x', env=self.agent_env, ok=False)
        self.assertEqual(p.returncode, 2)


if __name__ == '__main__':
    unittest.main()
