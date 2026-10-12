"""A source of a space at a submodule's root is immutable too (review 20, finding 6): the root space's `sources/`
folder is recognised in the merge and in the resolution check, as nested spaces already are."""
from pathlib import Path

from pisar import sync
from .sync_faults import RemoteFixture
from .support import commit_all, git


class RootSpaceSourceTests(RemoteFixture):
    def setUp(self):
        super().setUp()
        self.new_world('root-space')
        self.child = self.add_submodule()  # work/module: its .wiki.toml is at its own root
        self.origin = self.base / 'module-origin'
        (self.origin / 'sources').mkdir()
        (self.origin / 'sources/raw.txt').write_text('one\ntwo\nthree\nfour\nfive\n')
        commit_all(self.origin)
        git(self.child, 'pull', '-q')
        commit_all(self.root)
        self.bare = self.base / 'child.git'
        git(self.origin, 'clone', '-q', '--bare', str(self.origin), str(self.bare))
        git(self.child, 'remote', 'set-url', 'origin', str(self.bare))

    def diverge_source(self):
        (self.origin / 'sources/raw.txt').write_text('one\ntwo\nthree\nfour\nTHEIRS\n')
        commit_all(self.origin)
        git(self.origin, 'push', '-q', str(self.bare), 'HEAD')
        (self.child / 'sources/raw.txt').write_text('OURS\ntwo\nthree\nfour\nfive\n')
        commit_all(self.child)
        commit_all(self.root)

    def test_a_root_space_source_is_a_conflict_never_merged_line_by_line(self):
        self.diverge_source()
        sync.do_fetch(self.root, self.state, relative='work/module')
        result = sync.do_pull(self.root, self.state, relative='work/module', merge=True)
        self.assertEqual(result['status'], 'conflicts', result)
        conflict = result['conflicts'][0]
        self.assertEqual((conflict['path'], conflict['kind'], conflict['sources']),
                         ('sources/raw.txt', 'content', True))
        text = (Path(result['work']) / 'sources/raw.txt').read_text()
        self.assertNotIn('THEIRS', text, 'the remote side was merged into the source')
        self.assertEqual(text, 'OURS\ntwo\nthree\nfour\nfive\n')

    def test_a_manual_edit_of_a_root_space_source_is_refused_at_resolve(self):
        self.diverge_source()
        sync.do_fetch(self.root, self.state, relative='work/module')
        result = sync.do_pull(self.root, self.state, relative='work/module', merge=True)
        (Path(result['work']) / 'sources/raw.txt').write_text('OURS\ntwo\nthree\nfour\nhand edited\n')
        with self.assertRaisesRegex(Exception, 'immutable'):
            sync.do_resolve(self.root, self.state, result['sync_id'])
        self.assertEqual(git(self.child, 'status', '--porcelain'), '')
