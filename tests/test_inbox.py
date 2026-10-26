import io
import os
import stat
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pisar import inbox
from pisar.safety import WikiError
from tests.support import CHECKOUT, Fixture, clean_environ, pisar_command


class InboxTests(Fixture):
    def setUp(self):
        super().setUp()
        self.inbox_dir = self.state / 'inbox'
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

    def test_validate_id_accepts_clean_filename(self):
        self.assertEqual(inbox.validate_id('notes.txt'), 'notes.txt')
        self.assertEqual(inbox.validate_id('meeting notes 2026.md'), 'meeting notes 2026.md')
        self.assertEqual(inbox.validate_id('résumé.txt'), 'résumé.txt')

    def test_validate_id_rejects_empty_whitespace_or_control_chars(self):
        for bad in ('', '   ', 'notes\n.txt', 'notes\r.txt', 'notes\x00.txt', 'notes\x1f.txt', 'notes\x7f.txt'):
            with self.subTest(bad=bad), self.assertRaises(WikiError):
                inbox.validate_id(bad)

    def test_validate_id_rejects_slashes_traversal_and_leading_dot(self):
        for bad in ('a/b.txt', 'a\\b.txt', '.hidden', '..', 'notes..txt', '../notes.txt'):
            with self.subTest(bad=bad), self.assertRaises(WikiError):
                inbox.validate_id(bad)

    def test_listing_returns_sorted_regular_files_skipping_dot_control_fifo_and_symlinks(self):
        (self.inbox_dir / 'b.txt').write_text('b')
        (self.inbox_dir / 'a.txt').write_text('a')
        (self.inbox_dir / '.dotfile').write_text('dot')
        (self.inbox_dir / 'bad\nname.txt').write_text('control char')
        (self.inbox_dir / 'subdir').mkdir()
        target = self.state / 'outside.txt'
        target.write_text('outside')
        (self.inbox_dir / 'sym.txt').symlink_to(target)
        try:
            os.mkfifo(self.inbox_dir / 'fifo_entry.txt')
        except (AttributeError, OSError):
            pass

        output = inbox.list_items(self.state)
        self.assertEqual(output, ['a.txt', 'b.txt'])

    def test_listing_when_inbox_directory_missing_returns_empty(self):
        missing_state = self.state / 'nonexistent'
        self.assertEqual(inbox.list_items(missing_state), [])

    def test_symlinked_inbox_directory_is_refused(self):
        sym_state = self.base / 'symstate'
        sym_state.mkdir()
        (sym_state / 'inbox').symlink_to(self.inbox_dir)
        with self.assertRaisesRegex(WikiError, 'symlink'):
            inbox.read_item(sym_state, 'a.txt')

    def test_read_item_exact_65536_bytes_has_no_truncation_marker(self):
        content = 'x' * 65536
        (self.inbox_dir / 'exact.txt').write_bytes(content.encode('utf-8'))
        text = inbox.read_item(self.state, 'exact.txt')
        self.assertEqual(text, content)
        self.assertNotIn('truncated', text)

    def test_read_item_65537_bytes_appends_exact_truncation_marker(self):
        content = 'y' * 65537
        (self.inbox_dir / 'large.txt').write_bytes(content.encode('utf-8'))
        text = inbox.read_item(self.state, 'large.txt')
        self.assertTrue(text.startswith('y' * 65536))
        self.assertTrue(text.endswith('\n[pisar: inbox item truncated; limit 65536 bytes]\n'))

    def test_read_item_chunked_reads_accumulate_to_bound(self):
        # Review Focus 1: short reads loop until 65537 or EOF
        content = 'z' * 70000
        (self.inbox_dir / 'chunked.txt').write_bytes(content.encode('utf-8'))
        real_read = os.read
        def chunked_read(fd, n):
            return real_read(fd, min(n, 1000))
        with patch('os.read', side_effect=chunked_read):
            text = inbox.read_item(self.state, 'chunked.txt')
            self.assertEqual(len(text.split('\n')[0]), 65536)
            self.assertTrue(text.endswith('\n[pisar: inbox item truncated; limit 65536 bytes]\n'))

    def test_read_item_multibyte_utf8_boundary_truncates_cleanly(self):
        # Review Focus 2: 65534 'a's + 1 3-byte '€' = 65537 bytes
        prefix = b'a' * 65534
        euro = '€'.encode('utf-8')
        (self.inbox_dir / 'multibyte.txt').write_bytes(prefix + euro)
        text = inbox.read_item(self.state, 'multibyte.txt')
        self.assertEqual(text, ('a' * 65534) + '\n[pisar: inbox item truncated; limit 65536 bytes]\n')

    def test_read_item_rejects_directory_symlink_or_fifo(self):
        target = self.state / 'outside.txt'
        target.write_text('outside')
        (self.inbox_dir / 'link.txt').symlink_to(target)
        with self.assertRaisesRegex(WikiError, 'regular'):
            inbox.read_item(self.state, 'link.txt')

        (self.inbox_dir / 'dir.txt').mkdir()
        with self.assertRaisesRegex(WikiError, 'regular'):
            inbox.read_item(self.state, 'dir.txt')

        fifo = self.inbox_dir / 'fifo.txt'
        try:
            os.mkfifo(fifo)
            has_fifo = True
        except (AttributeError, OSError):
            has_fifo = False
        if has_fifo:
            with self.assertRaisesRegex(WikiError, 'regular'):
                inbox.read_item(self.state, 'fifo.txt')

    def test_read_item_malformed_utf8_raises_wiki_error(self):
        (self.inbox_dir / 'bad.txt').write_bytes(b'\xff\xfe\x00')
        with self.assertRaises(WikiError):
            inbox.read_item(self.state, 'bad.txt')

    def test_read_item_empty_id_raises_wiki_error(self):
        with self.assertRaises(WikiError):
            inbox.read_item(self.state, '')

    def test_listing_skips_entries_rejected_by_validate_id(self):
        (self.inbox_dir / 'name..txt').write_text('dots')
        (self.inbox_dir / 'normal file.txt').write_text('normal')
        items = inbox.list_items(self.state)
        self.assertEqual(items, ['normal file.txt'])

    def test_read_item_dir_replacement_race(self):
        (self.inbox_dir / 'target.txt').write_text('inside')
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'target.txt').write_text('outside')

        real_open = os.open
        def racy_open(path, flags, *args, **kwargs):
            if str(path) == 'target.txt' or str(path).endswith('/target.txt'):
                renamed = self.inbox_dir.parent / 'inbox_old'
                if self.inbox_dir.exists() and not self.inbox_dir.is_symlink():
                    self.inbox_dir.rename(renamed)
                    self.inbox_dir.symlink_to(outside)
            return real_open(path, flags, *args, **kwargs)

        with patch('os.open', side_effect=racy_open):
            text = inbox.read_item(self.state, 'target.txt')
            self.assertEqual(text, 'inside')

    def test_read_item_wraps_fstat_and_read_os_errors_in_wiki_error(self):
        (self.inbox_dir / 'err.txt').write_text('data')
        with patch('os.fstat', side_effect=OSError('mocked fstat error')):
            with self.assertRaisesRegex(WikiError, 'cannot inspect'):
                inbox.read_item(self.state, 'err.txt')
        with patch('os.read', side_effect=OSError('mocked read error')):
            with self.assertRaisesRegex(WikiError, 'cannot read'):
                inbox.read_item(self.state, 'err.txt')

    def test_cli_dispatch_bypasses_ruwana_and_wiki(self):
        (self.inbox_dir / 'probe.txt').write_text('data\n')
        from pisar import cli
        with patch('pisar.settings.ruwana', side_effect=AssertionError('ruwana called')), \
             patch('pisar.spaces.Wiki', side_effect=AssertionError('Wiki called')):
            code = cli.main(['--state-dir', str(self.state), 'inbox', 'read'])
            self.assertEqual(code, 0)
            code = cli.main(['--state-dir', str(self.state), 'inbox', 'read', '--id', 'probe.txt'])
            self.assertEqual(code, 0)

    def test_cli_inbox_read_listing_and_content(self):
        (self.inbox_dir / 'hello.txt').write_text('world\n')
        env = clean_environ()
        from tests import support
        if not support.EXECUTABLE:
            env['PYTHONPATH'] = str(CHECKOUT)
        # Test listing
        res = subprocess.run([*pisar_command(), '--state-dir', str(self.state), 'inbox', 'read'],
                             capture_output=True, env=env, cwd=str(self.root), text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, 'hello.txt\n')

        # Test item read
        res = subprocess.run([*pisar_command(), '--state-dir', str(self.state), 'inbox', 'read', '--id', 'hello.txt'],
                             capture_output=True, env=env, cwd=str(self.root), text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, 'world\n')

        # Test empty --id fails
        res = subprocess.run([*pisar_command(), '--state-dir', str(self.state), 'inbox', 'read', '--id', ''],
                             capture_output=True, env=env, cwd=str(self.root), text=True)
        self.assertEqual(res.returncode, 1)

