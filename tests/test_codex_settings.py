"""Codex settings: agent name, role defaults, default_agent and research_agent in the config file."""
import json
import os
from unittest import mock

from pisar import config, settings
from .support import Fixture


class CodexSettingsTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = self.base / 'config/pisar/config.toml'
        self.patch = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.base / 'config')})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write_config(self, text):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(text)

    def test_codex_is_a_supported_agent_and_default_agent(self):
        self.assertIn('codex', config.AGENTS)
        self.write_config('default_agent = "codex"\n')
        self.assertEqual(settings.default_agent(), ('codex', 'config'))

    def test_codex_role_defaults_are_overridable_and_main_session_has_none(self):
        self.assertEqual(settings.agent_options('codex'), {'model': None, 'effort': None})
        reviewer = settings.agent_options('codex', 'reviewer')
        researcher = settings.agent_options('codex', 'researcher')
        self.assertTrue(reviewer['model'] and reviewer['effort'])
        self.assertTrue(researcher['model'] and researcher['effort'])
        self.assertNotEqual(reviewer, settings.agent_options('claude', 'reviewer'))
        self.write_config('[agents.codex.reviewer]\nmodel = "my-model"\neffort = "low"\n')
        self.assertEqual(settings.agent_options('codex', 'reviewer'), {'model': 'my-model', 'effort': 'low'})
        self.assertEqual(settings.agent_options('claude', 'reviewer'), {'model': 'opus', 'effort': 'high'})

    def test_codex_main_session_model_and_effort_come_from_the_file(self):
        self.write_config('[agents.codex]\nmodel = "gpt-x"\neffort = "high"\n')
        self.assertEqual(settings.agent_options('codex'), {'model': 'gpt-x', 'effort': 'high'})

    def test_research_agent_defaults_to_claude_and_accepts_only_known_agents(self):
        self.assertEqual(settings.research_agent(), ('claude', 'default'))
        self.write_config('research_agent = "codex"\n')
        self.assertEqual(settings.research_agent(), ('codex', 'config'))
        self.write_config('research_agent = "gemini"\n')
        with self.assertRaisesRegex(Exception, 'research_agent: unsupported agent: gemini'):
            settings.research_agent()

    def test_show_reports_codex_options_and_research_agent_with_sources(self):
        self.write_config('research_agent = "codex"\n[agents.codex]\nmodel = "m"\n')
        result = json.loads(json.dumps(settings.show({})))['settings']
        self.assertEqual(result['research_agent'], {'value': 'codex', 'source': 'config'})
        self.assertEqual(result['agents.codex.model'], {'value': 'm', 'source': 'config'})
        self.assertIn('agents.codex.reviewer.effort', result)
        self.assertIn('agents.codex.researcher.model', result)
