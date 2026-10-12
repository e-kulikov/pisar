"""Unit checks of the path and entry guards that protect the work tree and the repository from a remote."""
from pathlib import Path
import tempfile
import unittest

from pisar import sync
from pisar.safety import WikiError


class PathGuardTests(unittest.TestCase):
    def test_unsafe_names_are_reported_for_every_form_a_remote_can_send(self):
        for name in ('..', 'a/../b', '.git', 'a/.git/config', 'a/.GIT/x', '.git~1', 'a\\b', '/abs', 'a//b', '',
                     'a/./b', 'bad\0name'):
            with self.subTest(name=name):
                self.assertTrue(sync._path_problem(name), name)
        for name in ('work/team/alpha/call.md', 'a.gitignore', 'x.git', 'dot.d/y.md'):
            with self.subTest(name=name):
                self.assertIsNone(sync._path_problem(name), name)

    def test_a_merge_refuses_unsafe_clean_entries_and_symbolic_links_before_anything_is_written(self):
        problems = sync._unsafe_entries({'a/../escape.txt': ('100644', 'x'), 'link.md': ('120000', 'y'),
                                         'ok.md': ('100644', 'z')}, [])
        self.assertEqual(len(problems), 2, problems)
        conflict = dict(path='c/../d.md', kind='content', stages={'1': None, '2': ('100644', 'a'), '3': None})
        self.assertTrue(sync._unsafe_entries({}, [conflict]))
        link = dict(path='l.md', kind='mode', stages={'1': None, '2': ('120000', 'a'), '3': None})
        self.assertTrue(sync._unsafe_entries({}, [link]))

    def test_a_write_never_leaves_the_work_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp) / 'work'
            work.mkdir()
            for name in ('../outside.md', 'a/../../outside.md', '.git/config'):
                with self.subTest(name=name), self.assertRaises(WikiError):
                    sync._write_entry(work, name, '100644', b'x')
            self.assertFalse((Path(temp) / 'outside.md').exists())


if __name__ == '__main__':
    unittest.main()
