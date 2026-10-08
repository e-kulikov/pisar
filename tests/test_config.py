"""Configuration file and precedence: flag > environment > config file > default."""
import hashlib
import json
import os
from unittest import mock

from pisar import settings
from .support import Fixture, commit_all, init_repo, space
from .test_save import BODY


class ConfigTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = self.base / 'config/pisar/config.toml'
        self.config_root = self.base / 'config-wiki'
        init_repo(self.config_root)
        space(self.config_root, 'work/configured', 'configured')
        commit_all(self.config_root)

    def write_config(self, text, path=None):
        path = path or self.config
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def show(self, *flags, env=None):
        return json.loads(self.run_pisar(*flags, 'config', 'show', env=env).stdout)

    def spaces(self, *flags, env=None):
        result = json.loads(self.run_pisar(*flags, 'spaces', env=env).stdout)
        return sorted(s['id'] for s in result['spaces'])

    def refused(self, text, *keys):
        self.write_config(text)
        p = self.run_pisar('--root', self.root, 'spaces', ok=False)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertTrue(p.stderr.startswith('pisar: '), p.stderr)
        self.assertIn(str(self.config), p.stderr)
        for key in keys:
            self.assertIn(key, p.stderr)
        self.assertEqual(p.stdout, '')
        return p

    # Effective values and their sources

    def test_without_a_file_every_value_is_a_default(self):
        result = self.show()
        self.assertEqual(result['file'], {'path': str(self.config), 'exists': False})
        data = self.base / 'data'
        self.assertEqual(result['settings'], {
            'root': {'value': str(data / 'wiki'), 'source': 'default'},
            'state_dir': {'value': str(data / 'pisar'), 'source': 'default'},
            'ruwana': {'value': 'ruwana', 'source': 'default'},
            'default_agent': {'value': None, 'source': 'default'},
            'agents.claude.model': {'value': None, 'source': 'default'},
            'agents.claude.effort': {'value': None, 'source': 'default'},
            'agents.claude.reviewer.model': {'value': 'opus', 'source': 'default'},
            'agents.claude.reviewer.effort': {'value': 'high', 'source': 'default'},
            'agents.claude.researcher.model': {'value': 'sonnet', 'source': 'default'},
            'agents.claude.researcher.effort': {'value': 'medium', 'source': 'default'},
        })

    def test_file_values_are_reported_with_config_source(self):
        self.write_config(f'''
default_agent = "claude"
root = "{self.config_root}"
state_dir = "{self.base / 'config-state'}"
ruwana = "/opt/ruwana/bin/ruwana"
[agents.claude]
model = "claude-sonnet-x"
effort = "low"
[agents.claude.reviewer]
model = "some-model"
[agents.claude.researcher]
effort = "max"
''')
        result = self.show()
        self.assertTrue(result['file']['exists'])
        settings = result['settings']
        expected = {
            'root': (str(self.config_root), 'config'),
            'state_dir': (str(self.base / 'config-state'), 'config'),
            'ruwana': ('/opt/ruwana/bin/ruwana', 'config'),
            'default_agent': ('claude', 'config'),
            'agents.claude.model': ('claude-sonnet-x', 'config'),
            'agents.claude.effort': ('low', 'config'),
            'agents.claude.reviewer.model': ('some-model', 'config'),
            'agents.claude.reviewer.effort': ('high', 'default'),
            'agents.claude.researcher.model': ('sonnet', 'default'),
            'agents.claude.researcher.effort': ('max', 'config'),
        }
        self.assertEqual({k: (v['value'], v['source']) for k, v in settings.items()}, expected)

    def test_flag_beats_environment_beats_file(self):
        self.write_config(f'root = "{self.config_root}"\nstate_dir = "/config/state"\nruwana = "config-ruwana"\n')
        env = {**self.env, 'PISAR_ROOT': '/env/root', 'PISAR_STATE_DIR': '/env/state',
               'PISAR_RUWANA_BIN': 'env-ruwana'}
        settings = self.show(env=env)['settings']
        self.assertEqual([(settings[k]['value'], settings[k]['source']) for k in ('root', 'state_dir', 'ruwana')],
                         [('/env/root', 'env'), ('/env/state', 'env'), ('env-ruwana', 'env')])
        settings = self.show('--root', '/flag/root', '--state-dir', '/flag/state', '--ruwana', 'flag-ruwana',
                             env=env)['settings']
        self.assertEqual([(settings[k]['value'], settings[k]['source']) for k in ('root', 'state_dir', 'ruwana')],
                         [('/flag/root', 'flag'), ('/flag/state', 'flag'), ('flag-ruwana', 'flag')])

    def test_empty_environment_falls_through_to_the_file(self):
        self.write_config(f'root = "{self.config_root}"\n')
        env = {**self.env, 'PISAR_ROOT': ''}
        self.assertEqual(self.show(env=env)['settings']['root'],
                         {'value': str(self.config_root), 'source': 'config'})

    def test_show_needs_no_existing_root(self):
        self.write_config('root = "/nowhere/at/all"\n')
        self.assertEqual(self.show()['settings']['root']['value'], '/nowhere/at/all')

    # The file location

    def test_file_falls_back_to_home_without_absolute_xdg_config_home(self):
        home = self.base / 'home'
        fallback = self.write_config(f'root = "{self.config_root}"\n', home / '.config/pisar/config.toml')
        for xdg in (None, '', 'relative/config'):
            with self.subTest(xdg=xdg):
                env = {**self.env, 'HOME': str(home)}
                env.pop('XDG_CONFIG_HOME')
                if xdg is not None:
                    env['XDG_CONFIG_HOME'] = xdg
                self.assertEqual(self.show(env=env)['file'], {'path': str(fallback), 'exists': True})
                self.assertEqual(self.spaces(env=env), ['configured'])

    # The file changes behaviour

    def test_root_from_the_file_is_used_and_environment_and_flag_override_it(self):
        self.write_config(f'root = "{self.config_root}"\n')
        self.assertEqual(self.spaces(), ['configured'])
        self.assertEqual(self.spaces(env={**self.env, 'PISAR_ROOT': str(self.root)}), ['alpha', 'beta', 'home'])
        self.assertEqual(self.spaces('--root', self.root,
                                     env={**self.env, 'PISAR_ROOT': str(self.base / 'absent')}),
                         ['alpha', 'beta', 'home'])

    def test_state_dir_from_the_file_receives_the_journal(self):
        state = self.base / 'config-state'
        self.write_config(f'state_dir = "{state}"\n')
        self.run_pisar('--root', self.root, 'capture', '--space', 'work/alpha', '--id', 'from-config',
                       '--source', self.external())
        self.assertEqual(len(list(state.glob('roots/*/operations/*.json'))), 1)
        self.assertFalse((self.base / 'data/pisar').exists())

    def test_ruwana_from_the_file_is_called(self):
        fake = self.base / 'bin/config-ruwana'
        fake.parent.mkdir()
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$0" >"$FAKE_RUWANA_LOG"\n'
                        'echo "synthetic ruwana failure" >&2\nexit 3\n')
        fake.chmod(0o755)
        self.write_config(f'ruwana = "{fake}"\n')
        source = self.external()
        plan = dict(schema_version=1, operation_id='config-save', space_id='work/alpha',
                    source=dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
                    meeting=dict(id='config-meeting', title='Config meeting', body=BODY),
                    tasks=[dict(id='prepare', space_id='work/alpha', title='Prepare', agreed=True)])
        path = self.base / 'config-plan.json'
        path.write_text(json.dumps(plan))
        log = self.base / 'ruwana.log'
        p = self.run_pisar('--root', self.root, '--state-dir', self.state, 'save', '--plan', path,
                           env={**self.env, 'FAKE_RUWANA_LOG': str(log)}, ok=False)
        self.assertIn('synthetic ruwana failure', p.stderr)
        self.assertEqual(log.read_text().splitlines(), [str(fake)])

    # Invalid files are errors naming the file and the key

    def test_unknown_keys_are_errors(self):
        self.refused('rot = "/x"\n', 'rot')
        self.refused('[agents.claude]\nmodle = "x"\n', 'agents.claude.modle')
        self.refused('[agents.claude.reviewer]\ntemperature = 1\n', 'agents.claude.reviewer.temperature')
        self.refused('[agents.codex]\nmodel = "x"\n', 'agents.codex')
        self.refused('[agents.claude.critic]\nmodel = "x"\n', 'agents.claude.critic')

    def test_wrong_types_are_errors(self):
        self.refused('root = 1\n', 'root')
        self.refused('state_dir = ["/a"]\n', 'state_dir')
        self.refused('ruwana = true\n', 'ruwana')
        self.refused('default_agent = 1\n', 'default_agent')
        self.refused('agents = "claude"\n', 'agents')
        self.refused('[agents]\nclaude = "x"\n', 'agents.claude')
        self.refused('[agents.claude]\nmodel = 3\n', 'agents.claude.model')
        self.refused('[agents.claude]\nreviewer = "opus"\n', 'agents.claude.reviewer')
        self.refused('[agents.claude.researcher]\neffort = 2\n', 'agents.claude.researcher.effort')

    def test_relative_root_and_state_dir_are_errors(self):
        self.refused('root = "relative/wiki"\n', 'root', 'absolute')
        self.refused('state_dir = "state"\n', 'state_dir', 'absolute')
        self.refused('root = "~/wiki"\n', 'root', 'absolute')

    def test_relative_ruwana_path_is_an_error_but_a_command_name_is_not(self):
        self.refused('ruwana = "bin/ruwana"\n', 'ruwana')
        self.write_config('ruwana = "ruwana-custom"\n')
        self.assertEqual(self.show()['settings']['ruwana'], {'value': 'ruwana-custom', 'source': 'config'})

    def test_empty_or_blank_strings_are_errors(self):
        self.refused('root = ""\n', 'root')
        self.refused('ruwana = " "\n', 'ruwana')
        self.refused('default_agent = ""\n', 'default_agent')
        self.refused('[agents.claude]\nmodel = "  "\n', 'agents.claude.model')

    def test_unsupported_default_agent_is_an_error(self):
        self.refused('default_agent = "nope"\n', 'default_agent', 'nope')

    def test_invalid_toml_is_an_error(self):
        self.refused('root = \n')

    def test_a_directory_in_place_of_the_file_is_an_error(self):
        self.config.mkdir(parents=True)
        p = self.run_pisar('--root', self.root, 'spaces', ok=False)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn(str(self.config), p.stderr)

    def test_invalid_file_fails_config_show_too(self):
        self.write_config('rot = 1\n')
        p = self.run_pisar('config', 'show', ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn('rot', p.stderr)

    def test_version_and_skill_ignore_an_invalid_file(self):
        self.write_config('rot = 1\n')
        self.run_pisar('--version')
        self.run_pisar('--skill')

    # The accessor later commands use for subagents

    def test_agent_options_apply_role_defaults_and_file_values(self):
        self.write_config('[agents.claude]\nmodel = "main"\n[agents.claude.reviewer]\neffort = "max"\n')
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.base / 'config')}):
            self.assertEqual(settings.agent_options('claude'), {'model': 'main', 'effort': None})
            self.assertEqual(settings.agent_options('claude', 'reviewer'), {'model': 'opus', 'effort': 'max'})
            self.assertEqual(settings.agent_options('claude', 'researcher'),
                             {'model': 'sonnet', 'effort': 'medium'})
