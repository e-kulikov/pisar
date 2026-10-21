"""Read-only ruwana snapshots normalize task values at the adapter boundary."""
import json
import os
import unittest
from unittest.mock import patch

from .support import Fixture
from pisar.spaces import Wiki


class RuwanaSnapshotTests(Fixture):
    def test_ruwana_child_gets_candidate_root_and_not_ambient_pisar_settings(self):
        from pisar.ruwana import Ruwana
        binary = self.base / 'environment-ruwana'
        binary.write_text('#!/usr/bin/env python3\nimport json, os\n'
                          'print(json.dumps({key: os.environ.get(key) for key in '
                          '["WIKI_ROOT", "PISAR_ROOT", "PISAR_STATE_DIR", "PISAR_RUWANA_BIN"]}))\n')
        binary.chmod(0o700)
        adapter = Ruwana(Wiki(self.root), binary)
        with patch.dict(os.environ, {'WIKI_ROOT': '/ambient/wiki', 'PISAR_ROOT': '/ambient/root',
                                     'PISAR_STATE_DIR': '/ambient/state',
                                     'PISAR_RUWANA_BIN': '/ambient/ruwana'}):
            values = json.loads(adapter._snapshot_run('environment'))
        self.assertEqual(values['WIKI_ROOT'], str(self.root))
        self.assertIsNone(values['PISAR_ROOT'])
        self.assertIsNone(values['PISAR_STATE_DIR'])
        self.assertIsNone(values['PISAR_RUWANA_BIN'])

    def test_snapshot_is_read_only_and_normalizes_title_and_blank_description(self):
        from pisar.ruwana import Ruwana
        task_id = 'synthetictask'
        task_path = self.alpha / '.ruwana' / f'{task_id}.toml'
        task_path.parent.mkdir()
        task_path.write_text('synthetic task state\n')
        binary = self.base / 'ruwana-test-double'
        binary.write_text('#!/bin/sh\nexit 99\n')
        binary.chmod(0o700)
        records = [{
            'id': task_id,
            'project': 'work/team/alpha',
            'source': ['wiki:work/alpha:meeting#task-synthetic-task'],
            'title': '  Deploy   the release \t',
            'description': ' \u00a0\t\n',
            'due': None,
            'status': 'open',
            'file_path': str(task_path),
        }]
        calls = []

        def fake_run(adapter, *args):
            calls.append(args)
            if args[0] == 'list':
                return json.dumps(records)
            if args[0] == 'show':
                return json.dumps(records[0])
            raise AssertionError(f'write or unexpected ruwana command: {args[0]}')

        adapter = Ruwana(Wiki(self.root), binary)
        with patch.object(Ruwana, '_snapshot_run', fake_run):
            snapshot = adapter.snapshot(['work/alpha'])
        self.assertEqual(len(snapshot.tasks), 1)
        task = snapshot.tasks[0]
        self.assertEqual(task.title, 'Deploy   the release')
        self.assertIsNone(task.description)
        self.assertEqual(task.status, 'open')
        self.assertEqual(task.source, ('wiki:work/alpha:meeting#task-synthetic-task',))
        self.assertEqual(calls[0][0], 'list')
        self.assertTrue(all(call[0] in ('list', 'show') for call in calls))
        self.assertEqual(task_path.read_text(), 'synthetic task state\n')

    def test_snapshot_fails_closed_on_duplicate_identity_or_missing_task_file(self):
        from pisar.ruwana import Ruwana
        task_path = self.alpha / '.ruwana' / 'missing.toml'
        binary = self.base / 'ruwana-test-double'
        binary.write_text('#!/bin/sh\nexit 99\n')
        binary.chmod(0o700)
        base_record = {'id': 'taskone', 'project': 'work/team/alpha', 'source': ['wiki:work/alpha:meeting#task-one'],
                       'title': 'A title', 'description': None, 'due': None, 'status': 'open',
                       'file_path': str(task_path)}
        adapter = Ruwana(Wiki(self.root), binary)
        with patch.object(Ruwana, '_snapshot_run', return_value=json.dumps([base_record, base_record])):
            from pisar.safety import WikiError
            with self.assertRaisesRegex(WikiError, 'duplicate|identity'):
                adapter.snapshot(['work/alpha'])
        with patch.object(Ruwana, '_snapshot_run', side_effect=[json.dumps([base_record]), json.dumps(base_record)]):
            with self.assertRaisesRegex(WikiError, 'file|missing|unexpected'):
                adapter.snapshot(['work/alpha'])


if __name__ == '__main__':
    unittest.main()
