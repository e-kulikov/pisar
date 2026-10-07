"""Domains: top-level directories marked by .domain.toml, nothing hardcoded."""
import json
from .support import Fixture, commit_all, document, domain, space


class DomainDiscoveryTests(Fixture):
    def spaces(self, *args):
        return self.data('spaces', *args)['spaces']

    def test_spaces_report_domain_and_address(self):
        found = {s['id']: s for s in self.spaces()}
        self.assertEqual(found['alpha']['domain'], 'work')
        self.assertEqual(found['alpha']['address'], 'work/alpha')
        self.assertEqual(found['home']['domain'], 'personal')
        self.assertEqual(found['home']['address'], 'personal/home')
        self.assertEqual(found['home']['path'], 'personal/10-projects/home')

    def test_any_marked_directory_is_a_domain_and_unmarked_ones_are_not(self):
        domain(self.root, 'acme', 'Acme Corp')
        space(self.root, 'acme/20-areas/ops', 'ops', kind='area')
        (self.root / 'docs').mkdir()
        space(self.root / 'docs', 'guide', 'guide')  # .wiki.toml outside any domain
        (self.personal.parents[1] / '.domain.toml').unlink()
        commit_all(self.root)
        self.assertEqual(sorted(s['address'] for s in self.spaces()),
                         ['acme/ops', 'work/alpha', 'work/beta'])
        self.assertTrue(self.data('check')['ok'])

    def test_invalid_markers_are_reported_with_their_path(self):
        bad = {
            'id differs from directory': 'schema_version = 1\nid = "other"\ntitle = "Acme"\n',
            'wrong schema': 'schema_version = 2\nid = "acme"\ntitle = "Acme"\n',
            'schema as string': 'schema_version = "1"\nid = "acme"\ntitle = "Acme"\n',
            'missing title': 'schema_version = 1\nid = "acme"\n',
            'blank title': 'schema_version = 1\nid = "acme"\ntitle = " "\n',
            'unknown key': 'schema_version = 1\nid = "acme"\ntitle = "Acme"\nowner = "x"\n',
            'unknown layout kind': 'schema_version = 1\nid = "acme"\ntitle = "Acme"\n[layout]\nmisc = "x"\n',
            'layout escapes': 'schema_version = 1\nid = "acme"\ntitle = "Acme"\n[layout]\nproject = "../x"\n',
            'layout not string': 'schema_version = 1\nid = "acme"\ntitle = "Acme"\n[layout]\narea = 3\n',
            'layout collision': 'schema_version = 1\nid = "acme"\ntitle = "Acme"\n[layout]\narea = "10-projects"\n',
            'min_segments zero': 'schema_version = 1\nid = "acme"\ntitle = "Acme"\n[ids]\nmin_segments = 0\n',
            'min_segments bool': 'schema_version = 1\nid = "acme"\ntitle = "Acme"\n[ids]\nmin_segments = true\n',
            'aliases not strings': 'schema_version = 1\nid = "acme"\ntitle = "Acme"\n[sensitive]\naliases = [1]\n',
            'unknown sensitive key': 'schema_version = 1\nid = "acme"\ntitle = "Acme"\n[sensitive]\nclients = []\n',
            'invalid toml': 'schema_version = 1\nid = "acme\n',
        }
        marker = self.root / 'acme/.domain.toml'
        marker.parent.mkdir()
        for name, content in bad.items():
            with self.subTest(name):
                marker.write_text(content)
                failed = self.cli('spaces', ok=False)
                self.assertIn('acme/.domain.toml', failed.stderr)
                result = json.loads(self.cli('check', ok=False).stdout)
                self.assertTrue(any('acme/.domain.toml' in e for e in result['errors']), result)

    def test_full_marker_is_accepted(self):
        domain(self.root, 'acme', 'Acme Corp', extra=(
            '[layout]\nproject = "p"\narea = "a"\nresource = "r"\narchive = "z"\ninbox = "in"\n'
            '[ids]\nmin_segments = 2\n[sensitive]\naliases = ["ACME Inc"]\nterms = ["Rocket"]\n'))
        space(self.root, 'acme/p/launch-plan', 'launch-plan')
        self.assertIn('acme/launch-plan', [s['address'] for s in self.spaces()])
        from pisar.spaces import Wiki
        acme = Wiki(self.root).domains['acme']
        self.assertEqual(acme.title, 'Acme Corp')
        self.assertEqual(acme.layout['inbox'], 'in')
        self.assertEqual(acme.min_segments, 2)
        self.assertEqual((acme.aliases, acme.terms), (('ACME Inc',), ('Rocket',)))

    def test_layout_defaults(self):
        from pisar.spaces import Wiki
        work = Wiki(self.root).domains['work']
        self.assertEqual(work.layout, dict(project='10-projects', area='20-areas', resource='30-resources',
                                           archive='40-archives', inbox='inbox'))
        self.assertEqual(work.min_segments, 1)
        self.assertEqual((work.aliases, work.terms), ((), ()))

    def test_symlinked_marker_or_domain_directory_is_refused(self):
        outside = self.base / 'outside-domain'
        outside.mkdir()
        (outside / '.domain.toml').write_text('schema_version = 1\nid = "linked"\ntitle = "Linked"\n')
        space(outside, 'sub/leak', 'leak')
        (self.root / 'linked').symlink_to(outside, target_is_directory=True)
        failed = self.cli('spaces', ok=False)
        self.assertIn('linked', failed.stderr)
        (self.root / 'linked').unlink()
        (self.root / 'linked').mkdir()
        (self.root / 'linked/.domain.toml').symlink_to(outside / '.domain.toml')
        self.assertIn('linked/.domain.toml', self.cli('spaces', ok=False).stderr)

    def test_domain_directory_cannot_itself_be_a_space(self):
        space(self.root, 'work', 'work')
        self.assertIn('work/.wiki.toml', self.cli('spaces', ok=False).stderr)


class AddressTests(Fixture):
    def setUp(self):
        super().setUp()
        domain(self.root, 'acme', 'Acme Corp')
        self.acme = space(self.root, 'acme/10-projects/alpha', 'alpha')
        (self.acme / 'call.md').write_text(document(owner='acme/alpha', body='Acme-only words.'))
        commit_all(self.root)

    def test_space_ids_are_unique_per_domain_only(self):
        self.assertEqual(sorted(s['address'] for s in self.data('spaces')['spaces'] if s['id'] == 'alpha'),
                         ['acme/alpha', 'work/alpha'])
        self.assertTrue(self.data('check')['ok'])
        work = self.data('read', 'wiki:work/alpha:call-one')
        acme = self.data('read', 'wiki:acme/alpha:call-one')
        self.assertEqual((work['path'], acme['path']), ('work/team/alpha/call.md', 'acme/10-projects/alpha/call.md'))
        self.assertEqual(acme['reference'], 'wiki:acme/alpha:call-one')
        self.assertEqual([r['reference'] for r in self.data('search', 'words', '--space', 'acme/alpha')['results']],
                         ['wiki:acme/alpha:call-one'])
        self.assertEqual([d['reference'] for d in self.data('inventory', '--space', 'work/alpha')['documents']],
                         ['wiki:work/alpha:call-one'])

    def test_duplicate_id_within_a_domain_is_an_error_even_when_archived(self):
        space(self.root, 'acme/40-archives/old-alpha', 'alpha', status='archived')
        self.assertIn('duplicate space id: acme/alpha', self.cli('spaces', ok=False).stderr)

    def test_bare_space_ids_are_refused_where_addresses_are_expected(self):
        self.assertIn('domain/id', self.cli('read', 'wiki:alpha:call-one', ok=False).stderr)
        self.cli('search', 'words', '--space', 'alpha', ok=False)
        self.cli('inventory', '--space', 'alpha', ok=False)
        self.cli('capture', '--space', 'alpha', '--id', 'bare', '--source', self.external(), ok=False)
        self.cli('capture', '--space', 'nowhere/alpha', '--id', 'bare', '--source', self.external(), ok=False)
        self.assertFalse(list(self.root.rglob('bare.md')))

    def test_front_matter_space_ids_are_addresses(self):
        (self.acme / 'call.md').write_text(document(owner='alpha'))
        self.cli('check', ok=False)
        (self.acme / 'call.md').write_text(document(owner='work/alpha'))
        self.cli('check', ok=False)

    def test_legacy_references_are_reported_by_check(self):
        (self.acme / 'call.md').write_text(document(owner='acme/alpha', body='See wiki:alpha:call-one.'))
        result = json.loads(self.cli('check', ok=False).stdout)
        self.assertTrue(any('wiki:alpha:call-one' in e for e in result['errors']), result)

    def test_domain_is_the_confidentiality_boundary(self):
        for content in (document(owner='acme/alpha', related=('work/beta',)),
                        document(owner='acme/alpha', body='See wiki:work/alpha:call-one.'),
                        document(owner='acme/alpha', sources=('wiki:work/alpha:call-one',))):
            with self.subTest(content=content):
                (self.acme / 'call.md').write_text(content)
                result = json.loads(self.cli('check', ok=False).stdout)
                self.assertTrue(any('cross-domain' in e for e in result['errors']), result)
        failed = self.cli('capture', '--space', 'work/alpha', '--id', 'leak',
                          '--source', self.acme / 'call.md', ok=False)
        self.assertIn('source domain', failed.stderr)
        self.assertFalse(list(self.root.rglob('leak.md')))

    def test_capture_writes_address_metadata_and_a_domain_qualified_journal(self):
        self.data('capture', '--space', 'acme/alpha', '--id', 'acme-capture', '--source', self.external())
        meta = self.data('read', 'wiki:acme/alpha:acme-capture')['metadata']
        self.assertEqual(meta['space_ids'], ['acme/alpha'])
        self.assertTrue(list(self.state.rglob('operations/capture-acme-alpha-acme-capture.json')))
