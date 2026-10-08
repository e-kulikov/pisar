"""Execute the README rollback recipe on real tasks and synthetic Git history."""
import hashlib
import json
import subprocess
from .support import CHECKOUT, Fixture, RUWANA, git


class RollbackTests(Fixture):
    def exercise_rollback(self, target, space_id, module=None, task_space=None):
        self.require_ruwana()
        source = self.external()
        value = dict(schema_version=1, operation_id='rollback-one', space_id=space_id,
                     source=dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
                     meeting=dict(id='rollback-call', title='Synthetic rollback call', body='Agreed to prepare a draft.',
                                  related_space_ids=[task_space] if task_space and task_space != space_id else []),
                     tasks=[dict(id='draft', space_id=task_space or space_id, title='Prepare draft', agreed=True)])
        plan = self.base / 'rollback-plan.json'
        plan.write_text(json.dumps(value))
        saved = self.data('--ruwana', RUWANA, 'save', '--plan', plan)
        task = saved['tasks']['draft']
        task_path = self.root / task['path']
        env = {**self.env, 'WIKI_ROOT': str(self.root)}
        done = subprocess.run([str(RUWANA), 'done', task['id'], '--project', task['project']],
                              env=env, text=True, capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        owner = module or self.root
        local_task_path = task_path.relative_to(owner).as_posix()
        git(owner, 'add', '--', local_task_path)
        git(owner, 'commit', '-qm', 'User task status change', '--', local_task_path)
        if module:
            git(self.root, 'add', '--', 'work/module')
            git(self.root, 'commit', '-qm', 'User task gitlink', '--', 'work/module')
        task_bytes = task_path.read_bytes()
        readme = (CHECKOUT / 'README.md').read_text()
        self.assertTrue('## Task-preserving rollback' in readme,
                        'README needs an executable task-preserving rollback procedure')
        section = readme.split('## Task-preserving rollback', 1)[1]
        recipe = section.split('```sh\n', 1)[1].split('```', 1)[0]
        for repo in ([module, self.root] if module else [self.root]):
            rollback = subprocess.run(['bash', '-c', recipe], cwd=self.base,
                                      env={**env, 'OWNER_REPO': str(repo), 'OPERATION': 'rollback-one'},
                                      text=True, capture_output=True)
            self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
            staged = git(repo, 'diff', '--cached', '--name-only').splitlines()
            self.assertFalse(any('.ruwana' in path.split('/') for path in staged))
            if staged:
                git(repo, 'commit', '-qm', 'Roll back knowledge, preserve tasks', '--', *staged)
            if staged and repo == module:
                # Bring the parent clean at the new child HEAD before its own rollback.
                git(self.root, 'add', '--', 'work/module')
                git(self.root, 'commit', '-qm', 'Rollback child gitlink', '--', 'work/module')
        if module:
            self.assertIn(git(module, 'rev-parse', 'HEAD'), git(self.root, 'ls-files', '--stage', '--', 'work/module'))
        self.assertEqual(task_path.read_bytes(), task_bytes)
        tasks = subprocess.run([str(RUWANA), 'list', '--all', '--project', task['project'], '--format', 'json'],
                               env=env, text=True, capture_output=True)
        self.assertEqual(tasks.returncode, 0, tasks.stderr)
        records = json.loads(tasks.stdout)
        self.assertEqual([(r['id'], r['status']) for r in records], [(task['id'], 'done')])
        self.assertFalse((target / 'meetings/rollback-call.md').exists())
        self.assertFalse((target / 'sources/meetings/rollback-call/transcript.md').exists())
        self.assertTrue(self.data('check')['ok'])
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_documented_rollback_keeps_closed_real_task_in_plain_repository(self):
        self.exercise_rollback(self.alpha, 'work/alpha')

    def test_documented_rollback_keeps_closed_real_task_and_updates_submodule_gitlink(self):
        module = self.add_submodule()
        self.exercise_rollback(module, 'work/module', module)

    def test_documented_rollback_root_meeting_and_task_only_child_preserve_gitlink(self):
        module = self.add_submodule()
        self.exercise_rollback(self.alpha, 'work/alpha', module, task_space='work/module')
