"""Submodules: a parent never records a child commit that is not present; children are synced first and their
pins are committed in the parents. Remotes are local repositories in the temporary base directory."""
import json
import os
import subprocess

from .support import Fixture, clean_environ, commit_all, document, git


class SyncSubmoduleTests(Fixture):
    def setUp(self):
        super().setUp()
        self.module = self.add_submodule()  # work/module: a child repository, origin = base/module-origin
        self.module_origin = self.base / 'module-origin'
        self.branch = git(self.root, 'symbolic-ref', '--short', 'HEAD')
        self.remote = self.base / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(self.remote)], check=True)
        git(self.root, 'remote', 'add', 'origin', str(self.remote))
        git(self.root, 'push', '-q', 'origin', self.branch)
        self.other = self.base / 'other'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(self.other)], check=True, env=clean_environ())
        git(self.other, 'config', 'user.email', 'synthetic@example.invalid')
        git(self.other, 'config', 'user.name', 'Synthetic fixture')
        git(self.root, 'push', '-q', 'origin', self.branch)

    def new_child_commit(self):
        """A new commit in the child's origin (the child's own remote), returns its id."""
        (self.module_origin / 'child-note.md').write_text(document('child-note', 'work/module', body='Child.'))
        commit_all(self.module_origin)
        return git(self.module_origin, 'rev-parse', 'HEAD')

    def pin_on_remote(self, commit):
        """The root's remote moves: its work/module pin becomes COMMIT (only the gitlink changes there)."""
        git(self.other, 'pull', '-q', 'origin', self.branch)
        git(self.other, 'update-index', '--add', '--cacheinfo', f'160000,{commit},work/module')
        git(self.other, 'commit', '-q', '-m', 'Move the module pin')
        git(self.other, 'push', '-q', 'origin', self.branch)
        return git(self.other, 'rev-parse', 'HEAD')

    def test_a_remote_pin_is_refused_until_the_child_is_synced(self):
        child_head = self.new_child_commit()
        self.pin_on_remote(child_head)
        before = git(self.root, 'rev-parse', 'HEAD')
        self.cli('sync', 'fetch')
        result = json.loads(self.cli('sync', 'pull', ok=False).stdout)
        self.assertEqual(result['status'], 'needs-submodule-sync')
        self.assertIn('work/module', result['problems'][0])
        self.assertIn('children before parents', result['problems'][0])
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), before)

    def test_child_then_parent_keeps_the_parent_free_of_modified_submodules(self):
        child_head = self.new_child_commit()
        remote_head = self.pin_on_remote(child_head)
        self.cli('sync', 'fetch', '--repo', 'work/module')
        child = json.loads(self.cli('sync', 'pull', '--repo', 'work/module').stdout)
        self.assertEqual((child['ok'], child['status']), (True, 'fast-forward'))
        self.assertEqual(git(self.module, 'rev-parse', 'HEAD'), child_head)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')  # The parent committed the new pin.
        self.assertEqual(git(self.root, 'ls-tree', 'HEAD', 'work/module').split()[2], child_head)
        self.assertTrue(git(self.root, 'log', '-1', '--format=%s').startswith('wiki: sync-'))

        self.cli('sync', 'fetch')
        diverged = json.loads(self.cli('sync', 'pull', ok=False).stdout)
        self.assertEqual(diverged['status'], 'diverged')
        merged = json.loads(self.cli('sync', 'pull', '--merge').stdout)
        self.assertEqual(merged['status'], 'merge')
        self.assertEqual(git(self.root, 'ls-tree', 'HEAD', 'work/module').split()[2], child_head)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD^2'), remote_head)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_a_dirty_parent_refuses_the_child_move_before_anything_moves(self):
        child_head = self.new_child_commit()
        self.assertTrue(child_head)
        (self.root / 'stray.txt').write_text('mine\n')
        before = git(self.module, 'rev-parse', 'HEAD')
        self.cli('sync', 'fetch', '--repo', 'work/module')
        p = self.cli('sync', 'pull', '--repo', 'work/module', ok=False)
        self.assertIn('stray.txt', p.stdout + p.stderr)
        self.assertEqual(git(self.module, 'rev-parse', 'HEAD'), before)

    def test_a_removed_or_unregistered_child_is_refused(self):
        child_head = self.new_child_commit()
        git(self.other, 'pull', '-q', 'origin', self.branch)
        git(self.other, 'update-index', '--add', '--cacheinfo', f'160000,{child_head},work/unknown')
        git(self.other, 'commit', '-q', '-m', 'Pin an unregistered child')
        git(self.other, 'push', '-q', 'origin', self.branch)
        self.cli('sync', 'fetch')
        p = self.cli('sync', 'pull', ok=False)
        self.assertIn('unexpected-submodule', p.stderr + p.stdout)

    def test_an_interrupted_child_sync_pins_the_parent_once_when_it_resumes(self):
        from unittest import mock
        from pisar import sync

        class Interrupted(Exception):
            pass

        def stop(point):
            if point == 'pinned':
                raise Interrupted(point)

        child_head = self.new_child_commit()
        self.cli('sync', 'fetch', '--repo', 'work/module')
        before = int(git(self.root, 'rev-list', '--count', 'HEAD'))
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.base / 'noconfig')}), \
                mock.patch('pisar.sync.checkpoint', side_effect=stop):
            with self.assertRaises(Interrupted):
                sync.do_pull(self.root, self.state, relative='work/module')
            result = sync.do_pull(self.root, self.state, relative='work/module')
        self.assertEqual(result['status'], 'fast-forward')
        self.assertEqual(int(git(self.root, 'rev-list', '--count', 'HEAD')), before + 1)
        self.assertEqual(git(self.root, 'ls-tree', 'HEAD', 'work/module').split()[2], child_head)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(json.loads(self.cli('sync', 'status', '--repo', 'work/module').stdout)['open'], [])
