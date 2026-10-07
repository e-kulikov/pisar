"""Zipapp build, release archive, tag validation and Conventional Commit checks."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from .support import CHECKOUT, clean_environ, commit_all, init_repo, space


SCRIPTS = CHECKOUT / 'scripts'
VERSION = tomllib.loads((CHECKOUT / 'pyproject.toml').read_text())['project']['version']


def run(*args, ok=True, **kwargs):
    p = subprocess.run([str(a) for a in args], text=True, capture_output=True, **kwargs)
    if ok and p.returncode:
        raise AssertionError(f'{args} exited {p.returncode}: {p.stdout}{p.stderr}')
    if not ok and not p.returncode:
        raise AssertionError(f'{args} unexpectedly succeeded: {p.stdout}{p.stderr}')
    return p


class PackagingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='pisar-package-test-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.env = clean_environ(SOURCE_DATE_EPOCH='1700000000',
                                 XDG_DATA_HOME=str(self.base / 'data'), PYTHONPATH='')

    def build(self, name='pisar', env=None):
        output = self.base / name
        p = run(sys.executable, SCRIPTS / 'build-zipapp.py', output, env=env or self.env, cwd=self.base)
        self.assertEqual(p.stdout, f'{output}\n')
        return output

    def package(self, version, zipapp, output, ok=True):
        return run(SCRIPTS / 'package-release.sh', version, zipapp, output,
                   env=self.env, cwd=self.base, ok=ok)

    def test_zipapp_is_executable_reproducible_and_source_only(self):
        first = self.build('one/pisar')
        second = self.build('two/pisar')
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertTrue(first.read_bytes().startswith(b'#!/usr/bin/env python3\n'))
        self.assertTrue(first.stat().st_mode & stat.S_IXUSR)
        with zipfile.ZipFile(first) as archive:
            names = archive.namelist()
            self.assertEqual(names, sorted(names))
            self.assertIn('__main__.py', names)
            self.assertIn('pisar/cli.py', names)
            self.assertIn('pisar/_version.py', names)
            self.assertIn('pisar/skill.md', names)
            self.assertIn('pisar/agent-prompt.md', names)
            self.assertIn('pisar/agent.py', names)
            self.assertFalse([n for n in names if not (n == '__main__.py' or n.startswith('pisar/'))])
            self.assertFalse([n for n in names if '__pycache__' in n or n.endswith('.pyc')])
            self.assertEqual({i.date_time for i in archive.infolist()}, {(2023, 11, 14, 22, 13, 20)})
        changed = self.build('three/pisar', env={**self.env, 'SOURCE_DATE_EPOCH': '1700000002'})
        self.assertNotEqual(first.read_bytes(), changed.read_bytes())

    def test_zipapp_runs_outside_checkout_with_embedded_version(self):
        zipapp = self.build()
        moved = self.base / 'elsewhere/bin/pisar'
        moved.parent.mkdir(parents=True)
        shutil.copy2(zipapp, moved)
        p = run(moved, '--version', env=self.env, cwd=self.base)
        self.assertEqual(p.stdout, f'pisar {VERSION}\n')
        root = self.base / 'data/wiki'
        init_repo(root)
        space(root, 'work/alpha', 'alpha')
        commit_all(root)
        self.assertEqual([s['id'] for s in json.loads(run(moved, 'spaces', env=self.env, cwd=self.base).stdout)['spaces']],
                         ['alpha'])
        self.assertTrue(json.loads(run(moved, 'check', env=self.env, cwd=self.base).stdout)['ok'])

    def test_zipapp_refuses_old_python_clearly(self):
        main = zipfile.ZipFile(self.build()).read('__main__.py').decode()
        self.assertIn('(3, 11)', main)
        compile(main, '__main__.py', 'exec', flags=0, dont_inherit=True)

    def test_release_archive_is_reproducible_and_complete(self):
        zipapp = self.build()
        first = Path(self.package(VERSION, zipapp, self.base / 'out1').stdout.strip())
        second = Path(self.package(VERSION, zipapp, self.base / 'out2').stdout.strip())
        self.assertEqual(first.name, f'pisar_{VERSION}_any.tar.gz')
        self.assertEqual(first.read_bytes(), second.read_bytes())
        prefix = f'pisar_{VERSION}_any'
        with tarfile.open(first) as archive:
            members = {m.name: m for m in archive.getmembers()}
            self.assertEqual(set(members), {prefix, f'{prefix}/pisar', f'{prefix}/README.md'})
            executable = members[f'{prefix}/pisar']
            self.assertEqual((executable.mode & 0o777, executable.uid, executable.gid), (0o755, 0, 0))
            self.assertEqual(members[f'{prefix}/README.md'].mode & 0o777, 0o644)
            self.assertEqual({m.mtime for m in members.values()}, {1700000000})
            self.assertEqual(archive.extractfile(executable).read(), zipapp.read_bytes())
            self.assertEqual(archive.extractfile(f'{prefix}/README.md').read(),
                             (CHECKOUT / 'README.md').read_bytes())
            archive.extractall(self.base / 'extracted', filter='data')
        installed = self.base / 'extracted' / prefix / 'pisar'
        self.assertEqual(run(installed, '--version', env=self.env, cwd=self.base).stdout, f'pisar {VERSION}\n')

    def test_release_archive_refuses_mismatched_executable_version(self):
        zipapp = self.build()
        p = self.package('9.9.9', zipapp, self.base / 'out', ok=False)
        self.assertIn('9.9.9', p.stderr)
        self.assertFalse((self.base / 'out').exists() and list((self.base / 'out').iterdir()))

    def test_release_tag_must_match_project_version(self):
        pyproject = self.base / 'pyproject.toml'
        pyproject.write_text('[project]\nname = "pisar"\nversion = "1.2.3"\n\n[tool.other]\nversion = "7.7.7"\n')
        script = SCRIPTS / 'validate-release-version.sh'
        self.assertEqual(run(script, 'v1.2.3', pyproject).stdout, '1.2.3\n')
        for tag in ('v1.2.4', '1.2.3', 'v1.2.3-rc1', 'v1.2', 'v7.7.7', ''):
            with self.subTest(tag):
                run(script, tag, pyproject, ok=False)
        run(script, 'v1.2.3', ok=False)

    def test_project_version_is_the_release_tag_version(self):
        out = run(SCRIPTS / 'validate-release-version.sh', f'v{VERSION}', CHECKOUT / 'pyproject.toml').stdout
        self.assertEqual(out, f'{VERSION}\n')

    def test_conventional_commit_check(self):
        repo = self.base / 'repo'
        init_repo(repo)
        env = {**self.env, 'GIT_AUTHOR_DATE': '@1700000000 +0000', 'GIT_COMMITTER_DATE': '@1700000000 +0000'}
        for subject in ('chore: start', 'feat(cli)!: breaking change', 'fix: repair'):
            run('git', '-C', repo, 'commit', '-q', '--allow-empty', '--no-gpg-sign', '-m', subject, env=env)
        script = SCRIPTS / 'check-conventional-commits.sh'
        run(script, 'HEAD', cwd=repo)
        run('git', '-C', repo, 'commit', '-q', '--allow-empty', '--no-gpg-sign', '-m', 'Fix the thing', env=env)
        self.assertIn('Fix the thing', run(script, 'HEAD', cwd=repo, ok=False).stderr)

    def test_release_configuration_matches_python_packaging(self):
        config = json.loads((CHECKOUT / 'release-please-config.json').read_text())
        manifest = json.loads((CHECKOUT / '.release-please-manifest.json').read_text())
        self.assertEqual(config['release-type'], 'python')
        self.assertTrue(config['draft'])
        self.assertTrue(config['force-tag-creation'])
        self.assertFalse(config['include-component-in-tag'])
        self.assertEqual(config['packages']['.'].get('package-name'), 'pisar')
        self.assertEqual(manifest, {'.': VERSION})


if __name__ == '__main__':
    unittest.main()
