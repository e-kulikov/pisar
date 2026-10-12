"""No sync ever runs a program that a repository's own configuration or attribute file names (review 20b, A and B).

A recursive `git status` would read every submodule's own attribute file and configuration, so sync never runs one:
each repository it inspects is guarded first, and inspected with submodules ignored. A marker file written by a
recording filter must never exist, whichever repository carries the filter."""
from pathlib import Path

from . import sync_faults
from .sync_faults import RemoteFixture
from .support import commit_all, document, git


def git_file(repo, name):
    """The path of a Git administrative file of REPO (a submodule's .git is a file, not a directory)."""
    path = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-path', name).strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class NestedGuardTests(RemoteFixture):
    def setUp(self):
        super().setUp()
        self.new_world('nested')
        self.child = self.add_submodule()
        git(self.root, 'push', '-q', 'origin', self.branch)  # the parent's history includes the submodule
        self.marker = self.base / 'filter-ran'
        self.filter_program = f'touch {self.marker}; cat'

    def test_a_child_with_an_attributed_filter_is_never_run_during_a_parent_pull(self):
        git(self.child, 'config', 'filter.evil.clean', self.filter_program)
        git(self.child, 'config', 'filter.evil.smudge', self.filter_program)
        git_file(self.child, 'info/attributes').write_text('*.md filter=evil\n')
        (self.child / 'note.md').write_bytes((self.child / 'note.md').read_bytes())  # stat cache invalidated
        self.publish(lambda repo: (repo / 'work/team/alpha/new.md').write_text(document('new', 'work/alpha')))
        self.start()
        result = sync_faults.pull_json(self)
        self.assertEqual(result['status'], 'fast-forward')
        self.assertFalse(self.marker.exists(), 'a submodule filter ran during a parent pull')

    def test_a_parent_with_an_attributed_filter_refuses_a_child_pull_before_anything_runs(self):
        git(self.root, 'config', 'filter.evil.clean', self.filter_program)
        git_file(self.root, 'info/attributes').write_text('* filter=evil\n')
        origin = self.base / 'module-origin'
        (origin / 'child-new.md').write_text(document('child-new', 'work/module', body='Child.'))
        commit_all(origin)
        self.start(relative='work/module')
        before = self.snapshot(status=False)
        p = self.cli('sync', 'pull', '--repo', 'work/module', ok=False)
        self.assertTrue(p.stderr.startswith('pisar: '), p.stderr)
        self.assertIn(str(self.root), p.stderr)  # the repository that failed the guard is named
        self.assertFalse(self.marker.exists(), 'a parent filter ran during a child pull')
        self.assertEqual(self.snapshot(status=False), before)

    def test_a_parent_filter_in_its_configuration_refuses_a_child_pull_too(self):
        git(self.root, 'config', 'filter.evil.clean', self.filter_program)
        origin = self.base / 'module-origin'
        (origin / 'child-new.md').write_text(document('child-new', 'work/module', body='Child.'))
        commit_all(origin)
        self.start(relative='work/module')
        p = self.cli('sync', 'pull', '--repo', 'work/module', ok=False)
        self.assertIn('filter.evil.clean', p.stderr)
        self.assertFalse(self.marker.exists())
