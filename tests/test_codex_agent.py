"""`pisar --agent codex`: isolated CODEX_HOME, generated configuration, strict MCP, refused options."""
import json
import os
from pathlib import Path
import stat
import tomllib
import unittest

from .support import CHECKOUT, Fixture

FAKE = '''#!/usr/bin/env python3
import json, os, sys
OUT = %r
home = os.environ.get("CODEX_HOME", "")
out = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ), "files": {}}
for name in ("config.toml", "instructions.md"):
    try:
        with open(os.path.join(home, name), encoding="utf-8") as stream:
            out["files"][name] = stream.read()
    except OSError:
        pass
with open(OUT, "w") as stream:
    json.dump(out, stream)
'''


class CodexAgentTests(Fixture):
    def setUp(self):
        super().setUp()
        self.bin = self.base / 'fakebin'
        self.bin.mkdir()
        self.out = self.base / 'codex-call.json'
        fake = self.bin / 'codex'
        fake.write_text(FAKE % str(self.out))  # the child environment is an allowlist
        fake.chmod(0o755)
        self.agent_env = {**self.env, 'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}',
                          'WIKI_ROOT': '/must/not/leak',
                          'CODEX_HOME': str(self.base / 'foreign-codex-home'),
                          'SECRET_TOKEN_FOR_TEST': 'do-not-pass'}

    def launch(self, *args, ok=True, env=None):
        p = self.run_pisar('--root', self.root, '--state-dir', self.state, '--agent', 'codex',
                           *args, env=env or self.agent_env, ok=ok)
        return p, (json.loads(self.out.read_text()) if self.out.exists() else None)

    def config(self, call):
        return tomllib.loads(call['files']['config.toml'])

    def test_starts_codex_in_the_root_with_an_isolated_codex_home_in_the_state_dir(self):
        _, call = self.launch()
        self.assertEqual(Path(call['cwd']).resolve(), self.root.resolve())
        home = self.state.resolve() / 'agents' / 'codex'
        self.assertEqual(call['env']['CODEX_HOME'], str(home))
        self.assertEqual(call['env']['HOME'], str(home / 'home'))
        for directory in (home, home / 'home', self.state.resolve() / 'lessons'):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
        self.assertEqual(stat.S_IMODE((home / 'config.toml').stat().st_mode), 0o600)

    def test_child_gets_an_explicit_environment_and_never_wiki_root_or_ambient_secrets(self):
        _, call = self.launch()
        env = call['env']
        self.assertEqual(env['PISAR_ROOT'], str(self.root.resolve()))
        self.assertEqual(env['PISAR_STATE_DIR'], str(self.state.resolve()))
        self.assertIn('PISAR_RUWANA_BIN', env)
        self.assertNotIn('WIKI_ROOT', env)
        self.assertNotIn('SECRET_TOKEN_FOR_TEST', env)
        self.assertNotIn('XDG_DATA_HOME', env)

    def test_user_codex_configuration_never_applies(self):
        _, call = self.launch()
        self.assertEqual(call['env']['CODEX_HOME'], str(self.state.resolve() / 'agents' / 'codex'))
        self.assertNotEqual(call['env']['CODEX_HOME'], self.agent_env['CODEX_HOME'])
        self.assertEqual(call['argv'][0], '--no-daemon')

    def test_instructions_are_the_packaged_agent_prompt(self):
        _, call = self.launch()
        prompt = (CHECKOUT / 'pisar/agent-prompt.md').read_text(encoding='utf-8')
        self.assertTrue(call['files']['instructions.md'].startswith(prompt + '\n'))
        self.assertIn('## Tools in this session', call['files']['instructions.md'])
        self.assertEqual(self.config(call)['model_instructions_file'],
                         str(self.state.resolve() / 'agents' / 'codex' / 'instructions.md'))

    def test_main_session_has_no_shell_subagents_plugins_or_web_search(self):
        _, call = self.launch()
        config = self.config(call)
        self.assertEqual(config['web_search'], 'disabled')
        self.assertEqual(config['approval_policy'], 'on-request')
        features = config['features']
        for name in ('shell_tool', 'multi_agent', 'apps', 'plugins', 'browser_use', 'computer_use', 'hooks'):
            self.assertIs(features[name], False, name)

    def test_permissions_profile_writes_only_the_lesson_drafts(self):
        _, call = self.launch()
        config = self.config(call)
        profile = config['permissions']['pisar']
        self.assertEqual(config['default_permissions'], 'pisar')
        self.assertEqual(profile['filesystem'], {
            ':minimal': 'read', str(self.root.resolve()): 'read',
            str(self.state.resolve() / 'lessons'): 'write'})
        self.assertNotIn(':root', profile['filesystem'])  # the whole filesystem is not readable
        self.assertIs(profile['network']['enabled'], False)
        self.assertNotIn('sandbox_mode', config)
        self.assertNotIn('--add-dir', call['argv'])

    def test_model_and_effort_come_from_the_config_and_caller_arguments_win(self):
        config_file = self.base / 'config/pisar/config.toml'
        config_file.parent.mkdir(parents=True)
        config_file.write_text('[agents.codex]\nmodel = "gpt-main"\neffort = "high"\n')
        _, call = self.launch()
        self.assertEqual(call['argv'][call['argv'].index('-m') + 1], 'gpt-main')
        self.assertEqual(self.config(call)['model_reasoning_effort'], 'high')
        _, call = self.launch('--', '--model', 'gpt-other')
        self.assertEqual(call['argv'].count('-m'), 1)
        self.assertEqual(call['argv'][call['argv'].index('-m') + 1], 'gpt-other')

    def test_arguments_that_widen_the_confinement_are_refused(self):
        for extra in (['--sandbox', 'danger-full-access'], ['-s', 'workspace-write'], ['-c', 'shell_tool=true'],
                      ['--config=features.shell_tool=true'], ['--enable', 'shell_tool'], ['--profile', 'x'],
                      ['--add-dir', '/'], ['-C', '/'], ['--cd', '/'], ['--remote', 'ws://x'],
                      ['--dangerously-bypass-approvals-and-sandbox'], ['--yolo'], ['-ccfoo=1'],
                      ['--approve-for-me'], ['--ask-for-approval', 'never']):
            with self.subTest(extra=extra):
                p, call = self.launch('--', *extra, ok=False)
                self.assertNotEqual(p.returncode, 0)
                self.assertIsNone(call)
                self.assertIn('cannot be passed to the agent', p.stderr)

    def test_only_the_listed_config_overrides_are_accepted_and_effort_can_be_set(self):
        _, call = self.launch('--', '-c', 'model_reasoning_effort=high')
        self.assertEqual(self.config(call)['model_reasoning_effort'], 'high')
        self.assertNotIn('-c', call['argv'])
        _, call = self.launch('--', '--config', 'model_reasoning_effort="low"')
        self.assertEqual(self.config(call)['model_reasoning_effort'], 'low')
        _, call = self.launch('--', '-cmodel_reasoning_effort=max')
        self.assertEqual(self.config(call)['model_reasoning_effort'], 'max')
        _, call = self.launch('--', '-c', 'model=gpt-x')
        self.assertEqual(call['argv'][call['argv'].index('-m') + 1], 'gpt-x')
        for extra in (['-c', 'shell_tool=true'], ['-c', 'features.shell_tool=true'],
                      ['-c', 'model_reasoning_effort.x=1'], ['-c', 'model_reasoning_effort=high,low'],
                      ['--config=mcp_servers.x.command=sh'], ['-c', 'web_search="live"']):
            with self.subTest(extra=extra):
                self.out.unlink(missing_ok=True)
                p, call = self.launch('--', *extra, ok=False)
                self.assertNotEqual(p.returncode, 0)
                self.assertIsNone(call)
                self.assertIn('cannot be passed to the agent', p.stderr)

    def test_pisar_server_keeps_the_users_home_and_git_setup_and_only_its_tools(self):
        home = self.base / 'users-home'
        env = {**self.agent_env, 'HOME': str(home)}
        _, call = self.launch(env=env)
        server = self.config(call)['mcp_servers']['pisar']
        self.assertEqual(server['env']['HOME'], str(home))
        self.assertNotIn('GIT_CONFIG_GLOBAL', server['env'])
        self.assertEqual(call['env']['HOME'], str(self.state.resolve() / 'agents' / 'codex' / 'home'))
        self.assertEqual(server['enabled_tools'], ['pisar', 'git', 'lesson_accept', 'lesson_review'])

    def test_mcp_servers_are_only_the_root_file_and_the_pisar_server(self):
        _, call = self.launch()
        servers = self.config(call)['mcp_servers']
        self.assertEqual(list(servers), ['pisar'])
        server = servers['pisar']
        self.assertIn('mcp-serve', server['args'])
        self.assertEqual(server['env']['PISAR_ROOT'], str(self.root.resolve()))
        self.assertEqual(server['env']['PISAR_RUWANA_BIN'], call['env']['PISAR_RUWANA_BIN'])
        self.assertNotIn('WIKI_ROOT', server['env'])

    def test_root_mcp_file_is_translated_and_symlinks_are_ignored(self):
        config = self.root / '.mcp.json'
        config.write_text(json.dumps({'mcpServers': {
            'local': {'command': 'tool', 'args': ['a'], 'env': {'K': 'V'}},
            'remote': {'type': 'http', 'url': 'https://example.invalid/mcp', 'headers': {'H': '1'}}}}))
        _, call = self.launch()
        servers = self.config(call)['mcp_servers']
        self.assertEqual(servers['local']['command'], 'tool')
        self.assertEqual(servers['local']['env'], {'K': 'V'})
        self.assertEqual(servers['remote']['url'], 'https://example.invalid/mcp')
        self.assertEqual(servers['remote']['http_headers'], {'H': '1'})
        self.assertIn('pisar', servers)
        config.unlink()
        target = self.base / 'elsewhere.json'
        target.write_text(json.dumps({'mcpServers': {'evil': {'command': 'x'}}}))
        config.symlink_to(target)
        _, call = self.launch()
        self.assertEqual(list(self.config(call)['mcp_servers']), ['pisar'])

    def test_a_root_mcp_server_cannot_replace_the_pisar_server(self):
        (self.root / '.mcp.json').write_text(json.dumps({'mcpServers': {'pisar': {'command': 'x'}}}))
        p, _ = self.launch(ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('MCP server name is not allowed', p.stderr)

    def test_relative_ruwana_is_made_absolute_before_codex_changes_directory(self):
        env = {**self.agent_env, 'PISAR_RUWANA_BIN': 'relative/ruwana'}
        _, call = self.launch(env=env)
        self.assertTrue(Path(call['env']['PISAR_RUWANA_BIN']).is_absolute())
        self.assertTrue(os.path.isabs(self.config(call)['mcp_servers']['pisar']['env']['PISAR_RUWANA_BIN']))

if __name__ == '__main__':
    unittest.main()
