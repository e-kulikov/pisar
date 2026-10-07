"""Extraction of the bundled Claude plugin: contents, atomicity, idempotence, hook."""
import json
import threading
from unittest import mock
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from pisar import __version__, claude_plugin as plugin
from .support import CHECKOUT


def frontmatter(text):
    head, _, _ = text[4:].partition('\n---\n')
    return dict(line.split(': ', 1) for line in head.splitlines())


def parse_reviewer(text):
    """(frontmatter dict, JSON example object) of a generated reviewer subagent file."""
    if not text.startswith('---\n') or '\n---\n' not in text[4:]:
        raise ValueError('missing frontmatter')
    head, _, body = text[4:].partition('\n---\n')
    meta = {}
    for line in head.splitlines():
        key, sep, value = line.partition(': ')
        if not sep:
            raise ValueError(f'bad frontmatter line: {line!r}')
        meta[key] = value
    start = body.find('{"schema_version"')
    if start < 0:
        raise ValueError('no JSON example')
    example, _ = json.JSONDecoder().raw_decode(body[start:])
    return meta, example


def contract_problems(example, model):
    """Differences between a reviewer JSON object and the agreed `lesson review` contract."""
    if not isinstance(example, dict):
        return ['not an object']
    problems = []
    if set(example) != {'schema_version', 'revision', 'sha256', 'verdict', 'findings', 'reviewer'}:
        problems.append(f'keys: {sorted(example)}')
    if example.get('schema_version') != 1:
        problems.append('schema_version')
    if example.get('verdict') != 'clear|concerns|block':
        problems.append('verdict')
    findings = example.get('findings')
    if not isinstance(findings, list) or not findings or not all(
            isinstance(f, dict) and set(f) == {'tier', 'category', 'excerpt', 'comment', 'suggestion'}
            and f['tier'] == 'severe|ask|warn' for f in findings):
        problems.append('findings')
    if example.get('reviewer') != {'model': model}:
        problems.append('reviewer')
    return problems


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='pisar-plugin-test-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.target = self.base / 'agents' / 'claude'

    def extract(self, **reviewer):
        return plugin.extract(self.target, reviewer)

    def tree(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}

    def test_extracts_into_a_directory_named_by_the_pisar_version(self):
        path = self.extract()
        self.assertEqual(path, self.target / 'plugin' / __version__)
        manifest = json.loads((path / '.claude-plugin' / 'plugin.json').read_text())
        self.assertEqual(manifest['version'], __version__)
        self.assertEqual(manifest['name'], 'pisar')

    def test_contains_three_skills_hook_and_reviewer(self):
        path = self.extract()
        for name in ('pisar', 'lessons', 'research'):
            text = (path / 'skills' / name / 'SKILL.md').read_text()
            self.assertEqual(frontmatter(text)['name'], name)
            self.assertGreater(len(frontmatter(text)['description']), 30)
        self.assertTrue((path / 'hooks' / 'hooks.json').is_file())
        self.assertTrue((path / 'agents' / 'lesson-reviewer.md').is_file())

    def test_pisar_skill_is_the_single_source_skill_text(self):
        path = self.extract()
        self.assertEqual((path / 'skills' / 'pisar' / 'SKILL.md').read_text(encoding='utf-8'),
                         (CHECKOUT / 'pisar/skill.md').read_text(encoding='utf-8'))

    def test_reviewer_uses_defaults_and_configured_model_and_effort(self):
        text = (self.extract() / 'agents' / 'lesson-reviewer.md').read_text()
        self.assertEqual((frontmatter(text)['model'], frontmatter(text)['effort']), ('opus', 'high'))
        text = (self.extract(model='some-model[1m]', effort='max') / 'agents' / 'lesson-reviewer.md').read_text()
        self.assertEqual((frontmatter(text)['model'], frontmatter(text)['effort']), ('some-model[1m]', 'max'))
        self.assertEqual(json.loads((self.target / 'plugin' / __version__ / '.claude-plugin/plugin.json')
                                    .read_text())['version'], __version__)

    def reviewer(self, **options):
        return (self.extract(**options) / 'agents' / 'lesson-reviewer.md').read_text()

    def test_reviewer_file_parses_as_a_subagent_with_the_configured_settings(self):
        meta = parse_reviewer(self.reviewer(model='m-1', effort='low'))[0]
        self.assertEqual({k: meta[k] for k in ('name', 'model', 'effort', 'tools')},
                         {'name': 'lesson-reviewer', 'model': 'm-1', 'effort': 'low', 'tools': 'Read'})
        self.assertGreater(len(meta['description']), 30)

    def test_reviewer_example_is_valid_json_matching_the_contract(self):
        example = parse_reviewer(self.reviewer(model='m-1'))[1]
        self.assertEqual(contract_problems(example, 'm-1'), [])

    def test_contract_validator_rejects_malformed_examples(self):
        good = parse_reviewer(self.reviewer())[1]
        broken = []
        for key in good:
            broken.append({k: v for k, v in good.items() if k != key})
        broken += [{**good, 'extra': 1}, {**good, 'schema_version': 2},
                   {**good, 'verdict': 'fine'}, {**good, 'findings': {}},
                   {**good, 'findings': [{'tier': 'severe|ask|warn'}]},
                   {**good, 'findings': [{**good['findings'][0], 'extra': 1}]},
                   {**good, 'reviewer': {}}, {**good, 'reviewer': {'model': 'other'}}, []]
        for example in broken:
            with self.subTest(example=example):
                self.assertTrue(contract_problems(example, 'opus'))

    def test_reviewer_parser_rejects_a_file_without_frontmatter_or_json(self):
        for text in ('no frontmatter', '---\nname: x\n---\nno json here', '---\nname x\n---\n{}'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_reviewer(text)

    def test_extraction_is_idempotent_and_leaves_files_untouched(self):
        path = self.extract()
        before = self.tree(path)
        stamps = {p: (path / p).stat().st_mtime_ns for p in before}
        self.assertEqual(self.extract(), path)
        self.assertEqual(self.tree(path), before)
        self.assertEqual({p: (path / p).stat().st_mtime_ns for p in before}, stamps)

    def test_changed_reviewer_settings_replace_the_tree_and_leave_no_leftovers(self):
        path = self.extract()
        (path / 'stray.txt').write_text('edited by hand')
        self.extract(model='other')
        self.assertFalse((path / 'stray.txt').exists())
        self.assertEqual(frontmatter((path / 'agents/lesson-reviewer.md').read_text())['model'], 'other')
        self.assertEqual(sorted(p.name for p in (self.target / 'plugin').iterdir() if p.name != '.lock'), [__version__])

    def test_damaged_tree_is_repaired(self):
        path = self.extract()
        (path / 'hooks' / 'hooks.json').unlink()
        self.extract()
        self.assertTrue((path / 'hooks' / 'hooks.json').is_file())

    def test_other_versions_stay_and_partial_leftovers_are_ignored(self):
        old = self.target / 'plugin' / '0.0.1'
        old.mkdir(parents=True)
        (old / 'marker').write_text('x')
        (self.target / 'plugin' / f'.{__version__}.tmp-123').mkdir()
        self.extract()
        self.assertTrue((old / 'marker').exists())

    def test_failed_replacement_keeps_the_installed_tree(self):
        path = self.extract(model='first')
        real = os.replace
        calls = []

        def failing(src, dst, *a, **k):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError('simulated failure')
            return real(src, dst, *a, **k)

        with mock.patch.object(plugin.os, 'replace', failing):
            with self.assertRaises(OSError):
                self.extract(model='second')
        self.assertEqual(frontmatter((path / 'agents/lesson-reviewer.md').read_text())['model'], 'first')
        self.assertEqual(sorted(p.name for p in (self.target / 'plugin').iterdir() if p.name != '.lock'),
                         [__version__])

    def test_interrupted_replacement_is_recovered_from_the_retired_tree(self):
        path = self.extract(model='first')
        retired = path.with_name(f'.{__version__}.old-1')
        path.rename(retired)  # a crash between the two renames
        self.assertFalse(path.exists())
        self.extract(model='first')
        self.assertEqual(frontmatter((path / 'agents/lesson-reviewer.md').read_text())['model'], 'first')
        self.assertFalse(retired.exists())

    def test_interrupted_replacement_restores_the_retired_tree_before_replacing(self):
        path = self.extract(model='first')
        retired = path.with_name(f'.{__version__}.old-1')
        path.rename(retired)
        with mock.patch.object(plugin, 'tree', side_effect=OSError('cannot build')):
            with self.assertRaises(OSError):
                self.extract(model='second')
        self.assertEqual(frontmatter((path / 'agents/lesson-reviewer.md').read_text())['model'], 'first')

    def test_concurrent_extractors_are_serialized_and_leave_one_complete_tree(self):
        errors = []

        def worker(model):
            try:
                for _ in range(5):
                    self.extract(model=model)
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(f'm{i}',)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        path = self.target / 'plugin' / __version__
        model = frontmatter((path / 'agents/lesson-reviewer.md').read_text())['model']
        self.assertEqual(self.tree(path), plugin.tree({'model': model}))
        self.assertEqual(sorted(p.name for p in (self.target / 'plugin').iterdir() if p.name != '.lock'),
                         [__version__])

    def test_hook_json_matches_write_edit_and_multiedit(self):
        hooks = json.loads((self.extract() / 'hooks' / 'hooks.json').read_text())
        entries = hooks['hooks']['PreToolUse']
        self.assertEqual(len(entries), 1)
        self.assertEqual(set(entries[0]['matcher'].split('|')), {'Write', 'Edit', 'MultiEdit'})
        self.assertIn('${CLAUDE_PLUGIN_ROOT}', entries[0]['hooks'][0]['command'])


class HookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='pisar-hook-test-')
        self.addCleanup(temp.cleanup)
        self.path = plugin.extract(Path(temp.name) / 'agents' / 'claude', {})

    def run_hook(self, tool, file_path, **extra):
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': tool,
                   'tool_input': {'file_path': file_path, **extra}}
        p = subprocess.run([sys.executable, '-I', str(self.path / 'hooks' / 'protect-descriptors.py')],
                           input=json.dumps(payload), text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout) if p.stdout.strip() else None

    def test_denies_direct_edits_of_descriptors_with_a_pointer_to_commands(self):
        for tool in ('Write', 'Edit', 'MultiEdit'):
            for name in ('.wiki.toml', '.domain.toml'):
                with self.subTest(tool=tool, name=name):
                    out = self.run_hook(tool, f'/some/where/acme/10-projects/x/{name}')
                    decision = out['hookSpecificOutput']
                    self.assertEqual(decision['hookEventName'], 'PreToolUse')
                    self.assertEqual(decision['permissionDecision'], 'deny')
                    self.assertIn('pisar domain', decision['permissionDecisionReason'])
                    self.assertIn('pisar space', decision['permissionDecisionReason'])

    def test_other_files_are_not_touched(self):
        for name in ('README.md', 'wiki.toml', '.wiki.toml.bak', 'domain.toml', '.domain.toml.md'):
            with self.subTest(name):
                self.assertIsNone(self.run_hook('Write', f'/x/{name}'))

    def test_malformed_input_never_blocks(self):
        p = subprocess.run([sys.executable, '-I', str(self.path / 'hooks' / 'protect-descriptors.py')],
                           input='not json', text=True, capture_output=True)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ''))


class PackagedFilesTests(unittest.TestCase):
    def test_packaged_resources_are_declared_for_installs_and_zipapp(self):
        import tomllib
        data = tomllib.loads((CHECKOUT / 'pyproject.toml').read_text())
        patterns = data['tool']['setuptools']['package-data']['pisar']
        self.assertTrue(any(p.startswith('plugin/claude') for p in patterns), patterns)


if __name__ == '__main__':
    unittest.main()
