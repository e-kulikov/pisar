"""A save that commits to one child repository twice can resume after any interruption."""
import hashlib
import json
import unittest
from unittest import mock

from pisar import gitops, operations
from pisar.safety import WikiError
from pisar.spaces import Wiki
from .support import Fixture, git


class RecoveredChildCommits(Fixture):
    def test_interruption_after_the_second_child_commit_resumes_through_the_parent_gitlink(self):
        module = self.add_submodule()
        source = self.external()
        plan = self.base / 'plan.json'
        plan.write_text(json.dumps(dict(
            schema_version=1, operation_id='twice-in-a-child', space_id='work/module',
            source=dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
            meeting=dict(id='child-meeting', title='Child meeting', body='Agreed to launch.'),
            tasks=[], pages=[])))
        real, calls = gitops.commit, []

        def commit(repo, paths, operation, **kwargs):
            result = real(repo, paths, operation, **kwargs)
            if repo == module and result:
                calls.append(result)
                if len(calls) == 2:  # killed right after the second child commit, before any checkpoint
                    raise WikiError('interrupted after the second child commit')
            return result
        with mock.patch.object(gitops, 'commit', commit), self.assertRaises(WikiError):
            operations.save(Wiki(self.root), self.state, plan)
        self.assertEqual(len(calls), 2)
        self.cli('--ruwana', 'ruwana', 'save', '--plan', plan)  # the retry used to fail on the first SHA
        pinned = git(self.root, 'ls-tree', 'HEAD', 'work/module').split()[2]
        self.assertEqual(pinned, git(module, 'rev-parse', 'HEAD'))
        self.assertEqual(pinned, calls[1])
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertTrue(self.data('check')['ok'])


if __name__ == '__main__':
    unittest.main()
