"""Installing the plan is revalidated path by path (review 20b, C and D).

C: a user's edit made while the install loop runs (of a later file, or of a later index entry) is never overwritten:
each path is checked again just before it is replaced; the install stops, the edit is kept, and the journal stays
resumable. D: a file's bytes and mode are published together, so a crash right after the publication resumes."""
import os
from unittest import mock

from pisar import sync
from .sync_faults import Faults, Interrupted, RemoteFixture, run_interrupted
from .support import commit_all, git


def hook_once(point, action):
    """A checkpoint hook that runs ACTION the first time POINT is reached, then behaves normally."""
    done = []

    def stop(name):
        if name == point and not done:
            done.append(name)
            action()
    return stop


class InterleavingTests(RemoteFixture):
    def setUp(self):
        super().setUp()
        self.new_world('interleave')
        for name, text in (('a.txt', 'old a\n'), ('z.txt', 'old z\n')):
            (self.root / name).write_text(text)
        commit_all(self.root)
        git(self.root, 'push', '-q', 'origin', self.branch)
        git(self.other, 'pull', '-q', 'origin', self.branch)
        self.publish(lambda repo: ((repo / 'a.txt').write_text('new a\n'), (repo / 'z.txt').write_text('new z\n')))
        self.start()

    def test_a_user_edit_of_a_later_working_file_during_the_install_is_preserved(self):
        def edit():
            (self.root / 'z.txt').write_text('USER EDIT DURING SYNC\n')
        with mock.patch('pisar.sync.checkpoint', side_effect=hook_once('file-written', edit)):
            with self.assertRaisesRegex(Exception, 'z.txt'):
                sync.do_pull(self.root, self.state)
        self.assertEqual((self.root / 'z.txt').read_text(), 'USER EDIT DURING SYNC\n')
        self.assertEqual((self.root / 'a.txt').read_text(), 'new a\n')  # the install before the edit is kept
        journal = next(iter(self.journals().values()))
        self.assertIn(b'"status": "open"', journal)  # still resumable, not complete

    def test_a_user_edit_of_a_later_index_entry_during_the_install_is_preserved(self):
        def stage():
            path = self.root / 'user-blob.txt'
            path.write_text('USER INDEX ENTRY\n')
            git(self.root, 'update-index', '--add', '--cacheinfo', '100644,' +
                git(self.root, 'hash-object', '-w', 'user-blob.txt') + ',z.txt')
            path.unlink()
        with mock.patch('pisar.sync.checkpoint', side_effect=hook_once('index-set', stage)):
            with self.assertRaisesRegex(Exception, 'z.txt'):
                sync.do_pull(self.root, self.state)
        self.assertEqual(git(self.root, 'show', ':z.txt'), 'USER INDEX ENTRY')
        self.assertEqual((self.root / 'z.txt').read_text(), 'old z\n')


class ExecutableTests(RemoteFixture):
    def publish_tool(self, text):
        def change(repo):
            (repo / 'tool.txt').write_text(text)
            (repo / 'tool.txt').chmod(0o755)
        return self.publish(change)

    def interrupt_after_publication(self, name):
        """Stop right after the replacement of NAME is published, before anything else runs (the crash window)."""
        real_replace = os.replace

        def replace(source, target, *args, **kwargs):
            real_replace(source, target, *args, **kwargs)
            if str(target).endswith(name):
                raise Interrupted(f'after publishing {name}')
        return mock.patch('os.replace', side_effect=replace)

    def test_a_crash_right_after_an_added_executable_is_published_resumes(self):
        self.new_world('exec-added')
        self.publish_tool('#!/bin/sh\necho new\n')
        self.start()
        with self.interrupt_after_publication('tool.txt'):
            self.assertTrue(run_interrupted(sync.do_pull, self.root, self.state))
        self.assertEqual(os.stat(self.root / 'tool.txt').st_mode & 0o777, 0o755)
        result = sync.do_pull(self.root, self.state)
        self.assertEqual(result['status'], 'fast-forward')
        self.assertEqual(os.stat(self.root / 'tool.txt').st_mode & 0o777, 0o755)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_a_crash_right_after_a_modified_executable_is_published_resumes(self):
        self.new_world('exec-modified')
        (self.root / 'tool.txt').write_text('#!/bin/sh\necho old\n')
        (self.root / 'tool.txt').chmod(0o755)
        commit_all(self.root)
        git(self.root, 'push', '-q', 'origin', self.branch)
        git(self.other, 'pull', '-q', 'origin', self.branch)
        self.publish_tool('#!/bin/sh\necho new\n')
        self.start()
        with self.interrupt_after_publication('tool.txt'):
            self.assertTrue(run_interrupted(sync.do_pull, self.root, self.state))
        self.assertEqual(sync.do_pull(self.root, self.state)['status'], 'fast-forward')
        self.assertEqual(os.stat(self.root / 'tool.txt').st_mode & 0o777, 0o755)

    def test_a_user_mode_change_after_the_install_is_refused(self):
        self.new_world('exec-user-mode')
        self.publish_tool('#!/bin/sh\necho new\n')
        self.start()
        with Faults('index-set'):
            self.assertTrue(run_interrupted(sync.do_pull, self.root, self.state))
        (self.root / 'tool.txt').chmod(0o644)
        with self.assertRaisesRegex(Exception, 'tool.txt'):
            sync.do_pull(self.root, self.state)
        self.assertEqual(os.stat(self.root / 'tool.txt').st_mode & 0o777, 0o644)
