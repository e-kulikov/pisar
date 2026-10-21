"""Committed source-processing provenance is portable Git data."""
import hashlib
import unittest

from .support import Fixture, commit_all, git
from pisar.safety import WikiError
from pisar.spaces import Wiki


def processed_capture(space, capture_id, digest, meeting_reference):
    return ('+++\nschema_version = 1\n'
            f'id = "{capture_id}"\ntype = "source"\ntitle = "Captured source"\n'
            f'space_ids = ["{space}"]\nsources = ["sources/captures/{capture_id}/original"]\n'
            f'source_sha256 = "{digest}"\ningest_status = "processed"\n'
            f'processed_by = "save-{capture_id}"\nmeeting_reference = "{meeting_reference}"\n'
            '+++\n\nProcessed synthetic source.\n')


def meeting(space, ident, source_path):
    return ('+++\nschema_version = 1\n'
            f'id = "{ident}"\ntype = "meeting"\ntitle = "Synthetic meeting"\n'
            f'space_ids = ["{space}"]\nsources = ["{source_path}"]\n'
            '+++\n\nSynthetic meeting record.\n')


class ProvenanceTests(Fixture):
    def test_committed_capture_and_meeting_link_are_found_by_domain_space_and_hash(self):
        from pisar.provenance import committed_processing, find_processed
        source = b'original bytes\r\nwith exact whitespace \t\r\n'
        digest = hashlib.sha256(source).hexdigest()
        (self.alpha / 'sources/captures/one').mkdir(parents=True)
        (self.alpha / 'sources/captures/one/original').write_bytes(source)
        (self.alpha / 'inbox').mkdir()
        capture_ref = 'wiki:work/alpha:one'
        meeting_ref = 'wiki:work/alpha:meeting-one'
        (self.alpha / 'inbox/one.md').write_text(processed_capture('work/alpha', 'one', digest, meeting_ref))
        transcript = 'sources/meetings/meeting-one/transcript.md'
        (self.alpha / transcript).parent.mkdir(parents=True)
        (self.alpha / transcript).write_bytes(source)
        (self.alpha / 'meetings').mkdir()
        (self.alpha / 'meetings/meeting-one.md').write_text(meeting('work/alpha', 'meeting-one', transcript))
        commit_all(self.root)

        inventory = committed_processing(Wiki(self.root))
        found = find_processed(inventory, 'work/alpha', digest)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].source_sha256, digest)
        self.assertEqual(found[0].capture_reference, capture_ref)
        self.assertEqual(found[0].meeting_reference, meeting_ref)
        self.assertEqual(found[0].source_path, 'sources/captures/one/original')
        self.assertEqual((self.alpha / transcript).read_bytes(), source)

    def test_legacy_meeting_source_without_capture_descriptor_is_discovered(self):
        from pisar.provenance import committed_processing, find_processed
        source = b'legacy original\n'
        digest = hashlib.sha256(source).hexdigest()
        transcript = 'sources/meetings/legacy/transcript.md'
        (self.alpha / transcript).parent.mkdir(parents=True)
        (self.alpha / transcript).write_bytes(source)
        (self.alpha / 'meetings').mkdir()
        (self.alpha / 'meetings/legacy.md').write_text(meeting('work/alpha', 'legacy', transcript))
        commit_all(self.root)

        found = find_processed(committed_processing(Wiki(self.root)), 'work/alpha', digest)
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0].capture_reference)
        self.assertEqual(found[0].meeting_reference, 'wiki:work/alpha:legacy')
        self.assertEqual(found[0].source_path, transcript)

    def test_interrupted_pending_meeting_is_incomplete_not_processed(self):
        from pisar.provenance import committed_processing, find_processed
        source = b'committed transcript before task creation\n'
        digest = hashlib.sha256(source).hexdigest()
        transcript = 'sources/meetings/interrupted/transcript.md'
        (self.alpha / transcript).parent.mkdir(parents=True)
        (self.alpha / transcript).write_bytes(source)
        (self.alpha / 'meetings').mkdir()
        pending = meeting('work/alpha', 'interrupted', transcript).replace(
            'Synthetic meeting record.',
            '## Ruwana references\n\nPending: operation `save-interrupted` is incomplete. No task IDs claimed.')
        (self.alpha / 'meetings/interrupted.md').write_text(pending)
        commit_all(self.root)

        inventory = committed_processing(Wiki(self.root))
        self.assertFalse(inventory.processed)
        with self.assertRaisesRegex(WikiError, 'incomplete'):
            find_processed(inventory, 'work/alpha', digest)

    def test_two_independent_capture_ids_for_one_original_remain_two_decisions(self):
        from pisar.provenance import committed_processing, find_processed
        source = b'independently processed same original\n'
        digest = hashlib.sha256(source).hexdigest()
        (self.alpha / 'inbox').mkdir()
        for capture_id, meeting_id in (('machine-one', 'meeting-one'), ('machine-two', 'meeting-two')):
            capture_dir = self.alpha / f'sources/captures/{capture_id}'
            capture_dir.mkdir(parents=True)
            (capture_dir / 'original').write_bytes(source)
            meeting_ref = f'wiki:work/alpha:{meeting_id}'
            (self.alpha / f'inbox/{capture_id}.md').write_text(
                processed_capture('work/alpha', capture_id, digest, meeting_ref))
            transcript = f'sources/meetings/{meeting_id}/transcript.md'
            (self.alpha / transcript).parent.mkdir(parents=True)
            (self.alpha / transcript).write_bytes(source)
            (self.alpha / 'meetings').mkdir(exist_ok=True)
            (self.alpha / f'meetings/{meeting_id}.md').write_text(
                meeting('work/alpha', meeting_id, transcript))
        commit_all(self.root)

        found = find_processed(committed_processing(Wiki(self.root)), 'work/alpha', digest)
        self.assertEqual(len(found), 2)
        self.assertEqual({item.meeting_reference for item in found},
                         {'wiki:work/alpha:meeting-one', 'wiki:work/alpha:meeting-two'})

    def test_incomplete_committed_capture_without_local_journal_is_a_conflict(self):
        from pisar.provenance import committed_processing, find_processed
        source = b'committed but processing is incomplete\n'
        digest = hashlib.sha256(source).hexdigest()
        (self.alpha / 'sources/captures/incomplete').mkdir(parents=True)
        (self.alpha / 'sources/captures/incomplete/original').write_bytes(source)
        (self.alpha / 'inbox').mkdir()
        text = processed_capture('work/alpha', 'incomplete', digest, 'wiki:work/alpha:missing-meeting')
        text = text.replace('ingest_status = "processed"', 'ingest_status = "pending"')
        text = text.replace('processed_by = "save-incomplete"\n', '').replace(
            'meeting_reference = "wiki:work/alpha:missing-meeting"\n', '')
        (self.alpha / 'inbox/incomplete.md').write_text(text)
        commit_all(self.root)

        with self.assertRaisesRegex(WikiError, 'incomplete|pending|decision'):
            find_processed(committed_processing(Wiki(self.root)), 'work/alpha', digest)

    def test_inventory_ignores_uncommitted_processing_and_scopes_equal_hash_by_domain(self):
        from pisar.provenance import committed_processing, find_processed
        source = b'identical original bytes\n'
        digest = hashlib.sha256(source).hexdigest()
        # The same exact source is committed once in each personal/work domain.
        personal = self.root / 'personal/10-projects/home'
        for space_path, space_id, ident in ((self.alpha, 'work/alpha', 'work-meeting'),
                                             (personal, 'personal/home', 'personal-meeting')):
            transcript = f'sources/meetings/{ident}/transcript.md'
            (space_path / transcript).parent.mkdir(parents=True)
            (space_path / transcript).write_bytes(source)
            (space_path / 'meetings').mkdir(exist_ok=True)
            (space_path / f'meetings/{ident}.md').write_text(meeting(space_id, ident, transcript))
        commit_all(self.root)

        # A second uncommitted occurrence must not become portable provenance yet.
        pending = self.alpha / 'sources/meetings/uncommitted/transcript.md'
        pending.parent.mkdir(parents=True)
        pending.write_bytes(source)
        (self.alpha / 'meetings/uncommitted.md').write_text(meeting('work/alpha', 'uncommitted',
                                                                    'sources/meetings/uncommitted/transcript.md'))
        inventory = committed_processing(Wiki(self.root))
        self.assertEqual(len(find_processed(inventory, 'work/alpha', digest)), 1)
        self.assertEqual(len(find_processed(inventory, 'personal/home', digest)), 1)
        self.assertEqual(find_processed(inventory, 'work/alpha', '0' * 64), [])
        self.assertEqual(git(self.root, 'status', '--porcelain') != '', True)


if __name__ == '__main__':
    unittest.main()
