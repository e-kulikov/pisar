"""Constrained candidate resolver tools and Stage IV acceptance gates."""
import tempfile
import unittest
from pathlib import Path

from pisar import sync_resolver, sync_git
from pisar.safety import WikiError


class CandidateResolverToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / 'candidate'
        self.repo.mkdir()
        sync_git.text(self.repo, 'init', '-b', 'candidate')
        sync_git.text(self.repo, 'config', 'user.name', 'Synthetic User')
        sync_git.text(self.repo, 'config', 'user.email', 'synthetic@example.invalid')
        (self.repo / 'work').mkdir()
        (self.repo / 'work' / 'note.md').write_text('ours\n', encoding='utf-8')
        (self.repo / 'work' / 'private.md').write_text('private\n', encoding='utf-8')
        sync_git.text(self.repo, 'add', '--', '.')
        sync_git.text(self.repo, 'commit', '-m', 'fixture')
        self.base = sync_git.head(self.repo)
        self.tools = sync_resolver.CandidateTools(
            self.repo, domain='work', allowed_paths={'work/note.md'}, expected_head=self.base)

    def tearDown(self):
        self.temp.cleanup()

    def test_context_is_domain_scoped_and_only_exposes_bound_candidate_tools(self):
        context = self.tools.candidate_context()
        self.assertEqual(context['domain'], 'work')
        self.assertEqual(context['files'], {'work/note.md': 'ours\n'})
        self.assertEqual(set(self.tools.tool_names), {
            'candidate_context', 'candidate_write', 'candidate_stage', 'candidate_commit'})
        self.assertFalse(any('shell' in name or 'git' in name or 'push' in name for name in self.tools.tool_names))

    def test_write_requires_expected_digest_and_cannot_escape_bound_paths(self):
        import hashlib
        digest = hashlib.sha256(b'ours\n').hexdigest()
        with self.assertRaisesRegex(WikiError, 'expected_sha256'):
            self.tools.candidate_write('work/note.md', '0' * 64, 'resolved\n')
        with self.assertRaisesRegex(WikiError, 'outside'):
            self.tools.candidate_write('../outside', digest, 'x')
        with self.assertRaisesRegex(WikiError, 'outside'):
            self.tools.candidate_write('work/private.md', hashlib.sha256(b'private\n').hexdigest(), 'x')
        self.assertEqual((self.repo / 'work' / 'note.md').read_text(), 'ours\n')

    def test_failed_write_or_stage_does_not_authorize_commit(self):
        with self.assertRaisesRegex(WikiError, 'staged'):
            self.tools.candidate_commit({'summary': 'try opposite content'})
        with self.assertRaisesRegex(WikiError, 'expected_sha256'):
            self.tools.candidate_write('work/note.md', '0' * 64, 'resolved\n')
        digest = __import__('hashlib').sha256(b'ours\n').hexdigest()
        self.tools.candidate_write('work/note.md', digest, 'resolved\n')
        with self.assertRaisesRegex(WikiError, 'every path written'):
            self.tools.candidate_stage(['work/private.md'])
        with self.assertRaisesRegex(WikiError, 'staged'):
            self.tools.candidate_commit({'summary': 'commit'} )

    def test_commit_requires_all_written_bytes_staged_and_preserves_unrelated_content(self):
        import hashlib
        expected = hashlib.sha256(b'ours\n').hexdigest()
        self.tools.candidate_write('work/note.md', expected, 'resolved\n')
        with self.assertRaisesRegex(WikiError, 'staged'):
            self.tools.candidate_commit({'summary': 'resolved candidate'})
        self.tools.candidate_stage(['work/note.md'])
        report = {'summary': 'resolved candidate'}
        self.tools.authorize_commit(report)
        result = self.tools.candidate_commit(report)
        self.assertEqual(sync_git.parents(self.repo, result['commit']), (self.base,))
        self.assertEqual((self.repo / 'work' / 'private.md').read_text(), 'private\n')
        self.assertEqual((self.repo / 'work' / 'note.md').read_text(), 'resolved\n')

    def test_commit_refuses_when_index_blob_differs_from_accepted_worktree_output(self):
        import hashlib
        import subprocess
        expected = hashlib.sha256(b'ours\n').hexdigest()
        self.tools.candidate_write('work/note.md', expected, 'accepted output\n')
        self.tools.candidate_stage(['work/note.md'])
        opposite = subprocess.run(['git', '-C', str(self.repo), 'hash-object', '-w', '--stdin'],
                                  input=b'opposite indexed bytes\n', capture_output=True, check=True).stdout.decode().strip()
        sync_git.text(self.repo, 'update-index', '--cacheinfo', '100644', opposite, 'work/note.md')
        report = {'summary': 'Commit accepted output'}
        with self.assertRaisesRegex(WikiError, 'index|staged'):
            self.tools.authorize_commit(report)
        self.assertEqual(sync_git.head(self.repo), self.base)

    def test_commit_preserves_an_inherited_staged_deletion(self):
        import hashlib
        import subprocess
        (self.repo / 'work/private.md').unlink()
        sync_git.text(self.repo, 'add', '--', 'work/private.md')
        expected = hashlib.sha256(b'ours\n').hexdigest()
        self.tools = sync_resolver.CandidateTools(
            self.repo, domain='work', allowed_paths={'work/note.md', 'work/private.md'}, expected_head=self.base)
        self.tools.candidate_write('work/note.md', expected, 'resolved\n')
        self.tools.candidate_stage(['work/note.md'])
        report = {'summary': 'Commit candidate resolution'}
        self.tools.authorize_commit(report)
        committed = self.tools.candidate_commit(report)
        absent = subprocess.run(['git', '-C', str(self.repo), 'cat-file', '-e',
                                 f'{committed["commit"]}:work/private.md'], capture_output=True)
        self.assertNotEqual(absent.returncode, 0)

    def test_owner_approval_binds_inherited_index_blob_from_the_proposal(self):
        import hashlib
        import subprocess
        from pisar.sync_resolver import CandidateTools
        (self.repo / 'work/private.md').write_text('inherited staged output\n')
        sync_git.text(self.repo, 'add', '--', 'work/private.md')
        initial_staged = {'work/private.md'}
        first = CandidateTools(self.repo, domain='*', allowed_paths={'work/note.md', 'work/private.md'},
                               expected_head=self.base, initial_staged=initial_staged)
        saved_index = {name: [list(entry) for entry in entries]
                       for name, entries in first._initial_index.items()}
        first.candidate_write('work/note.md', hashlib.sha256(b'ours\n').hexdigest(), 'accepted\n')
        first.candidate_stage(['work/note.md'])
        second = CandidateTools(self.repo, domain='*', allowed_paths={'work/note.md', 'work/private.md'},
                                expected_head=self.base, initial_staged=initial_staged,
                                initial_index=saved_index)
        second._written = dict(first._written)
        second._staged = set(first._staged)
        opposite = subprocess.run(['git', '-C', str(self.repo), 'hash-object', '-w', '--stdin'],
                                  input=b'changed inherited bytes\n', capture_output=True,
                                  check=True).stdout.decode().strip()
        sync_git.text(self.repo, 'update-index', '--cacheinfo', '100644', opposite, 'work/private.md')
        with self.assertRaisesRegex(WikiError, 'inherited staged index entry'):
            second.authorize_commit({'summary': 'Commit candidate'})
        self.assertEqual(sync_git.head(self.repo), self.base)

    def test_resolver_refuses_git_topology_metadata(self):
        path = self.repo / '.gitmodules'
        path.write_text('[submodule "x"]\n\tpath = x\n\turl = ./one\n', encoding='utf-8')
        sync_git.text(self.repo, 'add', '.gitmodules')
        sync_git.text(self.repo, 'commit', '-m', 'fixture submodule metadata')
        head = sync_git.head(self.repo)
        with self.assertRaisesRegex(WikiError, 'metadata|topology|Git'):
            sync_resolver.CandidateTools(self.repo, domain='workspace', allowed_paths={'.gitmodules'},
                                         expected_head=head).candidate_context()
        self.assertEqual(path.read_text(), '[submodule "x"]\n\tpath = x\n\turl = ./one\n')

    def test_late_stale_candidate_response_is_refused(self):
        context = self.tools.candidate_context()
        (self.repo / 'work' / 'note.md').write_text('changed while resolver ran\n')
        import hashlib
        with self.assertRaisesRegex(WikiError, 'stale'):
            self.tools.candidate_write('work/note.md', hashlib.sha256(context['files']['work/note.md'].encode()).hexdigest(),
                                       'late model response\n')

    def test_manual_resolution_is_proposed_then_committed_only_after_owner_decision(self):
        import hashlib

        def fake(context, tools):
            digest = hashlib.sha256(context['files']['work/note.md'].encode()).hexdigest()
            result = tools.candidate_write('work/note.md', digest, 'the selected combined fact\n')
            tools.candidate_stage(['work/note.md'])
            return {'summary': 'Resolve competing statements', 'confidence': 0.99, 'context_complete': True,
                    'findings': [],
                    'statements': [{'id': 's1', 'disposition': 'preserved'}],
                    'mappings': [{'path': 'work/note.md', 'sha256': result['sha256'],
                                  'statement_ids': ['s1']}]}

        proposal = sync_resolver.resolve_proposal(
            self.repo, 'work', {'work/note.md'}, fake, expected_head=self.base)
        self.assertEqual(sync_git.head(self.repo), self.base)
        policy = {'approval': 'manual', 'min_confidence': 0.95}
        with self.assertRaisesRegex(WikiError, 'owner approval'):
            sync_resolver.commit_proposal(proposal, policy)
        self.assertEqual(sync_git.head(self.repo), self.base)
        committed = sync_resolver.commit_proposal(proposal, policy,
                                                   human_decision='keep both with attribution')
        self.assertEqual(sync_git.parents(self.repo, committed['commit']), (self.base,))

    def test_resolver_cannot_edit_source_task_or_binary_inputs(self):
        with self.assertRaisesRegex(WikiError, 'task, source, wiki or Git topology metadata'):
            sync_resolver.CandidateTools(self.repo, domain='work', allowed_paths={'work/.ruwana/task.toml'},
                                         expected_head=self.base).candidate_context()
        with self.assertRaisesRegex(WikiError, 'task, source, wiki or Git topology metadata'):
            sync_resolver.CandidateTools(self.repo, domain='work', allowed_paths={'work/sources/original.md'},
                                         expected_head=self.base).candidate_context()
        (self.repo / 'work' / 'binary.bin').write_bytes(b'\xff')
        with self.assertRaisesRegex(WikiError, 'non-text'):
            sync_resolver.CandidateTools(self.repo, domain='work', allowed_paths={'work/binary.bin'},
                                         expected_head=self.base).candidate_context()
        (self.repo / 'work' / 'note.md').unlink()
        (self.repo / 'work' / 'note.md').symlink_to(self.root / 'outside.md')
        with self.assertRaisesRegex(WikiError, 'symlink'):
            sync_resolver.CandidateTools(self.repo, domain='work', allowed_paths={'work/note.md'},
                                         expected_head=self.base).candidate_context()

    def test_json_tool_plan_is_host_executed_and_cannot_add_a_shell_capability(self):
        import hashlib
        tools = self.tools
        output = 'host-applied\n'
        report = {'summary': 'Apply the scoped candidate correction', 'confidence': 0.99,
                  'context_complete': True,
                  'statements': [{'id': 's1', 'disposition': 'preserved'}], 'findings': [],
                  'mappings': [{'path': 'work/note.md', 'sha256': hashlib.sha256(output.encode()).hexdigest(),
                                'statement_ids': ['s1']}]}
        plan = {'tool_calls': [
            {'name': 'candidate_write', 'arguments': {'path': 'work/note.md',
             'expected_sha256': hashlib.sha256(b'ours\n').hexdigest(), 'text': output}},
            {'name': 'candidate_stage', 'arguments': {'paths': ['work/note.md']}},
            {'name': 'candidate_commit', 'arguments': {'report': report}}], 'report': report}
        self.assertEqual(sync_resolver.execute_plan(plan, tools), report)
        self.assertEqual((self.repo / 'work/note.md').read_text(), output)
        plan['tool_calls'][0]['name'] = 'shell'
        with self.assertRaisesRegex(WikiError, 'unsupported tool'):
            sync_resolver.execute_plan(plan, tools)

    def test_json_tool_plan_rejects_oversized_statement_id_before_candidate_writes(self):
        import hashlib
        original_index = sync_git.text(self.repo, 'write-tree')
        output = 'would otherwise be written\n'
        digest = hashlib.sha256(output.encode()).hexdigest()
        ident = 's' * 201
        report = {'summary': 'Reject malformed accounting', 'confidence': 0.99,
                  'context_complete': True,
                  'statements': [{'id': ident, 'disposition': 'preserved'}], 'findings': [],
                  'mappings': [{'path': 'work/note.md', 'sha256': digest, 'statement_ids': [ident]}]}
        plan = {'tool_calls': [
            {'name': 'candidate_write', 'arguments': {'path': 'work/note.md',
             'expected_sha256': hashlib.sha256(b'ours\n').hexdigest(), 'text': output}},
            {'name': 'candidate_stage', 'arguments': {'paths': ['work/note.md']}},
            {'name': 'candidate_commit', 'arguments': {'report': report}}], 'report': report}
        with self.assertRaisesRegex(WikiError, 'statement accounting is invalid'):
            sync_resolver.execute_plan(plan, self.tools)
        self.assertEqual((self.repo / 'work/note.md').read_text(), 'ours\n')
        self.assertEqual(sync_git.text(self.repo, 'write-tree'), original_index)
        self.assertEqual(sync_git.text(self.repo, 'status', '--porcelain=v1'), '')

    def test_fake_resolver_resolves_a_real_git_conflict_without_touching_other_files(self):
        import hashlib
        sync_git.text(self.repo, 'switch', '-c', 'theirs')
        (self.repo / 'work/note.md').write_text('theirs\n')
        sync_git.text(self.repo, 'add', 'work/note.md')
        sync_git.text(self.repo, 'commit', '-m', 'theirs')
        theirs = sync_git.head(self.repo)
        sync_git.text(self.repo, 'switch', 'candidate')
        (self.repo / 'work/note.md').write_text('ours version\n')
        sync_git.text(self.repo, 'add', 'work/note.md')
        sync_git.text(self.repo, 'commit', '-m', 'ours')
        ours = sync_git.head(self.repo)
        sync_git.text(self.repo, 'merge', '--no-commit', '--no-ff', theirs, check=False)
        self.assertTrue(sync_git.run(self.repo, 'ls-files', '--unmerged', '-z'))

        def fake(context, tools):
            output = 'combined resolution\n'
            written = tools.candidate_write('work/note.md',
                                             hashlib.sha256(context['files']['work/note.md'].encode()).hexdigest(),
                                             output)
            tools.candidate_stage(['work/note.md'])
            return {'summary': 'Resolve fixture conflict', 'confidence': 0.99, 'context_complete': True,
                    'findings': [],
                    'statements': [{'id': 's1', 'disposition': 'preserved'}],
                    'mappings': [{'path': 'work/note.md', 'sha256': written['sha256'],
                                  'statement_ids': ['s1']}]}

        proposal = sync_resolver.resolve_proposal(self.repo, 'work', {'work/note.md'}, fake,
                                                   expected_head=ours)
        committed = sync_resolver.commit_proposal(proposal, {'approval': 'manual', 'min_confidence': 0.95},
                                                    human_decision='accept test resolution')
        self.assertEqual(sync_git.parents(self.repo, committed['commit']), (ours, theirs))
        self.assertEqual((self.repo / 'work/note.md').read_text(), 'combined resolution\n')
        self.assertEqual((self.repo / 'work/private.md').read_text(), 'private\n')


class AcceptanceDecisionTests(unittest.TestCase):
    def bundle(self, **overrides):
        result = {'reports': [{'verdict': 'clear', 'context_complete': True, 'confidence': 0.95,
                              'findings': [], 'statements': [{'disposition': 'preserved'}]}],
                  'technical_checks_passed': True, 'candidate_current': True,
                  'resolver_commit_valid': True, 'fresh_review': True}
        result.update(overrides)
        return result

    def test_manual_always_needs_owner_even_at_zero_threshold_and_confidence_one(self):
        policy = {'approval': 'manual', 'min_confidence': 0}
        self.assertEqual(sync_resolver.acceptance_decision(self.bundle(), policy), 'pending')
        self.assertEqual(sync_resolver.acceptance_decision(self.bundle(), policy, 'owner approves'), 'accepted')

    def test_auto_requires_threshold_and_every_hard_gate(self):
        policy = {'approval': 'auto', 'min_confidence': 0.95}
        self.assertEqual(sync_resolver.acceptance_decision(self.bundle(), policy), 'accepted')
        below = self.bundle(reports=[{'verdict': 'clear', 'context_complete': True, 'confidence': 0.949,
                                     'findings': [], 'statements': [{'disposition': 'preserved'}]}])
        self.assertEqual(sync_resolver.acceptance_decision(below, policy), 'pending')
        for key, value in (('technical_checks_passed', False), ('candidate_current', False),
                           ('fresh_review', False)):
            with self.subTest(gate=key):
                self.assertEqual(sync_resolver.acceptance_decision(self.bundle(**{key: value}), policy), 'pending')

    def test_auto_rejects_any_hard_semantic_finding_even_at_confidence_one(self):
        policy = {'approval': 'auto', 'min_confidence': 0}
        for disposition in ('lost', 'uncertain', 'superseded'):
            bundle = self.bundle(reports=[{'verdict': 'clear', 'context_complete': True, 'confidence': 1,
                                           'findings': [], 'statements': [{'disposition': disposition}]}])
            self.assertEqual(sync_resolver.acceptance_decision(bundle, policy), 'pending')
        bundle = self.bundle(reports=[{'verdict': 'needs_decision', 'context_complete': True, 'confidence': 1,
                                       'findings': [{'category': 'contradiction'}],
                                       'statements': [{'disposition': 'preserved'}]}])
        self.assertEqual(sync_resolver.acceptance_decision(bundle, policy), 'pending')

    def test_auto_resolution_refuses_hard_finding_even_at_confidence_one(self):
        import hashlib

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            sync_git.text(repo, 'init', '-b', 'candidate')
            sync_git.text(repo, 'config', 'user.name', 'Synthetic User')
            sync_git.text(repo, 'config', 'user.email', 'synthetic@example.invalid')
            (repo / 'work').mkdir()
            (repo / 'work/note.md').write_text('original\n')
            sync_git.text(repo, 'add', '.')
            sync_git.text(repo, 'commit', '-m', 'fixture')
            head = sync_git.head(repo)

            def fake(context, tools):
                written = tools.candidate_write('work/note.md', hashlib.sha256(b'original\n').hexdigest(),
                                                'changed\n')
                tools.candidate_stage(['work/note.md'])
                return {'summary': 'change', 'confidence': 1.0, 'context_complete': True,
                        'findings': [{'category': 'contradiction', 'paths': ['work/note.md'],
                                      'id': 'f1', 'evidence_quotes': ['original'],
                                      'explanation': 'conflict remains', 'question': 'Which wording?'}],
                        'statements': [{'id': 's1', 'disposition': 'preserved'}],
                        'mappings': [{'path': 'work/note.md', 'sha256': written['sha256'],
                                      'statement_ids': ['s1']}]}

            proposal = sync_resolver.resolve_proposal(repo, 'work', {'work/note.md'}, fake,
                                                       expected_head=head)
            with self.assertRaisesRegex(WikiError, 'automatic-resolution gates'):
                sync_resolver.commit_proposal(proposal, {'approval': 'auto', 'min_confidence': 0})
            self.assertEqual(sync_git.head(repo), head)


if __name__ == '__main__':
    unittest.main()
