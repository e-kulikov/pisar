"""Conflicted merges: the report, the dedicated work tree, take, resolve and abort. Real repositories stay untouched
until the resolved result is validated."""
import json
import os
from pathlib import Path
import subprocess
from unittest import mock

from .support import Fixture, clean_environ, commit_all, document, git


class SyncConflictTests(Fixture):
    def setUp(self):
        super().setUp()
        self.remote = self.base / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(self.remote)], check=True)
        self.branch = git(self.root, 'symbolic-ref', '--short', 'HEAD')
        git(self.root, 'remote', 'add', 'origin', str(self.remote))
        git(self.root, 'push', '-q', 'origin', self.branch)
        self.other = self.base / 'other'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(self.other)], check=True, env=clean_environ())
        git(self.other, 'config', 'user.email', 'synthetic@example.invalid')
        git(self.other, 'config', 'user.name', 'Synthetic fixture')
        self.env_home = self.base / 'noconfig'

    def publish(self, change):
        change(self.other)
        commit_all(self.other)
        git(self.other, 'push', '-q', 'origin', self.branch)

    def locally(self, change, message='local change'):
        change(self.root)
        commit_all(self.root)
        git(self.root, 'commit', '-q', '--amend', '-m', message)

    def sync(self, *args, ok=False):
        self.cli('sync', 'fetch')
        return json.loads(self.cli('sync', 'pull', '--merge', *args, ok=ok).stdout)

    def repo_state(self):
        return dict(head=git(self.root, 'rev-parse', 'HEAD'), index=git(self.root, 'ls-files', '--stage'),
                    status=git(self.root, 'status', '--porcelain=v1', '--untracked-files=all'),
                    files={p.relative_to(self.root).as_posix(): p.read_bytes()
                           for p in self.root.rglob('*') if p.is_file() and '.git' not in p.relative_to(self.root).parts})

    def both_edit_call(self):
        self.locally(lambda r: (r / 'work/team/alpha/call.md').write_text(
            document(related=('work/beta',), body='Local wording.')))
        self.publish(lambda r: (r / 'work/team/alpha/call.md').write_text(
            document(related=('work/beta',), body='Remote wording.')))

    def test_a_text_conflict_is_reported_and_the_repository_is_untouched(self):
        self.both_edit_call()
        before = self.repo_state()
        result = self.sync()
        self.assertEqual(result['status'], 'conflicts')
        self.assertFalse(result['ok'])
        self.assertEqual([c['path'] for c in result['conflicts']], ['work/team/alpha/call.md'])
        self.assertEqual(result['conflicts'][0]['kind'], 'content')
        self.assertFalse(result['conflicts'][0]['sources'])
        self.assertEqual(self.repo_state(), before)
        self.assertEqual(len(git(self.root, 'worktree', 'list').splitlines()), 2)
        marked = (Path(result['work']) / 'work/team/alpha/call.md').read_text()
        self.assertIn('<<<<<<<', marked)

    def test_stages_are_in_the_work_tree_index_and_ids_are_reported(self):
        self.both_edit_call()
        result = self.sync()
        work = result['work']
        unmerged = git(work, 'ls-files', '-u')
        self.assertEqual(sorted(line.split()[2] for line in unmerged.splitlines()), ['1', '2', '3'])
        self.assertEqual(result['conflicts'][0]['ours']['id'], 'call-one')
        self.assertEqual(result['conflicts'][0]['theirs']['id'], 'call-one')

    def test_resolve_after_editing_finishes_the_merge_as_a_two_parent_commit(self):
        self.both_edit_call()
        result = self.sync()
        ident = result['sync_id']
        work = result['work']
        path = os.path.join(work, 'work/team/alpha/call.md')
        with open(path, 'w') as stream:
            stream.write(document(related=('work/beta',), body='Both wordings kept.'))
        out = json.loads(self.cli('sync', 'resolve', '--id', ident, '--what', 'Merge remote call wording',
                                  '--why', 'Both edits to the same call are kept by hand.').stdout)
        self.assertEqual(out['status'], 'resolved')
        self.assertEqual(len(git(self.root, 'rev-list', '--parents', '-n', '1', 'HEAD').split()), 3)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertIn('Both wordings kept.', (self.root / 'work/team/alpha/call.md').read_text())
        message = git(self.root, 'log', '-1', '--format=%B')
        self.assertIn(f'wiki: {ident}', message)
        self.assertIn('Merge remote call wording', message)
        self.assertIn('Both edits to the same call are kept by hand.', message)
        self.assertEqual(json.loads(self.cli('sync', 'status').stdout)['open'], [])
        self.assertTrue(json.loads(self.cli('check').stdout)['ok'])

    def test_resolve_refuses_while_markers_remain(self):
        self.both_edit_call()
        result = self.sync()
        before = self.repo_state()
        p = self.cli('sync', 'resolve', '--id', result['sync_id'], ok=False)
        self.assertIn('conflict markers', p.stderr)
        self.assertEqual(self.repo_state(), before)

    def test_take_resolves_one_path_by_taking_a_side(self):
        self.both_edit_call()
        result = self.sync()
        taken = json.loads(self.cli('sync', 'take', '--id', result['sync_id'], '--path', 'work/team/alpha/call.md',
                                    '--side', 'theirs').stdout)
        self.assertEqual(taken['unmerged'], [])
        out = json.loads(self.cli('sync', 'resolve', '--id', result['sync_id']).stdout)
        self.assertEqual(out['status'], 'resolved')
        self.assertIn('Remote wording.', (self.root / 'work/team/alpha/call.md').read_text())

    def test_a_source_is_never_merged_or_edited_only_taken_whole(self):
        def base(repo):
            (repo / 'work/team/alpha/sources').mkdir(parents=True, exist_ok=True)
            (repo / 'work/team/alpha/sources/transcript.txt').write_text('first\nsecond\nthird\n')
        base(self.root)
        commit_all(self.root)
        git(self.root, 'push', '-q', 'origin', self.branch)
        git(self.other, 'pull', '-q', 'origin', self.branch)
        # Non-overlapping line edits: a text merge would succeed; a source must never be merged.
        self.locally(lambda r: (r / 'work/team/alpha/sources/transcript.txt').write_text('LOCAL\nsecond\nthird\n'))
        self.publish(lambda r: (r / 'work/team/alpha/sources/transcript.txt').write_text('first\nsecond\nREMOTE\n'))
        result = self.sync()
        self.assertEqual(result['status'], 'conflicts')
        self.assertTrue(result['conflicts'][0]['sources'])
        self.assertEqual(result['conflicts'][0]['kind'], 'content')
        edited = Path(result['work']) / 'work/team/alpha/sources/transcript.txt'
        edited.write_text('hand edited\n')
        p = self.cli('sync', 'resolve', '--id', result['sync_id'], ok=False)
        self.assertIn('immutable', p.stderr)
        self.cli('sync', 'take', '--id', result['sync_id'], '--path', 'work/team/alpha/sources/transcript.txt',
                 '--side', 'theirs')
        self.assertEqual(json.loads(self.cli('sync', 'resolve', '--id', result['sync_id']).stdout)['status'],
                         'resolved')
        self.assertEqual((self.root / 'work/team/alpha/sources/transcript.txt').read_text(),
                         'first\nsecond\nREMOTE\n')

    def test_delete_modify_is_a_conflict_either_side_can_take(self):
        self.locally(lambda r: (r / 'work/team/alpha/call.md').write_text(document(body='Local edit.')))
        self.publish(lambda r: (r / 'work/team/alpha/call.md').unlink())
        result = self.sync()
        self.assertEqual(result['conflicts'][0]['kind'], 'delete-modify')
        self.cli('sync', 'take', '--id', result['sync_id'], '--path', 'work/team/alpha/call.md', '--side', 'theirs')
        self.assertEqual(json.loads(self.cli('sync', 'resolve', '--id', result['sync_id']).stdout)['status'],
                         'resolved')
        self.assertFalse((self.root / 'work/team/alpha/call.md').exists())

    def test_binary_conflicts_have_no_markers_and_take_a_side(self):
        self.locally(lambda r: (r / 'blob.bin').write_bytes(b'\0ours'))
        self.publish(lambda r: (r / 'blob.bin').write_bytes(b'\0theirs'))
        result = self.sync()
        self.assertEqual(result['conflicts'][0]['kind'], 'binary')
        p = self.cli('sync', 'resolve', '--id', result['sync_id'], ok=False)
        self.assertIn('take a side', p.stderr)  # A conflict without markers needs an explicit decision.
        self.cli('sync', 'take', '--id', result['sync_id'], '--path', 'blob.bin', '--side', 'ours')
        self.assertEqual(json.loads(self.cli('sync', 'resolve', '--id', result['sync_id']).stdout)['status'],
                         'resolved')
        self.assertEqual((self.root / 'blob.bin').read_bytes(), b'\0ours')

    def test_abort_leaves_nothing_behind_and_the_repository_is_untouched(self):
        self.both_edit_call()
        result = self.sync()
        before = self.repo_state()
        out = json.loads(self.cli('sync', 'abort', '--id', result['sync_id']).stdout)
        self.assertEqual(out['status'], 'aborted')
        self.assertEqual(len(git(self.root, 'worktree', 'list').splitlines()), 1)
        self.assertEqual(self.repo_state(), before)
        self.assertEqual(json.loads(self.cli('sync', 'status').stdout)['open'], [])
        again = self.sync()
        self.assertEqual(again['status'], 'conflicts')

    def test_take_refuses_a_path_that_is_not_conflicted(self):
        self.both_edit_call()
        result = self.sync()
        p = self.cli('sync', 'take', '--id', result['sync_id'], '--path', 'work/team/alpha/call.md',
                     '--side', 'ours', ok=True)
        self.assertEqual(json.loads(p.stdout)['status'], 'taken')
        p = self.cli('sync', 'take', '--id', result['sync_id'], '--path', 'personal/home', '--side', 'ours', ok=False)
        self.assertIn('not a conflicted path', p.stderr)

    def test_an_unsafe_conflicted_name_from_the_remote_is_never_written_into_the_work_tree(self):
        from pisar import sync
        self.locally(lambda r: (r / 'work/team/alpha/call.md').write_text(document(body='Local.')))
        self.publish(lambda r: (r / 'work/team/alpha/call.md').write_text(document(body='Remote.')))
        before = self.repo_state()
        self.cli('sync', 'fetch')
        with mock.patch.object(sync, '_path_problem',
                               side_effect=lambda p: 'unsafe path (test)' if p.endswith('call.md') else None), \
                mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.base / 'noconfig')}):
            result = sync.do_pull(self.root, self.state, merge=True)
        self.assertEqual(result['status'], 'invalid')
        self.assertEqual(self.repo_state(), before)
        self.assertEqual(len(git(self.root, 'worktree', 'list').splitlines()), 1)
