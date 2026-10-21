"""Portable provenance gates apply to committed candidate state."""
import hashlib
import subprocess
import unittest
from unittest.mock import patch

from .support import Fixture, RUWANA, RUWANA_AVAILABLE, commit_all, clean_environ, git, init_repo
from pisar.safety import WikiError
from pisar import sync
from pisar.sync import _merge_task_snapshots, _validate_processing


def processed_capture(ident, digest, meeting_id):
    return ('+++\nschema_version = 1\n'
            f'id = "{ident}"\ntype = "source"\ntitle = "Capture"\n'
            'space_ids = ["work/alpha"]\n'
            f'sources = ["sources/captures/{ident}/original"]\n'
            f'source_sha256 = "{digest}"\ningest_status = "processed"\n'
            f'processed_by = "save-{ident}"\n'
            f'meeting_reference = "wiki:work/alpha:{meeting_id}"\n'
            '+++\n\nProcessed source.\n')


def meeting(meeting_id, source):
    return ('+++\nschema_version = 1\n'
            f'id = "{meeting_id}"\ntype = "meeting"\ntitle = "Meeting"\n'
            'space_ids = ["work/alpha"]\n'
            f'sources = ["{source}"]\n'
            '+++\n\nMeeting result.\n')


class SyncPortableTests(Fixture):
    def _task_repo(self):
        repo = self.base / 'task-repo'
        init_repo(repo)
        task_path = repo / '.ruwana/tasks/one.md'
        task_path.parent.mkdir(parents=True)
        task_path.write_text('base task\n')
        commit_all(repo)
        base = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], env=clean_environ(),
                              check=True, text=True, capture_output=True).stdout.strip()
        return repo, task_path, base

    def _commit_task_edit(self, repo, task_path, content):
        task_path.write_text(content)
        commit_all(repo)
        return subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], env=clean_environ(),
                              check=True, text=True, capture_output=True).stdout.strip()

    def _snapshot_stub(self, records):
        from pisar.ruwana import TaskRecord, TaskSnapshot

        def snapshot(adapter, spaces=None):
            head = subprocess.run(['git', '-C', str(adapter.wiki.root), 'rev-parse', 'HEAD'],
                                  env=clean_environ(), check=True, text=True, capture_output=True).stdout.strip()
            state = records[head]
            return TaskSnapshot((TaskRecord('one', 'work/alpha', 'work/alpha',
                                            ('wiki:work/alpha:meeting#task-one',), 'Task', None, None,
                                            state, '.ruwana/tasks/one.md', '0' * 64, 'created', 'modified',
                                            (), (), ()),))
        return snapshot

    def test_sync_accepts_one_sided_task_status_change_after_snapshot_validation(self):
        from pisar.ruwana import Ruwana
        repo, task_path, base = self._task_repo()
        theirs = self._commit_task_edit(repo, task_path, 'done task\n')
        records = {base: 'open', theirs: 'done'}
        with patch.object(Ruwana, 'snapshot', self._snapshot_stub(records)):
            sync._task_changes(repo, base, theirs, theirs)

    def test_sync_refuses_divergent_task_status_changes(self):
        from pisar.ruwana import Ruwana
        repo, task_path, base = self._task_repo()
        ours = self._commit_task_edit(repo, task_path, 'done task\n')
        subprocess.run(['git', '-C', str(repo), 'switch', '-c', 'theirs', base], env=clean_environ(),
                       check=True, capture_output=True)
        theirs = self._commit_task_edit(repo, task_path, 'closed task\n')
        records = {base: 'open', ours: 'done', theirs: 'closed'}
        with patch.object(Ruwana, 'snapshot', self._snapshot_stub(records)):
            with self.assertRaisesRegex(WikiError, 'status'):
                sync._task_changes(repo, ours, theirs, theirs)

    def test_task_snapshot_merge_allows_one_sided_update_and_refuses_divergent_status(self):
        identity = ('work/alpha', 'taskone')
        base_task = dict(id='taskone', space_address='work/alpha', project='work/alpha',
                         source=('wiki:work/alpha:meeting#task-one',), title='Task',
                         description=None, due=None, status='open', path='.ruwana/taskone.toml',
                         created='created', modified='modified', related=(), tags=(), tasks=())
        updated = {**base_task, 'status': 'done'}
        self.assertEqual(_merge_task_snapshots({identity: base_task}, {identity: base_task},
                                               {identity: updated}), {identity: updated})
        other = {**base_task, 'status': 'in-progress'}
        with self.assertRaisesRegex(WikiError, 'status'):
            _merge_task_snapshots({identity: base_task}, {identity: updated}, {identity: other})

    def test_task_snapshot_merge_refuses_divergent_creation_history(self):
        identity = ('work/alpha', 'taskone')
        base = dict(id='taskone', space_address='work/alpha', project='work/alpha',
                    source=('wiki:work/alpha:meeting#task-one',), title='Task', description=None,
                    due=None, status='open', path='.ruwana/taskone.toml', created='2026-01-01',
                    modified='2026-01-01', related=(), tags=(), tasks=())
        incoming = {**base, 'status': 'done', 'modified': '2026-01-02'}
        candidate = {**incoming, 'created': '2000-01-01'}
        expected = _merge_task_snapshots({identity: base}, {identity: base}, {identity: incoming})
        self.assertNotEqual(expected[identity], candidate)

    @unittest.skipUnless(RUWANA_AVAILABLE, 'local Ruwana binary is required')
    def test_same_stable_task_source_under_two_ids_is_refused(self):
        from pisar.ruwana import Ruwana
        from pisar.spaces import Wiki
        adapter = Ruwana(Wiki(self.root), str(RUWANA))
        project = 'work/team/alpha'
        marker = 'wiki:work/alpha:meeting#task-one'
        for title in ('Same task copy one', 'Same task copy two'):
            adapter.run('add', f'--project={project}', f'--source=wiki:work/alpha:meeting',
                        f'--source={marker}', '--', title)
        with self.assertRaisesRegex(WikiError, 'duplicate stable ruwana task source identity'):
            adapter.snapshot(['work/alpha'])

    @unittest.skipUnless(RUWANA_AVAILABLE, 'local Ruwana binary is required')
    def test_clean_merge_of_independent_ids_for_one_task_marker_is_refused(self):
        from pisar.ruwana import Ruwana
        from pisar.spaces import Wiki
        adapter = Ruwana(Wiki(self.root), str(RUWANA))
        base = git(self.root, 'rev-parse', 'HEAD')
        marker = 'wiki:work/alpha:call-one#task-one'
        adapter.run('add', '--project=work/team/alpha', '--source=wiki:work/alpha:call-one',
                    f'--source={marker}', '--', 'Same agreed task')
        commit_all(self.root)
        ours = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'switch', '-c', 'other-task-copy', base)
        adapter = Ruwana(Wiki(self.root), str(RUWANA))
        adapter.run('add', '--project=work/team/alpha', '--source=wiki:work/alpha:call-one',
                    f'--source={marker}', '--', 'Same agreed task')
        commit_all(self.root)
        theirs = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'merge', '--no-ff', '-m', 'merge independent task copies', ours)
        result = git(self.root, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(WikiError, 'duplicate stable ruwana task source identity'):
            sync._task_changes(self.root, ours, theirs, result)

    @unittest.skipUnless(RUWANA_AVAILABLE, 'local Ruwana binary is required')
    def test_candidate_only_task_creation_history_edit_is_refused(self):
        import re
        from pisar.ruwana import Ruwana
        from pisar.spaces import Wiki
        adapter = Ruwana(Wiki(self.root), str(RUWANA))
        task = adapter.ensure(Wiki(self.root).space('work/alpha'),
                              dict(id='one', title='Agreed task', agreed=True),
                              'wiki:work/alpha:call-one')
        commit_all(self.root)
        base = git(self.root, 'rev-parse', 'HEAD')
        adapter.run('done', task['id'], '--project', task['project'])
        commit_all(self.root)
        theirs = git(self.root, 'rev-parse', 'HEAD')
        task_file = self.root / task['path']
        original = task_file.read_text()
        changed, count = re.subn(r'^created = .*$', 'created = "2000-01-01T00:00:00+00:00"',
                                 original, count=1, flags=re.MULTILINE)
        self.assertEqual(count, 1)
        task_file.write_text(changed)
        commit_all(self.root)
        result = git(self.root, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(WikiError, 'coherent three-way result'):
            sync._task_changes(self.root, base, base, result)

    @unittest.skipUnless(RUWANA_AVAILABLE, 'local Ruwana binary is required')
    def test_invalid_intermediate_task_file_is_refused_even_when_restored(self):
        from pisar.ruwana import Ruwana
        from pisar.spaces import Wiki
        adapter = Ruwana(Wiki(self.root), str(RUWANA))
        task = adapter.ensure(Wiki(self.root).space('work/alpha'),
                              dict(id='one', title='Agreed task', agreed=True),
                              'wiki:work/alpha:call-one')
        commit_all(self.root)
        base = git(self.root, 'rev-parse', 'HEAD')
        task_file = self.root / task['path']
        original = task_file.read_bytes()
        task_file.write_bytes(b'not = [valid TOML\n')
        commit_all(self.root)
        task_file.write_bytes(original)
        commit_all(self.root)
        tip = git(self.root, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(WikiError, 'task-validation-unavailable'):
            sync._task_changes(self.root, base, tip, tip)

    @unittest.skipUnless(RUWANA_AVAILABLE, 'local Ruwana binary is required')
    def test_valid_intermediate_task_transition_remains_acceptable(self):
        from pisar.ruwana import Ruwana
        from pisar.spaces import Wiki
        adapter = Ruwana(Wiki(self.root), str(RUWANA))
        task = adapter.ensure(Wiki(self.root).space('work/alpha'),
                              dict(id='one', title='Agreed task', agreed=True),
                              'wiki:work/alpha:call-one')
        commit_all(self.root)
        base = git(self.root, 'rev-parse', 'HEAD')
        adapter.run('done', task['id'], '--project', task['project'])
        commit_all(self.root)
        tip = git(self.root, 'rev-parse', 'HEAD')
        sync._task_changes(self.root, base, tip, tip)

    @unittest.skipUnless(RUWANA_AVAILABLE, 'local Ruwana binary is required')
    def test_space_root_submodule_task_update_uses_full_wiki_context(self):
        from pisar.ruwana import Ruwana
        from pisar.spaces import Wiki
        remote = self.base / 'module-origin'
        init_repo(remote)
        (remote / '.wiki.toml').write_text(
            'schema_version = 1\nid = "module"\nkind = "project"\nstatus = "active"\n')
        commit_all(remote)
        git(self.root, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(remote),
            'work/team/module')
        child = self.root / 'work/team/module'
        git(child, 'config', 'user.email', 'synthetic@example.invalid')
        git(child, 'config', 'user.name', 'Synthetic fixture')
        wiki = Wiki(self.root)
        adapter = Ruwana(wiki, str(RUWANA))
        record = adapter.ensure(wiki.space('work/module'),
                                dict(id='one', title='Agreed task', agreed=True),
                                'wiki:work/module:meeting')
        git(child, 'add', '.ruwana')
        git(child, 'commit', '-qm', 'base task')
        base = git(child, 'rev-parse', 'HEAD')
        git(self.root, 'add', 'work/team/module')
        git(self.root, 'commit', '-qm', 'pin task base')
        adapter.run('done', record['id'], '--project', 'work/team/module')
        git(child, 'add', '.ruwana')
        git(child, 'commit', '-qm', 'complete task')
        tip = git(child, 'rev-parse', 'HEAD')
        sync._task_changes(child, base, tip, tip, wiki_root=self.root, relative='work/team/module')

    def test_candidate_with_two_offline_results_for_one_hash_requires_decision(self):
        source = b'the same source saved independently\r\n'
        digest = hashlib.sha256(source).hexdigest()
        (self.alpha / 'inbox').mkdir()
        for ident, meeting_id in (('capture-one', 'meeting-one'), ('capture-two', 'meeting-two')):
            original = self.alpha / f'sources/captures/{ident}/original'
            original.parent.mkdir(parents=True)
            original.write_bytes(source)
            (self.alpha / f'inbox/{ident}.md').write_text(processed_capture(ident, digest, meeting_id))
            transcript = f'sources/meetings/{meeting_id}/transcript.md'
            (self.alpha / transcript).parent.mkdir(parents=True)
            (self.alpha / transcript).write_bytes(source)
            (self.alpha / 'meetings').mkdir(exist_ok=True)
            (self.alpha / f'meetings/{meeting_id}.md').write_text(meeting(meeting_id, transcript))
        commit_all(self.root)

        with self.assertRaisesRegex(WikiError, 'duplicate-source-decision-required'):
            _validate_processing(self.root)

    def test_same_hash_in_another_domain_does_not_create_a_candidate_duplicate(self):
        source = b'identical bytes remain domain scoped\n'
        digest = hashlib.sha256(source).hexdigest()
        for domain, space, ident in (('work', self.alpha, 'work-meeting'),
                                     ('personal', self.root / 'personal/10-projects/home', 'personal-meeting')):
            transcript = f'sources/meetings/{ident}/transcript.md'
            path = space / transcript
            path.parent.mkdir(parents=True)
            path.write_bytes(source)
            (space / 'meetings').mkdir(exist_ok=True)
            (space / f'meetings/{ident}.md').write_text(
                ('+++\nschema_version = 1\n'
                 f'id = "{ident}"\ntype = "meeting"\ntitle = "Meeting"\n'
                 f'space_ids = ["{domain}/' + ('alpha' if domain == 'work' else 'home') + '"]\n'
                 f'sources = ["{transcript}"]\n+++\n\nMeeting result.\n'))
        commit_all(self.root)
        _validate_processing(self.root)
        self.assertEqual(len(digest), 64)


if __name__ == '__main__':
    unittest.main()
