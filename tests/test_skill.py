import re
import unittest

from pisar.cli import parser
from .support import CHECKOUT, Fixture, clean_environ


def commands():
    """Every top-level command and triage action the parser defines."""
    top = next(a for a in parser()._actions if a.dest == 'command')
    triage = next(a for a in top.choices['triage']._actions if a.dest == 'action')
    return sorted(top.choices), sorted(triage.choices)


class SkillTests(Fixture):
    def skill(self, **env):
        return self.run_pisar('--skill', env=clean_environ(
            PISAR_ROOT=str(self.base / 'absent'), HOME=str(self.base / 'no-home'),
            XDG_DATA_HOME=str(self.base / 'no-data'), **env))

    def test_skill_needs_no_root_or_state_and_writes_only_stdout(self):
        p = self.skill()
        self.assertEqual(p.stderr, '')
        self.assertFalse((self.base / 'no-data').exists())
        self.assertTrue(p.stdout.endswith('\n'))
        self.assertGreater(len(p.stdout.splitlines()), 40)

    def test_skill_is_a_skill_document_with_frontmatter(self):
        text = self.skill().stdout
        match = re.match(r'---\nname: pisar\ndescription: (.+)\n---\n', text)
        self.assertIsNotNone(match, text[:200])
        self.assertGreater(len(match.group(1)), 40)

    def test_skill_covers_every_command_and_triage_action(self):
        text = self.skill().stdout
        top, actions = commands()
        for name in top:
            self.assertIn(f'pisar {name}', text, name)
        for name in actions:
            self.assertIn(f'triage {name}', text, name)

    def test_skill_documents_every_global_option_and_variable(self):
        text = self.skill().stdout
        for token in ('--root', '--state-dir', '--ruwana', 'PISAR_ROOT',
                      'PISAR_STATE_DIR', 'PISAR_RUWANA_BIN', '--scope', '--version'):
            self.assertIn(token, text, token)

    def test_skill_is_generic_and_leaks_nothing_local(self):
        text = self.skill().stdout
        for pattern in (r'/home/', r'/Users/', r'ekulikov', r'godel', r'@[\w-]+\.(com|org|net)',
                        r'herdr', r'\bcodex\b', r'~/'):
            self.assertIsNone(re.search(pattern, text, re.I), pattern)

    def test_skill_matches_the_packaged_source_file(self):
        self.assertEqual(self.skill().stdout, (CHECKOUT / 'pisar/skill.md').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
