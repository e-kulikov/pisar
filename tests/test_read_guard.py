import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from pisar import read_guard


class ReadGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'wiki'
        self.root.mkdir()
        self.state = self.base / 'state'
        self.state.mkdir()
        self.plugin = self.state / 'agents' / 'claude' / 'plugin' / 'gen1'
        self.plugin.mkdir(parents=True)
        self.lessons = self.state / 'lessons' / 'batch1'
        self.lessons.mkdir(parents=True)

    def run_guard(self, payload):
        stdin = io.StringIO(json.dumps(payload))
        stdout = io.StringIO()
        stderr = io.StringIO()
        old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
        try:
            sys.stdin, sys.stdout, sys.stderr = stdin, stdout, stderr
            code = read_guard.run(self.root, self.state, self.plugin)
        finally:
            sys.stdin, sys.stdout, sys.stderr = old_stdin, old_stdout, old_stderr
        return code, stdout.getvalue(), stderr.getvalue()

    def test_malformed_json_or_missing_or_non_dir_cwd_exits_2_with_empty_stdout(self):
        stdin = io.StringIO("not-json")
        stdout, stderr = io.StringIO(), io.StringIO()
        old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
        try:
            sys.stdin, sys.stdout, sys.stderr = stdin, stdout, stderr
            code = read_guard.run(self.root, self.state, self.plugin)
        finally:
            sys.stdin, sys.stdout, sys.stderr = old_stdin, old_stdout, old_stderr
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), '')
        self.assertTrue(len(stderr.getvalue()) > 0)

        for bad_cwd in (None, '', 'relative/path', str(self.base / 'missing')):
            with self.subTest(bad_cwd=bad_cwd):
                payload = {'tool_name': 'Read', 'tool_input': {'file_path': 'doc.md'}}
                if bad_cwd is not None:
                    payload['cwd'] = bad_cwd
                code, out, err = self.run_guard(payload)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertTrue(len(err) > 0)

    def test_non_string_file_path_or_bad_tool_input_exits_2_with_empty_stdout(self):
        for bad_inp in (123, "string", [], {'file_path': 123}, {'file_path': None}, {'pattern': 123}):
            with self.subTest(bad_inp=bad_inp):
                code, out, err = self.run_guard({
                    'tool_name': 'Read', 'cwd': str(self.root),
                    'tool_input': bad_inp
                })
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertTrue(len(err) > 0)

    def test_unexpected_tool_name_exits_2(self):
        code, out, err = self.run_guard({
            'tool_name': 'UnknownTool', 'cwd': str(self.root),
            'tool_input': {'file_path': 'doc.md'}
        })
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertTrue(len(err) > 0)

    def test_read_wiki_root_file_exits_2_with_wiki_hint(self):
        doc = self.root / 'doc.md'
        doc.write_text('hello')
        code, out, err = self.run_guard({
            'tool_name': 'Read', 'cwd': str(self.root),
            'tool_input': {'file_path': 'doc.md'}
        })
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(err.strip(),
                         'pisar data must be read through pisar: use pisar read <root-relative-path>, pisar search <query>, or pisar inventory.')

    def test_grep_and_glob_wiki_root_targets_exit_2_with_wiki_hint(self):
        # Glob with default cwd (wiki root)
        code, out, err = self.run_guard({
            'tool_name': 'Glob', 'cwd': str(self.root),
            'tool_input': {'pattern': '*.md'}
        })
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(err.strip(),
                         'pisar data must be read through pisar: use pisar read <root-relative-path>, pisar search <query>, or pisar inventory.')

        # Grep with default cwd (wiki root)
        code, out, err = self.run_guard({
            'tool_name': 'Grep', 'cwd': str(self.root),
            'tool_input': {'pattern': 'term'}
        })
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(err.strip(),
                         'pisar data must be read through pisar: use pisar read <root-relative-path>, pisar search <query>, or pisar inventory.')

        # Grep pointing to directory in wiki
        code, out, err = self.run_guard({
            'tool_name': 'Grep', 'cwd': str(self.root),
            'tool_input': {'pattern': 'term', 'path': str(self.root)}
        })
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(err.strip(),
                         'pisar data must be read through pisar: use pisar read <root-relative-path>, pisar search <query>, or pisar inventory.')

        # Grep pointing to single file in wiki
        doc = self.root / 'doc.md'
        doc.write_text('content')
        code, out, err = self.run_guard({
            'tool_name': 'Grep', 'cwd': str(self.root),
            'tool_input': {'pattern': 'content', 'path': str(doc)}
        })
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(err.strip(),
                         'pisar data must be read through pisar: use pisar read <root-relative-path>, pisar search <query>, or pisar inventory.')

    def test_read_state_targets_exit_2_with_state_hint(self):
        inbox_file = self.state / 'inbox' / 'item.txt'
        inbox_file.parent.mkdir(parents=True, exist_ok=True)
        inbox_file.write_text('item')
        code, out, err = self.run_guard({
            'tool_name': 'Read', 'cwd': str(self.root),
            'tool_input': {'file_path': str(inbox_file)}
        })
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(err.strip(),
                         'pisar state must be read through pisar: use pisar lesson show --batch ID for lesson drafts or pisar inbox read --id ID for global inbox items.')

        # Sync state under <state>/roots/<ns>/sync
        sync_file = self.state / 'roots' / 'ns' / 'sync' / 'op.json'
        sync_file.parent.mkdir(parents=True, exist_ok=True)
        sync_file.write_text('{}')
        code, out, err = self.run_guard({
            'tool_name': 'Read', 'cwd': str(self.root),
            'tool_input': {'file_path': str(sync_file)}
        })
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(),
                         'pisar state must be read through pisar: use pisar lesson show --batch ID for lesson drafts or pisar inbox read --id ID for global inbox items.')

        # Grep on file inside state
        code, out, err = self.run_guard({
            'tool_name': 'Grep', 'cwd': str(self.root),
            'tool_input': {'pattern': 'item', 'path': str(inbox_file)}
        })
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(),
                         'pisar state must be read through pisar: use pisar lesson show --batch ID for lesson drafts or pisar inbox read --id ID for global inbox items.')

    def test_read_lesson_workspace_draft_exits_0(self):
        draft = self.lessons / 'draft.md'
        draft.write_text('note draft')
        code, out, err = self.run_guard({
            'tool_name': 'Read', 'cwd': str(self.root),
            'tool_input': {'file_path': str(draft)}
        })
        self.assertEqual(code, 0)
        self.assertEqual(out, '')
        self.assertEqual(err, '')

    def test_glob_and_grep_on_lessons_are_denied(self):
        for tool in ('Glob', 'Grep'):
            with self.subTest(tool=tool):
                code, out, err = self.run_guard({
                    'tool_name': tool, 'cwd': str(self.root),
                    'tool_input': {'pattern': '*.md', 'path': str(self.lessons)}
                })
                self.assertEqual(code, 2)
                self.assertEqual(err.strip(),
                                 'pisar state must be read through pisar: use pisar lesson show --batch ID for lesson drafts or pisar inbox read --id ID for global inbox items.')

    def test_read_glob_grep_active_plugin_file_exits_0(self):
        skill = self.plugin / 'SKILL.md'
        skill.write_text('skill')
        for tool, inp in (('Read', {'file_path': str(skill)}),
                          ('Glob', {'pattern': '*.md', 'path': str(self.plugin)}),
                          ('Grep', {'pattern': 'skill', 'path': str(skill)})):
            with self.subTest(tool=tool):
                code, out, err = self.run_guard({
                    'tool_name': tool, 'cwd': str(self.root), 'tool_input': inp
                })
                self.assertEqual(code, 0)
                self.assertEqual(out, '')
                self.assertEqual(err, '')

    def test_plugin_escape_via_dot_dot_symlink_or_sibling_gen_exits_2(self):
        # Traversal with ..
        code, out, err = self.run_guard({
            'tool_name': 'Read', 'cwd': str(self.plugin),
            'tool_input': {'file_path': '../other.txt'}
        })
        self.assertEqual(code, 2)

        # Symlink inside plugin pointing out
        target = self.state / 'secret.txt'
        target.write_text('secret')
        link = self.plugin / 'link.txt'
        link.symlink_to(target)
        code, out, err = self.run_guard({
            'tool_name': 'Read', 'cwd': str(self.plugin),
            'tool_input': {'file_path': str(link)}
        })
        self.assertEqual(code, 2)

        # Older plugin generation or staging dir
        old_gen = self.state / 'agents' / 'claude' / 'plugin' / 'gen0'
        old_gen.mkdir(parents=True, exist_ok=True)
        (old_gen / 'file.txt').write_text('old')
        code, out, err = self.run_guard({
            'tool_name': 'Read', 'cwd': str(self.root),
            'tool_input': {'file_path': str(old_gen / 'file.txt')}
        })
        self.assertEqual(code, 2)

    def test_forbidden_glob_patterns_exit_2(self):
        for pattern in ('/etc/*', '../foo', '~/secret'):
            with self.subTest(pattern=pattern):
                code, out, err = self.run_guard({
                    'tool_name': 'Glob', 'cwd': str(self.plugin),
                    'tool_input': {'pattern': pattern}
                })
                self.assertEqual(code, 2)

    def test_ancestor_search_denied(self):
        code, out, err = self.run_guard({
            'tool_name': 'Glob', 'cwd': str(self.base),
            'tool_input': {'pattern': '**/*.md', 'path': str(self.base)}
        })
        self.assertEqual(code, 2)

    def test_read_outside_target_exits_0(self):
        outside_file = self.base / 'outside.txt'
        outside_file.write_text('unprotected')
        code, out, err = self.run_guard({
            'tool_name': 'Read', 'cwd': str(self.base),
            'tool_input': {'file_path': str(outside_file)}
        })
        self.assertEqual(code, 0)
        self.assertEqual(out, '')
        self.assertEqual(err, '')

    def test_symlink_entering_wiki_root_denied_with_wiki_hint(self):
        doc = self.root / 'doc.md'
        doc.write_text('secret wiki doc')
        outside_link = self.base / 'link_to_doc.md'
        outside_link.symlink_to(doc)
        code, out, err = self.run_guard({
            'tool_name': 'Read', 'cwd': str(self.base),
            'tool_input': {'file_path': str(outside_link)}
        })
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(),
                         'pisar data must be read through pisar: use pisar read <root-relative-path>, pisar search <query>, or pisar inventory.')


if __name__ == '__main__':
    unittest.main()
