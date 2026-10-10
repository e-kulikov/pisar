"""The bundled skills for the Codex agent: the Claude skills, adapted to tools and written into CODEX_HOME."""
import os
from pathlib import Path
import re
import stat
import unittest

from pisar import codex_plugin
from pisar.safety import WikiError
from .support import CHECKOUT, Fixture


class CodexSkillTests(Fixture):
    def claude(self, name):
        return (CHECKOUT / 'pisar/plugin/claude/skills' / name / 'SKILL.md').read_text(encoding='utf-8')

    def test_three_skills_with_frontmatter_and_the_same_names_as_claude(self):
        texts = codex_plugin.skill_texts()
        self.assertEqual(sorted(texts), ['lessons', 'pisar', 'research'])
        for name, text in texts.items():
            with self.subTest(skill=name):
                match = re.match(rf'---\nname: {name}\ndescription: .+\n---\n', text)
                self.assertIsNotNone(match, text[:120])

    def test_every_skill_has_the_tools_section_after_its_frontmatter(self):
        for name, text in codex_plugin.skill_texts().items():
            with self.subTest(skill=name):
                self.assertEqual(text.count('## Tools in this session'), 1)
                self.assertGreater(text.index('## Tools in this session'), text.index('\n---\n'))

    def test_lessons_use_the_lesson_review_and_lesson_accept_tools(self):
        text = codex_plugin.skill_texts()['lessons']
        self.assertIn('`lesson_review`', text)
        self.assertIn('`lesson_accept`', text)
        self.assertNotIn('pisar:lesson-reviewer', text)
        self.assertNotIn('`pisar lesson accept`', text)
        self.assertNotIn('pisar lesson accept --batch', text)
        self.assertIn('`pisar lesson show --batch ID`', text)
        self.assertIn("Accepting is the user's act. The `lesson_accept` tool always asks", text)
        self.assertIn('`--skip-review` only when the user', text)

    def test_lessons_keep_every_other_step_of_the_claude_skill(self):
        claude = self.claude('lessons')
        text = codex_plugin.skill_texts()['lessons']
        for step in range(1, 9):
            self.assertIn(f'\n{step}. ', claude)
            self.assertIn(f'\n{step}. ', text)
        for line in claude.splitlines():
            if 'pisar lesson accept' in line or 'pisar:lesson-reviewer' in line:
                continue  # replaced by the tool names on purpose
            if line.startswith(('- ', '1. ', '2. ', '3. ', '4. ', '6. ', '8. ')):
                self.assertIn(line, text, line)

    def test_research_skill_is_the_claude_text_with_the_tools_section(self):
        text = codex_plugin.skill_texts()['research']
        self.assertIn(self.claude('research').split('\n---\n', 1)[1].strip(), text)

    def test_pisar_skill_is_the_packaged_skill_document(self):
        text = codex_plugin.skill_texts()['pisar']
        self.assertIn('# Working with pisar', text)
        self.assertIn('pisar mcp-serve', text)

    def test_write_skills_publishes_each_file_and_leaves_no_temporary_files(self):
        config = self.base / 'codex-home'
        config.mkdir()
        directory = codex_plugin.write_skills(config)
        self.assertEqual(directory, config / 'skills')
        for name, text in codex_plugin.skill_texts().items():
            path = directory / name / 'SKILL.md'
            self.assertEqual(path.read_text(encoding='utf-8'), text)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        leftovers = [p for p in directory.rglob('*') if p.name.startswith('.')]
        self.assertEqual(leftovers, [])

    def test_rewriting_replaces_an_edited_skill(self):
        config = self.base / 'codex-home'
        config.mkdir()
        codex_plugin.write_skills(config)
        path = config / 'skills/research/SKILL.md'
        path.write_text('edited')
        codex_plugin.write_skills(config)
        self.assertEqual(path.read_text(encoding='utf-8'), codex_plugin.skill_texts()['research'])

    def test_a_changed_bundled_lesson_skill_is_an_error_not_a_silent_copy(self):
        original = codex_plugin._resource
        try:
            codex_plugin._resource = lambda name: original(name).replace('5. Review is mandatory:', 'x')
            with self.assertRaisesRegex(WikiError, 'bundled skill changed'):
                codex_plugin.skill_texts()
        finally:
            codex_plugin._resource = original


if __name__ == '__main__':
    unittest.main()
