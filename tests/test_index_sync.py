"""An interruption between update-ref and the index synchronization is repaired by the retry."""
import json
import unittest
from unittest import mock

from pisar import gitops, lesson, spacecmd
from pisar.safety import WikiError
from pisar.spaces import Wiki
from .support import git
from .test_lesson import LessonCase
from .test_spacecmd import SpaceCommandFixture


def die_at_reset(real):
    """Run normally, but stop at the first `git reset`: HEAD already advanced, the index not yet synced."""
    state = {'fired': False}

    def run(repo, *args, **kwargs):
        if args and args[0] == 'reset' and not state['fired']:
            state['fired'] = True
            raise WikiError('interrupted before the index synchronization')
        return real(repo, *args, **kwargs)
    return run


class MoveIndexSync(SpaceCommandFixture):
    def interrupted(self, *args):
        with mock.patch.object(gitops, 'run', die_at_reset(gitops.run)), self.assertRaises(WikiError):
            spacecmd.relocate(Wiki(self.root), self.state, *args)

    def marker_kind(self, path):
        return git(self.root, 'show', f'HEAD:{path}/.wiki.toml').split('kind = "')[1].split('"')[0]

    def test_a_move_interrupted_before_the_index_sync_is_repaired_by_the_retry(self):
        self.interrupted('move', 'work/alpha', 'area')
        self.assertIn('MM', git(self.root, 'status', '--porcelain'))  # the reproduced state
        self.cli('space', 'move', 'work/alpha', '--to', 'area')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(self.marker_kind('work/20-areas/alpha'), 'area')
        self.cli('space', 'archive', 'work/alpha')  # used to fail on the dirty index
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_archive_and_restore_interrupted_before_the_index_sync_are_repaired(self):
        self.interrupted('archive', 'work/alpha')
        self.cli('space', 'archive', 'work/alpha')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.interrupted('restore', 'work/alpha')
        self.cli('space', 'restore', 'work/alpha')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(self.spaces()['work/alpha']['status'], 'active')

    def test_the_retry_keeps_unrelated_staged_and_unstaged_changes(self):
        self.interrupted('move', 'work/alpha', 'area')
        (self.root / 'personal/10-projects/home/staged.txt').write_text('mine\n')
        git(self.root, 'add', '--', 'personal/10-projects/home/staged.txt')
        (self.root / 'personal/10-projects/home/note.md').write_text('edited, unstaged\n')
        # The unrelated changes make the tree dirty, so the operation refuses; the staged state is untouched.
        self.cli('space', 'move', 'work/alpha', '--to', 'area', ok=False)
        self.assertIn('personal/10-projects/home/staged.txt', git(self.root, 'diff', '--cached', '--name-only'))
        self.assertEqual(git(self.root, 'diff', '--name-only', '--', 'personal'),
                         'personal/10-projects/home/note.md')


class AcceptIndexSync(LessonCase):
    def test_accept_interrupted_before_the_index_sync_is_repaired_by_the_retry(self):
        batch, _, _ = self.prepared()
        with mock.patch.object(gitops, 'run', die_at_reset(gitops.run)), self.assertRaises(WikiError):
            lesson.accept(Wiki(self.root), self.state, batch)
        self.assertNotEqual(git(self.root, 'status', '--porcelain'), '')
        result = json.loads(self.cli('lesson', 'accept', '--batch', batch).stdout)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertTrue(self.note(result).is_file())
        self.assertTrue(self.data('check')['ok'])


if __name__ == '__main__':
    unittest.main()
