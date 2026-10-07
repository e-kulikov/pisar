"""Write operations are hook-free: no repository hook may run, in the root or in a child repository."""
import json
import os
from pathlib import Path
import stat
import subprocess
import unittest

from .support import git
from .test_spacecmd import SpaceCommandFixture

HOOKS = ('reference-transaction', 'pre-commit', 'prepare-commit-msg', 'commit-msg', 'post-commit',
         'post-checkout', 'post-merge', 'pre-merge-commit', 'post-rewrite', 'post-index-change')


class HookFreeWrites(SpaceCommandFixture):
    def install(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        for name in HOOKS:
            script = directory / name
            script.write_text(f'#!/bin/sh\necho {name} >> "{self.ran}"\nexit 0\n')
            script.chmod(script.stat().st_mode | stat.S_IXUSR)

    def sentinel(self):
        return self.ran.read_text().split() if self.ran.exists() else []

    def test_no_hook_runs_during_any_write_operation(self):
        module = self.add_submodule()
        origin = self.origin()
        source = self.external()
        plan = self.base / 'plan.json'
        plan.write_text(json.dumps(dict(
            schema_version=1, operation_id='hook-save', space_id='work/alpha',
            source=dict(path=str(source), sha256=__import__('hashlib').sha256(source.read_bytes()).hexdigest()),
            meeting=dict(id='hook-meeting', title='Hook meeting', body='Agreed to launch.'), tasks=[], pages=[])))
        review = self.base / 'review.json'
        self.ran = self.base / 'sentinel-ran'
        template = self.base / 'template'
        self.install(template / 'hooks')  # copied into every repository `git submodule add` clones
        with (self.base / 'home/.gitconfig').open('a') as config:
            config.write(f'[init]\n\ttemplateDir = {template}\n')
        for repo in (self.root, module):
            self.install(Path(git(repo, 'rev-parse', '--absolute-git-dir')) / 'hooks')
        self.ran.unlink(missing_ok=True)

        self.cli('capture', '--space', 'work/alpha', '--id', 'hook-capture', '--source', source)
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan)
        self.cli('space', 'create', '--domain', 'work', '--kind', 'project', '--title', 'Hook probe')
        self.cli('space', 'move', 'work/hook-probe', '--to', 'area')
        self.cli('space', 'archive', 'work/hook-probe')
        self.cli('domain', 'add', '--id', 'extra', '--title', 'Extra', '--repo', origin)
        started = self.data('lesson', 'start', '--from', 'work', '--to', 'work/module', '--title', 'Hook lesson')
        Path(started['draft']).write_text('Plan the launch around parallel workstreams.\n')
        checked = self.data('lesson', 'check', '--batch', started['batch'])
        review.write_text(json.dumps({'schema_version': 1, 'revision': checked['revision'],
                                      'sha256': checked['sha256'], 'verdict': 'clear', 'findings': [],
                                      'reviewer': {'model': 'synthetic'}}))
        self.cli('lesson', 'review', '--batch', started['batch'], '--file', review)
        self.cli('lesson', 'accept', '--batch', started['batch'])
        self.assertEqual(self.sentinel(), [])

        # The sentinels do work: an ordinary Git commit runs them.
        subprocess.run(['git', '-C', str(self.root), 'commit', '--allow-empty', '-qm', 'plain'],
                       env={**os.environ, 'HOME': str(self.base / 'home'), 'XDG_CONFIG_HOME': str(self.base / 'config')},
                       check=True, capture_output=True)
        self.assertIn('pre-commit', self.sentinel())


if __name__ == '__main__':
    unittest.main()
