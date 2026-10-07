"""Root, runtime and ruwana selection: flags override PISAR_* environment."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tomllib
from .support import CHECKOUT, EXECUTABLE, Fixture, clean_environ, commit_all, init_repo, space
from .test_save import BODY


def project_version():
    return tomllib.loads((CHECKOUT / 'pyproject.toml').read_text())['project']['version']


class SettingsTests(Fixture):
    def setUp(self):
        super().setUp()
        self.data_home = self.base / 'data'
        self.default_root = self.data_home / 'wiki'
        init_repo(self.default_root)
        space(self.default_root, 'work/default', 'default-space')
        commit_all(self.default_root)
        self.env_root = self.base / 'env-wiki'
        init_repo(self.env_root)
        space(self.env_root, 'work/env', 'env-space')
        commit_all(self.env_root)

    def spaces(self, *args, env=None):
        result = json.loads(self.run_pisar(*args, 'spaces', env=env).stdout)
        return sorted(s['id'] for s in result['spaces'])

    def capture(self, *flags, env=None, ok=True, ident='settings-capture'):
        return self.run_pisar(*flags, 'capture', '--space', 'work/alpha', '--id', ident,
                              '--source', self.external(), env=env, ok=ok)

    def journals(self, base):
        return list(Path(base).glob('roots/*/operations/*.json'))

    def fake_ruwana(self, name):
        """A stand-in that records how it was called, then fails the operation."""
        path = self.base / 'bin' / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('#!/bin/sh\n'
                        'printf "%s\\n%s\\n" "$0" "$WIKI_ROOT" >"$FAKE_RUWANA_LOG"\n'
                        'echo "synthetic ruwana failure" >&2\nexit 3\n')
        path.chmod(0o755)
        return path

    def decoy_in_root(self, *names):
        """Same relative paths inside the root, which is ruwana's working directory."""
        for name in names:
            path = self.root / 'bin' / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('#!/bin/sh\necho decoy >"$DECOY_RUWANA_LOG"\nexit 3\n')
            path.chmod(0o755)
        commit_all(self.root)

    def save_with_tasks(self, *flags, env, cwd=None):
        source = self.external()
        plan = dict(schema_version=1, operation_id='settings-save', space_id='work/alpha',
                    source=dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
                    meeting=dict(id='settings-meeting', title='Settings meeting', body=BODY),
                    tasks=[dict(id='prepare', space_id='work/alpha', title='Prepare', agreed=True)])
        path = self.base / 'settings-plan.json'
        path.write_text(json.dumps(plan))
        log = self.base / 'ruwana.log'
        decoy = self.base / 'decoy.log'
        env = {**env, 'FAKE_RUWANA_LOG': str(log), 'DECOY_RUWANA_LOG': str(decoy)}
        p = self.run_pisar('--root', self.root, '--state-dir', self.state, *flags,
                           'save', '--plan', path, env=env, ok=False, cwd=cwd)
        self.assertFalse(decoy.exists(), 'ruwana path resolved inside the root')
        self.assertIn('synthetic ruwana failure', p.stderr)
        return log.read_text().splitlines()

    def test_version_needs_no_root_and_matches_project_metadata(self):
        env = clean_environ(PISAR_ROOT=str(self.base / 'absent'), HOME=str(self.base / 'no-home'),
                            XDG_DATA_HOME=str(self.base / 'no-data'))
        p = self.run_pisar('--version', env=env)
        self.assertEqual(p.stdout, f'pisar {project_version()}\n')
        self.assertEqual(p.stderr, '')

    def test_package_has_no_second_version_literal(self):
        init = (CHECKOUT / 'pisar/__init__.py').read_text()
        self.assertIsNone(re.search(r'__version__\s*=\s*[\'"]', init))
        self.assertFalse((CHECKOUT / 'pisar/_version.py').exists(), 'generated only inside builds')

    def test_root_flag_overrides_environment_and_default(self):
        env = {**self.env, 'PISAR_ROOT': str(self.env_root)}
        self.assertEqual(self.spaces('--root', self.root, env=env), ['alpha', 'beta', 'home'])

    def test_root_environment_overrides_default(self):
        env = {**self.env, 'PISAR_ROOT': str(self.env_root)}
        self.assertEqual(self.spaces(env=env), ['env-space'])

    def test_default_root_is_xdg_data_home_wiki(self):
        self.assertEqual(self.spaces(), ['default-space'])

    def test_empty_root_environment_is_unset(self):
        self.assertEqual(self.spaces(env={**self.env, 'PISAR_ROOT': ''}), ['default-space'])

    def test_default_root_falls_back_to_home_without_absolute_xdg(self):
        home = self.base / 'home'
        root = home / '.local/share/wiki'
        init_repo(root)
        space(root, 'personal/home-default', 'home-default')
        commit_all(root)
        for xdg in (None, '', 'relative/data'):
            env = clean_environ(HOME=str(home))
            env.pop('XDG_DATA_HOME', None)
            if xdg is not None:
                env['XDG_DATA_HOME'] = xdg
            self.assertEqual(self.spaces(env=env), ['home-default'], xdg)

    def test_legacy_wiki_root_is_not_a_pisar_setting(self):
        env = {**self.env, 'WIKI_ROOT': str(self.env_root)}
        self.assertEqual(self.spaces(env=env), ['default-space'])

    def test_missing_default_root_is_an_error(self):
        env = clean_environ(XDG_DATA_HOME=str(self.base / 'empty-data'))
        p = self.run_pisar('spaces', env=env, ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertTrue(p.stderr.startswith('pisar: '), p.stderr)
        self.assertIn(str(self.base / 'empty-data/wiki'), p.stderr)

    def test_state_flag_overrides_environment(self):
        flag_state, env_state = self.base / 'flag-state', self.base / 'env-state'
        env = {**self.env, 'PISAR_STATE_DIR': str(env_state)}
        self.capture('--root', self.root, '--state-dir', flag_state, env=env)
        self.assertEqual(len(self.journals(flag_state)), 1)
        self.assertFalse(env_state.exists())

    def test_state_environment_overrides_default(self):
        env_state = self.base / 'env-state'
        env = {**self.env, 'PISAR_STATE_DIR': str(env_state)}
        self.capture('--root', self.root, env=env)
        self.assertEqual(len(self.journals(env_state)), 1)
        self.assertFalse((self.data_home / 'pisar').exists())

    def test_default_state_is_xdg_data_home_pisar(self):
        self.capture('--root', self.root)
        self.assertEqual(len(self.journals(self.data_home / 'pisar')), 1)

    def test_default_root_and_default_state_do_not_overlap(self):
        alpha = space(self.default_root, 'work/alpha', 'alpha')
        commit_all(self.default_root)
        self.capture()
        self.assertEqual(len(self.journals(self.data_home / 'pisar')), 1)
        self.assertTrue((alpha / 'inbox/settings-capture.md').is_file())
        self.assertEqual(subprocess.run(['git', '-C', str(self.default_root), 'status', '--porcelain'],
                                        capture_output=True, text=True).stdout, '')

    def test_overlapping_root_and_state_are_refused_before_runtime_writes(self):
        cases = [
            ('state is root', ['--state-dir', self.root], {}),
            ('state inside root', ['--state-dir', self.root / 'runtime'], {}),
            ('root inside state flag', ['--state-dir', self.base], {}),
            ('root inside state env', [], {'PISAR_STATE_DIR': str(self.base)}),
            ('default root inside state env', [], {'PISAR_ROOT': '', 'PISAR_STATE_DIR': str(self.data_home)}),
        ]
        space(self.default_root, 'work/alpha', 'alpha')
        commit_all(self.default_root)
        for name, flags, extra in cases:
            with self.subTest(name):
                root_flags = [] if 'PISAR_ROOT' in extra else ['--root', self.root]
                env = {**self.env, 'PISAR_ROOT': str(self.root), **extra}
                p = self.capture(*root_flags, *flags, env=env, ok=False, ident='overlap')
                self.assertRegex(p.stderr, r'^pisar: .*runtime')
                self.assertFalse(list(self.base.rglob('roots')), name)
                self.assertFalse(list(self.base.rglob('overlap.md')), name)

    def test_ruwana_flag_overrides_environment(self):
        flag, env_bin = self.fake_ruwana('flag-ruwana'), self.fake_ruwana('env-ruwana')
        calls = self.save_with_tasks('--ruwana', flag, env={**self.env, 'PISAR_RUWANA_BIN': str(env_bin)})
        self.assertEqual(calls[0], str(flag))

    def test_ruwana_environment_overrides_path(self):
        env_bin = self.fake_ruwana('env-ruwana')
        path_bin = self.fake_ruwana('ruwana')
        env = {**self.env, 'PISAR_RUWANA_BIN': str(env_bin),
               'PATH': f'{path_bin.parent}:{self.env["PATH"]}'}
        self.assertEqual(self.save_with_tasks(env=env)[0], str(env_bin))

    def test_ruwana_default_uses_path_and_receives_only_child_wiki_root(self):
        path_bin = self.fake_ruwana('ruwana')
        env = {**self.env, 'PATH': f'{path_bin.parent}:{self.env["PATH"]}',
               'WIKI_ROOT': str(self.env_root)}
        calls = self.save_with_tasks(env=env)
        self.assertEqual(calls, [str(path_bin), str(self.root.resolve())])

    def test_relative_ruwana_flag_resolves_from_invocation_directory(self):
        flag = self.fake_ruwana('flag-ruwana')
        self.decoy_in_root('flag-ruwana')
        calls = self.save_with_tasks('--ruwana', 'bin/flag-ruwana', env=self.env, cwd=self.base)
        self.assertEqual(calls[0], str(flag))

    def test_relative_ruwana_environment_resolves_from_invocation_directory(self):
        env_bin = self.fake_ruwana('env-ruwana')
        self.decoy_in_root('env-ruwana')
        calls = self.save_with_tasks(env={**self.env, 'PISAR_RUWANA_BIN': 'bin/env-ruwana'}, cwd=self.base)
        self.assertEqual(calls[0], str(env_bin))

    def test_relative_path_entry_resolves_from_invocation_directory(self):
        path_bin = self.fake_ruwana('ruwana')
        self.decoy_in_root('ruwana')
        calls = self.save_with_tasks(env={**self.env, 'PATH': f'bin:{self.env["PATH"]}'}, cwd=self.base)
        self.assertEqual(calls[0], str(path_bin))

    def test_launcher_and_module_work_outside_checkout(self):
        if EXECUTABLE:
            self.skipTest('source launcher test')
        p = subprocess.run([str(CHECKOUT / 'bin/pisar'), '--root', self.root, 'spaces'], cwd=self.base,
                           env=self.env, text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sorted(s['id'] for s in json.loads(p.stdout)['spaces']), ['alpha', 'beta', 'home'])
        self.assertFalse((CHECKOUT / 'bin/wiki').exists())
        self.assertFalse((CHECKOUT / 'wiki_cli').exists())

    def test_project_metadata_names_pisar(self):
        project = tomllib.loads((CHECKOUT / 'pyproject.toml').read_text())['project']
        self.assertEqual(project['name'], 'pisar')
        self.assertEqual(project['scripts'], {'pisar': 'pisar.cli:main'})
        self.assertEqual(project['requires-python'], '>=3.11')
        self.assertEqual(project.get('dependencies', []), [])
