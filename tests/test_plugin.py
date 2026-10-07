"""Extraction of the bundled Claude plugin: contents, atomicity, idempotence, hook."""
import json
import re
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


MANY = 8  # more generations than any retention bound could plausibly be


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

    def generations(self):
        return sorted(p.name for p in (self.target / 'plugin').iterdir() if not p.name.startswith('.'))

    def tree(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}

    def test_extracts_into_a_generation_named_by_version_and_content_hash(self):
        path = self.extract()
        self.assertEqual(path, self.target / 'plugin' / plugin.generation({}))
        self.assertRegex(path.name, rf'^{re.escape(__version__)}-[0-9a-f]{{8}}$')
        self.assertNotEqual(path, self.extract(model='other'))
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

    def test_changed_settings_publish_a_new_generation_and_never_touch_the_old_one(self):
        old = self.extract(model='first')
        before = self.tree(old)
        stamps = {n: (old / n).stat().st_mtime_ns for n in before}
        new = self.extract(model='second')
        self.assertNotEqual(old, new)
        self.assertEqual(frontmatter((new / 'agents/lesson-reviewer.md').read_text())['model'], 'second')
        self.assertEqual(self.tree(old), before)
        self.assertEqual({n: (old / n).stat().st_mtime_ns for n in before}, stamps)
        self.assertEqual(self.extract(model='first'), old)
        self.assertEqual(len(self.generations()), 2)

    def decoys(self):
        base = self.target / 'plugin'
        base.mkdir(parents=True, exist_ok=True)
        found = []
        for name in ('custom-deadbeef', '.build.tmp-unrelated', f'{__version__}-0123abcd.keep'):
            repo = base / name
            (repo / '.git').mkdir(parents=True)
            (repo / 'data.txt').write_text('user data')
            found.append(repo)
        return found

    def test_nothing_under_the_plugin_directory_is_ever_removed_or_moved(self):
        decoys = self.decoys()
        old = self.target / 'plugin' / '0.0.1-0123abcd'
        old.mkdir()
        (old / 'marker').write_text('x')
        paths = [self.extract(model=f'model-{i}') for i in range(MANY)]
        for path in paths + decoys + [old]:
            self.assertTrue(path.is_dir(), path)
        for repo in decoys:
            self.assertEqual((repo / 'data.txt').read_text(), 'user data')
            self.assertTrue((repo / '.git').is_dir())
        self.assertEqual((old / 'marker').read_text(), 'x')
        self.assertEqual(len(set(paths)), len(paths))

    def test_a_reader_on_the_first_generation_survives_many_newer_generations(self):
        first = self.extract(model='first')
        before = self.tree(first)
        for i in range(MANY):
            self.extract(model=f'newer-{i}')
        self.assertEqual(self.tree(first), before)

    def test_damaged_generation_is_replaced_by_a_fresh_one_and_left_untouched(self):
        damaged = self.extract()
        (damaged / 'hooks' / 'hooks.json').unlink()
        (damaged / 'extra.txt').write_text('mine')
        before = self.tree(damaged)
        fresh = self.extract()
        self.assertNotEqual(fresh, damaged)
        self.assertEqual(self.tree(damaged), before)
        self.assertEqual(self.tree(fresh), plugin.tree({}))
        self.assertEqual(self.extract(), fresh)
        self.assertEqual(len(self.generations()), 2)

    def test_extra_file_in_a_generation_counts_as_damage_not_as_a_reason_to_move_it(self):
        path = self.extract()
        (path / 'stray.txt').write_text('edited by hand')
        self.assertNotEqual(self.extract(), path)
        self.assertEqual((path / 'stray.txt').read_text(), 'edited by hand')
        self.assertTrue((path / 'hooks' / 'protect-descriptors.py').is_file())

    def test_publication_is_a_single_rename_to_a_new_path(self):
        self.extract(model='first')
        real = os.rename
        calls = []

        def spy(src, dst, *a, **k):
            calls.append((Path(dst).name, Path(dst).exists()))
            return real(src, dst, *a, **k)

        with mock.patch.object(plugin.os, 'rename', spy):
            new = self.extract(model='second')
        self.assertEqual(calls, [(new.name, False)])

    def test_failed_publication_leaves_every_existing_generation_intact(self):
        first = self.extract(model='first')
        damaged = self.extract(model='second')
        (damaged / 'hooks' / 'hooks.json').unlink()
        trees = {p: self.tree(p) for p in (first, damaged)}
        with mock.patch.object(plugin.os, 'rename', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                self.extract(model='second')
            with self.assertRaises(OSError):
                self.extract(model='third')
        self.assertEqual({p: self.tree(p) for p in trees}, trees)
        self.assertEqual(self.generations(), sorted(p.name for p in trees))

    def test_running_hook_never_misses_a_file_while_generations_change(self):
        first = self.extract(model='m0')
        seen = [first]
        stop = threading.Event()
        problems = []

        def reader():
            while not stop.is_set():
                for path in list(seen):
                    for name in ('hooks/protect-descriptors.py', 'hooks/hooks.json',
                                 'agents/lesson-reviewer.md', '.claude-plugin/plugin.json'):
                        try:
                            (path / name).read_bytes()
                        except OSError as error:
                            problems.append(f'{path.name}/{name}: {error}')

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for i in range(60):
                path = self.extract(model=f'm{i % 3}')
                if path not in seen:
                    seen.append(path)
        finally:
            stop.set()
            thread.join()
        self.assertEqual(problems, [])
        self.assertEqual(len(seen), 3)

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
        for name in self.generations():
            path = self.target / 'plugin' / name
            model = frontmatter((path / 'agents/lesson-reviewer.md').read_text())['model']
            self.assertEqual(self.tree(path), plugin.tree({'model': model}))
            self.assertEqual(name, plugin.generation({'model': model}))
        self.assertEqual([p.name for p in (self.target / 'plugin').iterdir() if p.name.startswith('.build')], [])

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
