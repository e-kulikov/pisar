"""Host validation and domain isolation for semantic sync review."""
import math
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from pisar import sync_review
from pisar.safety import WikiError


def report(domain='work', **updates):
    value = {
        'schema_version': 1, 'candidate_revision': 'rev-1', 'domain': domain,
        'model': 'fake-reviewer', 'prompt_version': sync_review.PROMPT_VERSION,
        'confidence': 0.99, 'context_complete': True, 'verdict': 'clear',
        'statements': [{'id': 's1', 'side': 'theirs', 'path': 'work/alpha/note.md',
                        'quote': 'Remote proposal.', 'disposition': 'preserved',
                        'output_path': 'work/alpha/note.md', 'output_quote': 'Remote proposal.',
                        'reason': 'The incoming assertion remains.'}],
        'findings': [], 'summary': 'The changed statement is preserved.'}
    value.update(updates)
    return value


class ReviewSchemaTests(unittest.TestCase):
    def setUp(self):
        self.request = {'candidate_revision': 'rev-1', 'domain': 'work',
                        'changed_paths': ['work/alpha/note.md'],
                        'versions': {'theirs': {'work/alpha/note.md': 'Remote proposal.'},
                                     'candidate': {'work/alpha/note.md': 'Remote proposal.'}}}

    def test_accepts_well_bound_complete_report(self):
        checked = sync_review.validate_review(self.request, report())
        self.assertEqual(checked['verdict'], 'clear')
        self.assertEqual(checked['domain'], 'work')

    def test_rejects_stale_or_other_domain_report(self):
        with self.assertRaisesRegex(WikiError, 'candidate_revision'):
            sync_review.validate_review(self.request, report(candidate_revision='old'))
        with self.assertRaisesRegex(WikiError, 'domain'):
            sync_review.validate_review(self.request, report(domain='personal'))

    def test_rejects_nonfinite_confidence_and_incomplete_context(self):
        with self.assertRaisesRegex(WikiError, 'confidence'):
            sync_review.validate_review(self.request, report(confidence=math.nan))
        with self.assertRaisesRegex(WikiError, 'context_complete'):
            sync_review.validate_review(self.request, report(context_complete=False))

    def test_rejects_missing_changed_path_accounting_and_invented_quote(self):
        value = report(statements=[])
        with self.assertRaisesRegex(WikiError, 'changed path'):
            sync_review.validate_review(self.request, value)
        value = report(statements=[{**report()['statements'][0], 'quote': 'not present'}])
        with self.assertRaisesRegex(WikiError, 'quote'):
            sync_review.validate_review(self.request, value)

    def test_clear_report_cannot_silently_label_a_claim_lost_or_superseded(self):
        value = report(statements=[{**report()['statements'][0], 'disposition': 'lost',
                                    'output_path': None, 'output_quote': None}])
        with self.assertRaisesRegex(WikiError, 'needs an evidence finding'):
            sync_review.validate_review(self.request, value)

    def test_rejects_duplicate_json_keys_and_non_json_numbers(self):
        with self.assertRaisesRegex(WikiError, 'duplicate'):
            sync_review.decode_report(b'{"verdict":"clear","verdict":"reject"}')
        with self.assertRaisesRegex(WikiError, 'non-finite'):
            sync_review.decode_report(b'{"confidence":NaN}')


class ReviewerBackendTests(unittest.TestCase):
    def test_configured_codex_backend_returns_a_callable_and_reads_its_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scratch, reviewer_home, main_home = (root / name for name in ('scratch', 'reviewer', 'main'))
            for path in (scratch, reviewer_home, main_home):
                path.mkdir()
            answer = report()

            def bounded(argv, prompt, cwd, environment):
                (Path(cwd) / 'answer.json').write_text(json.dumps(answer), encoding='utf-8')
                return SimpleNamespace(state='ok', returncode=0)

            wiki = SimpleNamespace(root=root)
            with mock.patch.object(sync_review.shutil, 'which', return_value='/usr/bin/codex'), \
                    mock.patch.object(sync_review.codex_agent, 'helper_home',
                                      side_effect=[reviewer_home, main_home]), \
                    mock.patch.object(sync_review.codex_agent, 'exec_environment', return_value={}), \
                    mock.patch.object(sync_review.research, 'scratch_base', return_value=scratch), \
                    mock.patch.object(sync_review.research, 'run_bounded', side_effect=bounded):
                backend = sync_review._codex_backend(wiki, root / 'runtime')
                self.assertTrue(callable(backend))
                self.assertEqual(backend({'test': 'request'}), answer)


class ManualReviewCommandTests(unittest.TestCase):
    def test_manual_fallback_has_a_distinct_command_from_unattended_semantic_review(self):
        from pisar.cli import parser
        parsed = parser().parse_args(['sync', 'manual-review', '--operation', 'op-1'])
        self.assertEqual((parsed.command, parsed.action, parsed.operation),
                         ('sync', 'manual-review', 'op-1'))
        parsed = parser().parse_args(['sync', 'resolve', '--operation', 'op-2'])
        self.assertEqual((parsed.action, parsed.operation), ('resolve', 'op-2'))
        with self.assertRaises(SystemExit):
            parser().parse_args(['sync', 'resolve', '--operation', 'op-2', '--revision', 'digest'])
        parsed = parser().parse_args(['sync', 'resolve-accept', '--operation', 'op-2', '--revision', 'digest',
                                      '--decision', 'keep both'])
        self.assertEqual((parsed.action, parsed.operation, parsed.revision, parsed.decision),
                         ('resolve-accept', 'op-2', 'digest', 'keep both'))
        self.assertEqual(parser().parse_args(['sync', 'feedback-summary']).action, 'feedback-summary')
        with self.assertRaises(SystemExit):
            parser().parse_args(['sync', 'review', '--operation', 'op-1', '--manual'])


if __name__ == '__main__':
    unittest.main()
