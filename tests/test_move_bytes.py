"""Moves, archives and restores keep every committed blob; Git attributes never re-stage the tree."""
import subprocess
import unittest

from .support import git
from .test_spacecmd import SpaceCommandFixture

ORIGINAL = b'Alpha line\r\nsecond line\r\n'


class MoveKeepsBlobs(SpaceCommandFixture):
    def setUp(self):
        super().setUp()
        self.source = self.base / 'crlf-original.txt'
        self.source.write_bytes(ORIGINAL)
        self.cli('capture', '--space', 'work/alpha', '--id', 'bytes', '--source', self.source)

    def attributes(self, text, config=None):
        (self.root / '.gitattributes').write_text(text)
        for key, value in (config or {}).items():
            git(self.root, 'config', key, value)
        git(self.root, 'add', '--', '.gitattributes')
        git(self.root, 'commit', '-qm', 'attributes')

    def tree(self):
        """Committed blob id of every file of work/alpha, keyed by its path inside the space."""
        space = self.spaces()['work/alpha']['path']
        out = subprocess.run(['git', '-C', str(self.root), 'ls-tree', '-r', 'HEAD', '--', space],
                             capture_output=True, text=True, check=True).stdout
        return space, {line.split('\t')[1][len(space) + 1:]: line.split()[2] for line in out.splitlines()}

    def blob(self, path):
        return subprocess.run(['git', '-C', str(self.root), 'cat-file', 'blob', f'HEAD:{path}'],
                              capture_output=True, check=True).stdout

    def step(self, *args, attributes=None, config=None):
        """Run one command; the attributes apply only at its destination, so the tree is clean before it."""
        if attributes:
            self.attributes(attributes, config)
        space, before = self.tree()
        marker = before.pop('.wiki.toml')
        self.cli('space', args[0], 'work/alpha', *args[1:])
        new_space, after = self.tree()
        self.assertNotEqual(new_space, space)
        self.assertNotEqual(after.pop('.wiki.toml'), marker)  # only the marker is new content
        self.assertEqual(after, before, args)  # every committed blob id is unchanged
        self.assertEqual(self.blob(f'{new_space}/sources/captures/bytes/original'), ORIGINAL)
        self.assertEqual((self.root / new_space / 'sources/captures/bytes/original').read_bytes(), ORIGINAL)

    def test_text_attributes_at_the_destination_do_not_alter_moved_blobs(self):
        self.step('move', '--to', 'area', attributes='work/20-areas/**/original text\n')
        self.step('archive', attributes='work/40-archives/**/original text\n')
        self.step('restore', attributes='work/10-projects/**/original text\n')

    def test_eol_conversion_at_the_destination_does_not_alter_moved_blobs(self):
        self.step('move', '--to', 'area', attributes='work/20-areas/**/original text eol=crlf\n')

    def test_a_clean_filter_at_the_destination_never_runs_during_a_move(self):
        self.step('move', '--to', 'area', attributes='work/20-areas/**/original filter=rewrite\n',
                  config={'filter.rewrite.clean': 'sed s/line/CHANGED/'})

    def test_a_space_with_a_nested_space_keeps_all_blobs(self):
        nested = self.root / 'work/team/alpha/inner'
        nested.mkdir()
        (nested / '.wiki.toml').write_text('schema_version = 1\nid = "inner"\nkind = "project"\nstatus = "active"\n')
        (nested / 'notes.txt').write_bytes(b'inner\r\nbytes\r\n')
        git(self.root, 'add', '--', 'work')
        git(self.root, 'commit', '-qm', 'nested')
        self.step('move', '--to', 'area', attributes='work/20-areas/**/original text\nwork/20-areas/**/*.txt text\n')
        _, tree = self.tree()
        self.assertEqual(subprocess.run(
            ['git', '-C', str(self.root), 'cat-file', 'blob',
             f'HEAD:work/20-areas/alpha/inner/notes.txt'], capture_output=True, check=True).stdout,
            b'inner\r\nbytes\r\n')

    def test_attributes_that_would_transform_the_marker_refuse_before_any_change(self):
        for text, config in (('work/20-areas/** text\n', None), ('* text eol=crlf\n', None),
                             ('*.toml filter=rewrite\n', {'filter.rewrite.clean': 'sed s/id/ID/'})):
            with self.subTest(attributes=text):
                self.attributes(text, config)
                head = git(self.root, 'rev-parse', 'HEAD')
                path = self.spaces()['work/alpha']['path']
                marker = (self.root / path / '.wiki.toml').read_bytes()
                p = self.cli('space', 'move', 'work/alpha', '--to', 'area', ok=False)
                self.assertIn('.gitattributes', p.stderr)
                self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
                self.assertEqual(self.spaces()['work/alpha']['path'], path)
                self.assertEqual((self.root / path / '.wiki.toml').read_bytes(), marker)
                git(self.root, 'rm', '-q', '--', '.gitattributes')
                git(self.root, 'commit', '-qm', 'drop attributes')


if __name__ == '__main__':
    unittest.main()
