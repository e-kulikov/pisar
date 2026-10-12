"""The [sync] configuration table: remote names and branches are validated before any Git call."""
import json

from .support import Fixture


class SyncConfigTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = self.base / 'config/pisar/config.toml'

    def write_config(self, text):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(text)

    def show(self):
        return json.loads(self.run_pisar('--root', self.root, 'config', 'show').stdout)['settings']

    def refused(self, text, *keys):
        self.write_config(text)
        p = self.run_pisar('--root', self.root, 'spaces', ok=False)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertTrue(p.stderr.startswith('pisar: '), p.stderr)
        self.assertIn(str(self.config), p.stderr)
        for key in keys:
            self.assertIn(key, p.stderr)
        self.assertEqual(p.stdout, '')

    def test_defaults_are_origin_and_no_branch(self):
        settings = self.show()
        self.assertEqual(settings['sync.remote'], {'value': 'origin', 'source': 'default'})
        self.assertEqual(settings['sync.branch'], {'value': None, 'source': 'default'})

    def test_file_values_are_reported_with_config_source(self):
        self.write_config('[sync]\nremote = "upstream"\nbranch = "feat/wiki-main"\n')
        settings = self.show()
        self.assertEqual(settings['sync.remote'], {'value': 'upstream', 'source': 'config'})
        self.assertEqual(settings['sync.branch'], {'value': 'feat/wiki-main', 'source': 'config'})

    def test_remote_must_be_a_configured_name_not_a_url_or_path(self):
        for value in ('https://example.invalid/wiki.git', 'ext::sh -c id', '../other', '/abs/path',
                      '-oProxyCommand=x', 'with space', 'a/b', '.hidden', ''):
            with self.subTest(value=value):
                self.refused(f'[sync]\nremote = "{value}"\n', 'sync.remote')

    def test_branch_must_be_a_short_refname(self):
        for value in ('-b', '--upload-pack=x', 'a..b', 'x.lock', 'with space', 'a//b', 'trail/', '/lead', ''):
            with self.subTest(value=value):
                self.refused(f'[sync]\nbranch = "{value}"\n', 'sync.branch')

    def test_unknown_keys_and_wrong_types_are_errors(self):
        self.refused('[sync]\nurl = "https://example.invalid/x"\n', 'sync.url')
        self.refused('[sync]\nremote = 3\n', 'sync.remote')
        self.refused('sync = "origin"\n', 'sync')
