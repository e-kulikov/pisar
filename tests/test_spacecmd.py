"""`pisar domain` and `pisar space` commands, on temporary fixtures only."""
import json
from pathlib import Path
import unittest

from pisar.operations import Runtime
from pisar.safety import sha256
from .support import Fixture, commit_all, domain, git, init_repo, space


def commits(repo):
    return int(git(repo, 'rev-list', '--count', 'HEAD'))


class SpaceCommandFixture(Fixture):
    def setUp(self):
        super().setUp()
        # Cloned submodules do not inherit a local identity: use a synthetic global one.
        home = self.base / 'home'
        home.mkdir()
        (home / '.gitconfig').write_text('[user]\n\temail = synthetic@example.invalid\n\tname = Synthetic fixture\n')
        self.env = {**self.env, 'HOME': str(home)}

    def spaces(self, *args):
        return {s['address']: s for s in self.data('spaces', *args)['spaces']}

    def clean(self, repo=None):
        self.assertEqual(git(repo or self.root, 'status', '--porcelain'), '')

    def origin(self, name='origin', marker=None):
        """A bare repository with one commit, usable as a local `--repo`."""
        work = self.base / f'{name}-work'
        init_repo(work)
        (work / 'README.md').write_text('# Seed\n')
        if marker:
            domain(work.parent, work.name, marker)
            (work / '.domain.toml').write_text(
                f'schema_version = 1\nid = "{marker}"\ntitle = "Seed"\n')
        commit_all(work)
        bare = self.base / f'{name}.git'
        git(self.base, 'clone', '-q', '--bare', str(work), str(bare))
        return bare


class DomainTests(SpaceCommandFixture):
    def test_list_reports_id_title_path_and_layout(self):
        found = {d['id']: d for d in self.data('domain', 'list')['domains']}
        self.assertEqual(sorted(found), ['personal', 'work'])
        self.assertEqual(found['work']['title'], 'Work')
        self.assertEqual(found['work']['path'], 'work')
        self.assertEqual(found['work']['layout']['archive'], '40-archives')

    def test_add_creates_marker_scaffold_and_one_commit(self):
        before = commits(self.root)
        result = self.data('domain', 'add', '--id', 'acme', '--title', 'Acme Corp')
        self.assertEqual(result['domain']['id'], 'acme')
        self.assertEqual(commits(self.root), before + 1)
        self.clean()
        for folder in ('10-projects', '20-areas', '30-resources', '40-archives', 'inbox'):
            self.assertTrue((self.root / 'acme' / folder).is_dir(), folder)
        self.assertTrue(self.data('check')['ok'])
        self.assertEqual({d['id']: d['title'] for d in self.data('domain', 'list')['domains']}['acme'],
                         'Acme Corp')

    def test_add_is_idempotent_and_rejects_a_different_title(self):
        self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme Corp')
        head = git(self.root, 'rev-parse', 'HEAD')
        again = self.data('domain', 'add', '--id', 'acme', '--title', 'Acme Corp')
        self.assertFalse(again['changed'])
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        failed = self.cli('domain', 'add', '--id', 'acme', '--title', 'Other', ok=False)
        self.assertIn('acme', failed.stderr)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_layout_none_writes_only_the_marker(self):
        self.cli('domain', 'add', '--id', 'bare', '--title', 'Bare', '--layout', 'none')
        self.assertEqual(sorted(p.name for p in (self.root / 'bare').iterdir()), ['.domain.toml'])
        self.clean()

    def test_invalid_ids_and_titles_are_refused(self):
        for args in (('--id', 'Acme', '--title', 'T'), ('--id', '-x', '--title', 'T'),
                     ('--id', 'a/b', '--title', 'T'), ('--id', 'acme', '--title', '  ')):
            with self.subTest(args):
                self.cli('domain', 'add', *args, ok=False)
        self.clean()

    def test_dirty_tree_refuses(self):
        (self.root / 'stray.txt').write_text('user edit')
        self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme', ok=False)
        self.assertFalse((self.root / 'acme').exists())

    def test_existing_unmarked_directory_gets_only_the_marker(self):
        (self.root / 'acme').mkdir()
        (self.root / 'acme/notes.md').write_text('keep me\n')
        commit_all(self.root)
        self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme')
        self.assertEqual((self.root / 'acme/notes.md').read_text(), 'keep me\n')
        self.assertFalse((self.root / 'acme/10-projects').exists())
        self.clean()

    def test_interrupted_add_commits_the_marker_it_wrote_before_the_crash(self):
        args = ('domain', 'add', '--id', 'acme', '--title', 'Acme', '--layout', 'none')
        self.cli(*args)
        git(self.root, 'reset', '-q', '--hard', 'HEAD~1')
        (self.root / 'acme').mkdir()
        (self.root / 'acme/.domain.toml').write_text('schema_version = 1\nid = "acme"\ntitle = "Acme"\n')
        runtime = Runtime(self.state, self.root)
        for journal in (runtime.path / 'operations').glob('domain-add-*.json'):
            data = json.loads(journal.read_text())
            data['status'] = 'incomplete'
            journal.write_text(json.dumps(data))
        before = commits(self.root)
        self.assertTrue(self.data(*args)['changed'])
        self.assertEqual(commits(self.root), before + 1)
        self.clean()
        self.assertIn('acme/.domain.toml', git(self.root, 'ls-files'))

    def test_add_repo_clones_a_local_repository_as_a_submodule(self):
        bare = self.origin()
        before = commits(self.root)
        self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme', '--repo', bare)
        acme = self.root / 'acme'
        self.assertTrue((acme / '.git').exists())
        self.assertIn('acme', git(self.root, 'submodule', 'status'))
        self.assertIn('id = "acme"', (acme / '.domain.toml').read_text())
        self.assertEqual(git(acme, 'status', '--porcelain'), '')
        self.clean()
        self.assertEqual(commits(self.root), before + 1)
        self.assertTrue((acme / '10-projects').is_dir())
        self.assertEqual(git(acme, 'log', '-1', '--format=%s')[:6], 'wiki: ')
        # Idempotent: nothing changes the second time.
        head, child = git(self.root, 'rev-parse', 'HEAD'), git(acme, 'rev-parse', 'HEAD')
        self.assertFalse(self.data('domain', 'add', '--id', 'acme', '--title', 'Acme', '--repo', bare)['changed'])
        self.assertEqual((git(self.root, 'rev-parse', 'HEAD'), git(acme, 'rev-parse', 'HEAD')), (head, child))
        self.assertTrue(self.data('check')['ok'])

    def half_done_add(self):
        """`domain add --repo` that crashed after the child commit, before the parent commit."""
        bare = self.origin()
        args = ('domain', 'add', '--id', 'acme', '--title', 'Acme', '--repo', bare)
        self.cli(*args)
        git(self.root, 'reset', '-q', '--soft', 'HEAD~1')
        for journal in (Runtime(self.state, self.root).path / 'operations').glob('domain-add-*.json'):
            data = json.loads(journal.read_text())
            data['status'] = 'incomplete'
            journal.write_text(json.dumps(data))
        return args

    def test_resumed_submodule_add_completes_when_nothing_else_changed(self):
        args = self.half_done_add()
        before = commits(self.root)
        self.assertTrue(self.data(*args)['changed'])
        self.assertEqual(commits(self.root), before + 1)
        self.clean()

    def test_resumed_submodule_add_refuses_an_edited_gitmodules(self):
        args = self.half_done_add()
        modules = self.root / '.gitmodules'
        modules.write_text(modules.read_text() + '[submodule "other"]\n\tpath = other\n\turl = ../other\n')
        head = git(self.root, 'rev-parse', 'HEAD')
        self.assertIn('.gitmodules', self.cli(*args, ok=False).stderr)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertIn('other', modules.read_text())

    def test_resumed_submodule_add_refuses_unrelated_commits_in_the_child(self):
        args = self.half_done_add()
        acme = self.root / 'acme'
        git(acme, 'config', 'user.email', 'synthetic@example.invalid')  # clones carry no identity
        git(acme, 'config', 'user.name', 'Synthetic fixture')
        (acme / 'stray.md').write_text('unrelated\n')
        git(acme, 'add', 'stray.md')
        git(acme, 'commit', '-qm', 'unrelated work')
        head = git(self.root, 'rev-parse', 'HEAD')
        self.assertIn('acme', self.cli(*args, ok=False).stderr)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_resumed_submodule_add_refuses_unexpected_files_in_the_child(self):
        args = self.half_done_add()
        (self.root / 'acme/stray.md').write_text('unrelated\n')
        head = git(self.root, 'rev-parse', 'HEAD')
        self.assertIn('stray.md', self.cli(*args, ok=False).stderr)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertTrue((self.root / 'acme/stray.md').exists())

    def test_add_repo_resumes_a_registered_submodule_without_a_marker(self):
        bare = self.origin()
        git(self.root, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(bare), 'acme')
        git(self.root, 'commit', '-qm', 'manual add')
        self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme', '--repo', bare)
        self.assertTrue((self.root / 'acme/.domain.toml').is_file())
        self.clean()

    def test_cloned_marker_must_be_a_regular_file_and_nothing_is_left_behind(self):
        work = self.base / 'linked-work'
        init_repo(work)
        (work / 'real.toml').write_text('schema_version = 1\nid = "acme"\ntitle = "Acme"\n')
        (work / '.domain.toml').symlink_to('real.toml')
        commit_all(work)
        bare = self.base / 'linked.git'
        git(self.base, 'clone', '-q', '--bare', str(work), str(bare))
        head = git(self.root, 'rev-parse', 'HEAD')
        failed = self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme', '--repo', bare, ok=False)
        self.assertIn('symlink', failed.stderr)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertFalse((self.root / 'acme').exists())
        self.assertFalse((self.root / '.git/modules/acme').exists())
        self.clean()
        self.assertTrue(self.data('check')['ok'])

    def test_invalid_clone_cleanup_removes_module_storage_of_a_linked_worktree(self):
        work = self.base / 'linked-work'
        init_repo(work)
        (work / 'real.toml').write_text('schema_version = 1\nid = "acme"\ntitle = "Acme"\n')
        (work / '.domain.toml').symlink_to('real.toml')
        commit_all(work)
        bare = self.base / 'linked.git'
        git(self.base, 'clone', '-q', '--bare', str(work), str(bare))
        tree = self.base / 'second-checkout'
        git(self.root, 'worktree', 'add', '-q', '-b', 'second', str(tree))
        self.assertTrue((tree / '.git').is_file())
        self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme', '--repo', bare, ok=False, root=tree)
        storage = Path(git(tree, 'rev-parse', '--path-format=absolute', '--git-path', 'modules/acme'))
        self.assertFalse(storage.exists(), storage)
        self.assertFalse((tree / 'acme').exists())
        self.clean(tree)

    def test_cleanup_keeps_module_storage_that_existed_before(self):
        work = self.base / 'linked-work'
        init_repo(work)
        (work / 'real.toml').write_text('schema_version = 1\nid = "acme"\ntitle = "Acme"\n')
        (work / '.domain.toml').symlink_to('real.toml')
        commit_all(work)
        bare = self.base / 'linked.git'
        git(self.base, 'clone', '-q', '--bare', str(work), str(bare))
        keep = self.root / '.git/modules/acme'
        keep.mkdir(parents=True)
        (keep / 'precious').write_text('not ours\n')
        self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme', '--repo', bare, ok=False)
        self.assertEqual((keep / 'precious').read_text(), 'not ours\n')

    def test_cloned_marker_with_another_title_is_refused_and_cleaned_up(self):
        bare = self.origin(marker='acme')
        failed = self.cli('domain', 'add', '--id', 'acme', '--title', 'Different', '--repo', bare, ok=False)
        self.assertIn('Seed', failed.stderr)
        self.assertFalse((self.root / 'acme').exists())
        self.clean()

    def test_add_repo_refuses_option_like_locations(self):
        self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme', '--repo', '--upload-pack=x', ok=False)
        self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme', '--repo', 'ext::sh -c id', ok=False)
        self.assertFalse((self.root / 'acme').exists())


class CreateTests(SpaceCommandFixture):
    def create(self, *args, **kwargs):
        return self.data('space', 'create', *args, **kwargs)

    def test_create_derives_the_id_and_writes_marker_readme_and_one_commit(self):
        before = commits(self.root)
        result = self.create('--domain', 'work', '--kind', 'project', '--title', 'Launch Plan')
        self.assertEqual(result['space']['address'], 'work/launch-plan')
        path = self.root / 'work/10-projects/launch-plan'
        self.assertEqual((path / 'README.md').read_text().splitlines()[0], '# Launch Plan')
        self.assertIn('kind = "project"', (path / '.wiki.toml').read_text())
        self.assertEqual(commits(self.root), before + 1)
        self.assertEqual(sorted(git(self.root, 'show', '--name-only', '--format=', 'HEAD').splitlines()),
                         ['work/10-projects/launch-plan/.wiki.toml', 'work/10-projects/launch-plan/README.md'])
        self.clean()
        self.assertIn('work/launch-plan', self.spaces())
        self.assertTrue(self.data('check')['ok'])

    def test_kind_decides_the_folder_but_never_enters_the_id(self):
        result = self.create('--domain', 'work', '--kind', 'area', '--title', 'Ops Runbooks')
        self.assertEqual(result['space']['address'], 'work/ops-runbooks')
        self.assertTrue((self.root / 'work/20-areas/ops-runbooks').is_dir())

    def test_title_without_usable_id_requires_an_explicit_id(self):
        failed = self.cli('space', 'create', '--domain', 'work', '--kind', 'area',
                          '--title', 'Проект', ok=False)
        self.assertIn('--id', failed.stderr)
        result = self.create('--domain', 'work', '--kind', 'area', '--title', 'Проект', '--id', 'project-ru')
        self.assertEqual(result['space']['address'], 'work/project-ru')
        self.assertEqual((self.root / 'work/20-areas/project-ru/README.md').read_text().splitlines()[0],
                         '# Проект')

    def test_min_segments_is_enforced(self):
        domain(self.root, 'acme', extra='[ids]\nmin_segments = 2\n')
        commit_all(self.root)
        failed = self.cli('space', 'create', '--domain', 'acme', '--kind', 'area', '--title', 'AI', ok=False)
        self.assertIn('2', failed.stderr)
        self.assertFalse((self.root / 'acme/20-areas').exists())
        self.create('--domain', 'acme', '--kind', 'area', '--title', 'AI', '--id', 'ai-research')
        self.cli('space', 'create', '--domain', 'acme', '--kind', 'area', '--title', 'X', '--id', 'x', ok=False)

    def test_conflict_names_the_owner_including_archived_and_suggests_alternatives(self):
        space(self.root, 'work/40-archives/ai', 'ai', kind='area', status='archived')
        commit_all(self.root)
        failed = self.cli('space', 'create', '--domain', 'work', '--kind', 'project', '--title', 'AI', ok=False)
        self.assertIn('ai: area, archived', failed.stderr)
        self.assertIn('ai-2', failed.stderr)
        self.assertFalse((self.root / 'work/10-projects/ai').exists())

    def test_same_id_may_exist_in_another_domain(self):
        self.create('--domain', 'work', '--kind', 'project', '--title', 'Home')
        self.assertEqual({'work/home', 'personal/home'} <= set(self.spaces()), True)

    def test_retry_with_the_same_inputs_changes_nothing(self):
        args = ('--domain', 'work', '--kind', 'project', '--title', 'Launch Plan')
        self.create(*args)
        head = git(self.root, 'rev-parse', 'HEAD')
        again = self.create(*args)
        self.assertFalse(again['changed'])
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_dirty_tree_and_unknown_domain_are_refused(self):
        (self.alpha / 'draft.md').write_text('user edit')
        self.cli('space', 'create', '--domain', 'work', '--kind', 'project', '--title', 'Launch Plan', ok=False)
        self.assertFalse((self.root / 'work/10-projects/launch-plan').exists())
        (self.alpha / 'draft.md').unlink()
        self.cli('space', 'create', '--domain', 'nope', '--kind', 'project', '--title', 'Launch Plan', ok=False)

    def test_interrupted_create_resumes_without_duplicates(self):
        args = ('--domain', 'work', '--kind', 'project', '--title', 'Launch Plan')
        self.create(*args)
        # Simulate a crash after the files were written but before the commit.
        git(self.root, 'reset', '-q', '--hard', 'HEAD~1')
        runtime = Runtime(self.state, self.root)
        for journal in (runtime.path / 'operations').glob('space-create-*.json'):
            data = json.loads(journal.read_text())
            data['status'] = 'incomplete'
            journal.write_text(json.dumps(data))
        path = self.root / 'work/10-projects/launch-plan'
        path.mkdir(parents=True)
        (path / 'README.md').write_text('# Launch Plan\n')
        again = self.create(*args)
        self.assertEqual(again['space']['address'], 'work/launch-plan')
        self.clean()
        self.assertTrue((path / '.wiki.toml').is_file())

    def interrupt(self, pattern):
        """Mark the finished operation's journal incomplete, as a crash before the commit leaves it."""
        for journal in (Runtime(self.state, self.root).path / 'operations').glob(pattern):
            data = json.loads(journal.read_text())
            data['status'] = 'incomplete'
            journal.write_text(json.dumps(data))

    def test_resumed_create_refuses_a_readme_edited_after_the_crash(self):
        args = ('--domain', 'work', '--kind', 'project', '--title', 'Launch Plan')
        self.create(*args)
        git(self.root, 'reset', '-q', '--hard', 'HEAD~1')
        path = self.root / 'work/10-projects/launch-plan'
        path.mkdir(parents=True)
        (path / 'README.md').write_text('# Launch Plan\n\nMy own notes.\n')
        self.interrupt('space-create-*.json')
        head = git(self.root, 'rev-parse', 'HEAD')
        failed = self.cli('space', 'create', *args, ok=False)
        self.assertIn('README.md', failed.stderr)
        self.assertEqual((path / 'README.md').read_text(), '# Launch Plan\n\nMy own notes.\n')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_resumed_create_refuses_unexpected_files_and_foreign_commits(self):
        args = ('--domain', 'work', '--kind', 'project', '--title', 'Launch Plan')
        self.create(*args)
        git(self.root, 'reset', '-q', '--hard', 'HEAD~1')
        path = self.root / 'work/10-projects/launch-plan'
        path.mkdir(parents=True)
        (path / 'draft.md').write_text('mine\n')
        self.interrupt('space-create-*.json')
        self.assertIn('draft.md', self.cli('space', 'create', *args, ok=False).stderr)
        (path / 'draft.md').unlink()
        (self.root / 'other.txt').write_text('unrelated commit\n')
        commit_all(self.root)
        self.assertIn('HEAD', self.cli('space', 'create', *args, ok=False).stderr)
        self.assertFalse((path / '.wiki.toml').exists())

    def test_create_inside_a_submodule_domain_commits_child_then_parent(self):
        bare = self.origin()
        self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme', '--repo', bare)
        parent, child = commits(self.root), commits(self.root / 'acme')
        self.create('--domain', 'acme', '--kind', 'resource', '--title', 'Style Guide')
        self.assertEqual((commits(self.root), commits(self.root / 'acme')), (parent + 1, child + 1))
        self.clean()
        self.clean(self.root / 'acme')
        self.assertIn('acme/style-guide', self.spaces())


class FindTests(SpaceCommandFixture):
    def find(self, text, *args):
        return [s['address'] for s in self.data('space', 'find', text, *args)['spaces']]

    def test_finds_by_id_title_and_alias_case_insensitively(self):
        self.data('space', 'create', '--domain', 'work', '--kind', 'project', '--title', 'Moon Landing')
        (self.beta / 'README.md').write_text('# Beta Programme\n')
        (self.beta / '.wiki.toml').write_text(
            'schema_version = 1\nid = "beta"\nkind = "project"\nstatus = "active"\naliases = ["Second Try"]\n')
        commit_all(self.root)
        self.assertEqual(self.find('MOON'), ['work/moon-landing'])
        self.assertEqual(self.find('programme'), ['work/beta'])
        self.assertEqual(self.find('second try'), ['work/beta'])
        self.assertEqual(self.find('alpha'), ['work/alpha'])
        self.assertEqual(self.find('zzz-nothing'), [])

    def test_find_honours_include_and_exclude_and_reports_status(self):
        self.assertEqual(self.find('home'), ['personal/home'])
        self.assertEqual(self.find('home', '--include', 'work'), [])
        self.assertEqual(self.find('a', '--exclude', 'personal', '--include', 'work'),
                         ['work/alpha', 'work/beta'])
        found = self.data('space', 'find', 'home')['spaces'][0]
        self.assertEqual((found['kind'], found['status']), ('project', 'active'))


class RelocateTests(SpaceCommandFixture):
    def setUp(self):
        super().setUp()
        self.data('space', 'create', '--domain', 'work', '--kind', 'project', '--title', 'Launch Plan')
        (self.root / 'work/10-projects/launch-plan/notes.md').write_text('notes\n')
        commit_all(self.root)

    def block(self, ident, **fields):
        runtime = Runtime(self.state, self.root)
        path = runtime.path / 'operations'
        path.mkdir(parents=True, exist_ok=True)
        (path / f'{ident}.json').write_text(json.dumps(dict(
            schema_version=1, root=str(self.root), operation_id=ident, status='incomplete', **fields)))

    def test_move_changes_kind_and_directory_in_one_commit(self):
        before = commits(self.root)
        result = self.data('space', 'move', 'work/launch-plan', '--to', 'area')
        self.assertEqual(result['space']['kind'], 'area')
        old, new = self.root / 'work/10-projects/launch-plan', self.root / 'work/20-areas/launch-plan'
        self.assertFalse(old.exists())
        self.assertEqual((new / 'notes.md').read_text(), 'notes\n')
        self.assertIn('kind = "area"', (new / '.wiki.toml').read_text())
        self.assertEqual(commits(self.root), before + 1)
        self.clean()
        self.assertEqual(self.spaces()['work/launch-plan']['kind'], 'area')
        self.assertTrue(self.data('check')['ok'])

    def test_move_to_the_same_kind_is_a_reported_no_op(self):
        head = git(self.root, 'rev-parse', 'HEAD')
        result = self.data('space', 'move', 'work/launch-plan', '--to', 'project')
        self.assertFalse(result['changed'])
        self.assertIn('already', result['message'])
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_move_refuses_dirty_tree_existing_target_and_unknown_inputs(self):
        (self.root / 'work/10-projects/launch-plan/notes.md').write_text('edited\n')
        self.cli('space', 'move', 'work/launch-plan', '--to', 'area', ok=False)
        self.assertTrue((self.root / 'work/10-projects/launch-plan').is_dir())
        git(self.root, 'checkout', '--', '.')
        (self.root / 'work/20-areas/launch-plan').mkdir(parents=True)
        (self.root / 'work/20-areas/launch-plan/keep.md').write_text('squatter\n')
        commit_all(self.root)
        failed = self.cli('space', 'move', 'work/launch-plan', '--to', 'area', ok=False)
        self.assertIn('exists', failed.stderr)
        self.cli('space', 'move', 'work/nope', '--to', 'area', ok=False)
        self.cli('space', 'move', 'launch-plan', '--to', 'area', ok=False)
        self.cli('space', 'move', 'work/launch-plan', '--to', 'archive', ok=False)

    def test_move_rewrites_single_quoted_markers(self):
        marker = self.root / 'work/10-projects/launch-plan/.wiki.toml'
        marker.write_text("schema_version = 1\nid = 'launch-plan'\nkind = 'project' # kept\nstatus = 'active'\n")
        commit_all(self.root)
        self.data('space', 'move', 'work/launch-plan', '--to', 'area')
        text = (self.root / 'work/20-areas/launch-plan/.wiki.toml').read_text()
        self.assertIn('# kept', text)
        self.assertEqual(self.spaces()['work/launch-plan']['kind'], 'area')
        self.clean()

    def test_unrewritable_marker_is_refused_before_anything_moves(self):
        marker = self.root / 'work/10-projects/launch-plan/.wiki.toml'
        marker.write_text('schema_version = 1\nid = "launch-plan"\nkind = """project"""\nstatus = "active"\n')
        commit_all(self.root)
        head = git(self.root, 'rev-parse', 'HEAD')
        self.cli('space', 'move', 'work/launch-plan', '--to', 'area', ok=False)
        self.assertTrue((self.root / 'work/10-projects/launch-plan').is_dir())
        self.assertFalse((self.root / 'work/20-areas/launch-plan').exists())
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.clean()

    def test_space_containing_a_submodule_is_refused_before_anything_moves(self):
        bare = self.origin()
        git(self.root, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(bare),
            'work/10-projects/launch-plan/vendor')
        git(self.root, 'commit', '-qm', 'vendor')
        head = git(self.root, 'rev-parse', 'HEAD')
        for args in (('move', 'work/launch-plan', '--to', 'area'), ('archive', 'work/launch-plan')):
            failed = self.cli('space', *args, ok=False)
            self.assertIn('submodule', failed.stderr)
        self.assertTrue((self.root / 'work/10-projects/launch-plan').is_dir())
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.clean()

    def test_move_refuses_while_an_incomplete_operation_touches_the_space(self):
        self.block('save-1', artifacts=[dict(path='work/10-projects/launch-plan/meetings/a.md')])
        failed = self.cli('space', 'move', 'work/launch-plan', '--to', 'area', ok=False)
        self.assertIn('save-1', failed.stderr)
        self.assertTrue((self.root / 'work/10-projects/launch-plan').is_dir())
        self.cli('space', 'archive', 'work/launch-plan', ok=False)

    def test_an_incomplete_operation_elsewhere_does_not_block(self):
        self.block('save-2', artifacts=[dict(path='work/team/alpha/meetings/a.md')])
        self.data('space', 'move', 'work/launch-plan', '--to', 'area')

    def test_archive_and_restore_round_trip_keeps_kind_and_blocks_id_reuse(self):
        self.data('space', 'move', 'work/launch-plan', '--to', 'resource')
        archived = self.data('space', 'archive', 'work/launch-plan')
        self.assertEqual((archived['space']['status'], archived['space']['kind']), ('archived', 'resource'))
        self.assertEqual(self.spaces()['work/launch-plan']['path'], 'work/40-archives/launch-plan')
        self.clean()
        self.assertFalse(self.data('space', 'archive', 'work/launch-plan')['changed'])
        failed = self.cli('space', 'create', '--domain', 'work', '--kind', 'area',
                          '--title', 'Launch Plan', ok=False)
        self.assertIn('resource, archived', failed.stderr)
        self.cli('space', 'move', 'work/launch-plan', '--to', 'area', ok=False)
        restored = self.data('space', 'restore', 'work/launch-plan')
        self.assertEqual((restored['space']['status'], restored['space']['kind']), ('active', 'resource'))
        self.assertEqual(self.spaces()['work/launch-plan']['path'], 'work/30-resources/launch-plan')
        self.assertFalse(self.data('space', 'restore', 'work/launch-plan')['changed'])
        self.clean()
        self.assertTrue(self.data('check')['ok'])

    def test_resume_after_an_interrupted_move(self):
        self.data('space', 'move', 'work/launch-plan', '--to', 'area')
        # Rewind, then leave the half-done state a crash would: renamed, marker edited, no commit.
        git(self.root, 'reset', '-q', '--hard', 'HEAD~1')
        (self.root / 'work/20-areas').mkdir(exist_ok=True)
        git(self.root, 'mv', 'work/10-projects/launch-plan', 'work/20-areas/launch-plan')
        marker = self.root / 'work/20-areas/launch-plan/.wiki.toml'
        marker.write_text(marker.read_text().replace('"project"', '"area"'))
        runtime = Runtime(self.state, self.root)
        for journal in (runtime.path / 'operations').glob('space-move-*.json'):
            data = json.loads(journal.read_text())
            data['status'] = 'incomplete'
            journal.write_text(json.dumps(data))
        before = commits(self.root)
        self.data('space', 'move', 'work/launch-plan', '--to', 'area')
        self.assertEqual(commits(self.root), before + 1)
        self.clean()
        self.assertEqual(self.spaces()['work/launch-plan']['kind'], 'area')

    def test_resumed_move_refuses_a_draft_added_after_the_crash(self):
        self.data('space', 'move', 'work/launch-plan', '--to', 'area')
        git(self.root, 'reset', '-q', '--hard', 'HEAD~1')
        (self.root / 'work/20-areas').mkdir(exist_ok=True)
        git(self.root, 'mv', 'work/10-projects/launch-plan', 'work/20-areas/launch-plan')
        marker = self.root / 'work/20-areas/launch-plan/.wiki.toml'
        marker.write_text(marker.read_text().replace('"project"', '"area"'))
        draft = self.root / 'work/20-areas/launch-plan/draft.md'
        draft.write_text('my draft\n')
        for journal in (Runtime(self.state, self.root).path / 'operations').glob('space-move-*.json'):
            data = json.loads(journal.read_text())
            data['status'] = 'incomplete'
            journal.write_text(json.dumps(data))
        head = git(self.root, 'rev-parse', 'HEAD')
        failed = self.cli('space', 'move', 'work/launch-plan', '--to', 'area', ok=False)
        self.assertIn('draft.md', failed.stderr)
        self.assertEqual(draft.read_text(), 'my draft\n')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertNotIn('draft.md', git(self.root, 'ls-files'))

    def test_resumed_move_refuses_a_directory_symlink_added_after_the_crash(self):
        self.data('space', 'move', 'work/launch-plan', '--to', 'area')
        git(self.root, 'reset', '-q', '--hard', 'HEAD~1')
        (self.root / 'work/20-areas').mkdir(exist_ok=True)
        git(self.root, 'mv', 'work/10-projects/launch-plan', 'work/20-areas/launch-plan')
        marker = self.root / 'work/20-areas/launch-plan/.wiki.toml'
        marker.write_text(marker.read_text().replace('"project"', '"area"'))
        draft = self.root / 'work/20-areas/launch-plan/elsewhere'
        draft.symlink_to(self.base)
        for journal in (Runtime(self.state, self.root).path / 'operations').glob('space-move-*.json'):
            data = json.loads(journal.read_text())
            data['status'] = 'incomplete'
            journal.write_text(json.dumps(data))
        head = git(self.root, 'rev-parse', 'HEAD')
        failed = self.cli('space', 'move', 'work/launch-plan', '--to', 'area', ok=False)
        self.assertIn('elsewhere', failed.stderr)
        self.assertTrue(draft.is_symlink())
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertNotIn('elsewhere', git(self.root, 'ls-files'))

    def test_move_inside_a_submodule_domain_commits_child_then_parent(self):
        bare = self.origin()
        self.cli('domain', 'add', '--id', 'acme', '--title', 'Acme', '--repo', bare)
        self.data('space', 'create', '--domain', 'acme', '--kind', 'project', '--title', 'Big Bang')
        parent, child = commits(self.root), commits(self.root / 'acme')
        self.data('space', 'move', 'acme/big-bang', '--to', 'area')
        self.assertEqual((commits(self.root), commits(self.root / 'acme')), (parent + 1, child + 1))
        self.clean()
        self.clean(self.root / 'acme')
        self.assertTrue((self.root / 'acme/20-areas/big-bang/.wiki.toml').is_file())

    def test_move_leaves_other_domains_untouched(self):
        digest = sha256((self.personal / '.wiki.toml').read_bytes())
        self.data('space', 'move', 'work/launch-plan', '--to', 'area')
        self.assertEqual(sha256((self.personal / '.wiki.toml').read_bytes()), digest)


if __name__ == '__main__':
    unittest.main()
