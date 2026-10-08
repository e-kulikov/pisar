import json
import shutil
from .support import Fixture, commit_all, document, git, space


class ReadTests(Fixture):
    def test_help_runs_from_checkout(self):
        self.assertIn('capture', self.cli('--help').stdout)

    def test_discovery_includes_plain_and_local_submodule_spaces(self):
        module = self.add_submodule()
        found = self.data('spaces', '--include', 'work')['spaces']
        self.assertEqual({s['id'] for s in found}, {'alpha', 'beta', 'module'})
        self.assertEqual(next(s['path'] for s in found if s['id'] == 'module'), 'work/module')
        self.assertEqual(git(module, 'status', '--porcelain'), '')

    def test_selected_lexical_search_and_related_inventory(self):
        result = self.data('search', 'русский', '--include', 'work', '--space', 'work/beta')
        self.assertEqual(result['backend'], 'lexical')
        self.assertEqual([r['reference'] for r in result['results']], ['wiki:work/alpha:call-one'])
        inventory = self.data('inventory', '--space', 'work/beta', '--include', 'work')
        self.assertEqual([d['reference'] for d in inventory['documents']], ['wiki:work/alpha:call-one'])
        self.assertEqual(self.data('search', 'Private', '--include', 'work')['results'], [])

    def test_read_rereads_actual_file_and_survives_move(self):
        self.cli('read', 'wiki:work/alpha:call-one', '--include', 'work')
        (self.alpha / 'call.md').write_text(document(body='New current evidence.'))
        target = self.root / 'work/40-archives/renamed'
        target.parent.mkdir(parents=True)
        shutil.move(str(self.alpha), target)
        read = self.data('read', 'wiki:work/alpha:call-one', '--include', 'work')
        self.assertIn('New current evidence.', read['content'])
        self.assertEqual(read['path'], 'work/40-archives/renamed/call.md')

    def test_read_rejects_unselected_domain_and_traversal(self):
        self.cli('read', 'wiki:personal/home:private-note', '--include', 'work', ok=False)
        for path in ('../transcript.txt', '/etc/passwd', 'work/../personal/10-projects/home/note.md'):
            with self.subTest(path=path):
                self.cli('read', path, ok=False)

    def test_duplicate_space_ids_are_errors(self):
        space(self.root, 'work/duplicate', 'alpha')
        self.assertIn('duplicate', self.cli('spaces', ok=False).stderr.lower())

    def test_duplicate_document_ids_are_ambiguous(self):
        (self.alpha / 'duplicate.md').write_text(document())
        self.assertIn('duplicate', self.cli('read', 'wiki:work/alpha:call-one', ok=False).stderr.lower())
        self.cli('check', ok=False)

    def test_check_validates_actual_documents_not_only_spaces(self):
        (self.alpha / 'call.md').write_text(document().replace('schema_version = 1', 'schema_version = 9'))
        result = json.loads(self.cli('check', ok=False).stdout)
        self.assertFalse(result['ok'])
        self.assertTrue(any('schema_version' in e for e in result['errors']))

    def test_metadata_owner_related_ids_dates_and_placeholders(self):
        bad = [document(owner='work/beta'), document(ident='BAD:id'),
               document(related=('work/missing',)),
               document().replace('title = "Launch call"', 'title = "{{ title }}"'),
               document().replace('type = "meeting"', 'type = "meeting"\noccurred_at = "2026-10-04"'),
               document().replace('type = "meeting"', 'type = "meeting"\noccurred_at = 2026-10-04T11:00:00Z')]
        for content in bad:
            with self.subTest(content=content):
                (self.alpha / 'call.md').write_text(content)
                self.cli('check', ok=False)
        (self.alpha / 'call.md').write_text(document().replace('type = "meeting"',
                                                              'type = "meeting"\noccurred_at = "2026-10-04T11:00:00+02:00"'))
        self.assertTrue(self.data('check')['ok'])

    def test_frontmatter_delimiters_are_standalone_lines(self):
        content = document().replace('title = "Launch call"', 'title = "A +++ call"')
        (self.alpha / 'call.md').write_text(content)
        self.assertTrue(self.data('check')['ok'])
        (self.alpha / 'call.md').write_text(content.replace('\n+++\n\n', '\n+++not-a-delimiter\n\n'))
        self.cli('check', ok=False)

    def test_cross_domain_refs_and_missing_local_evidence_fail_check(self):
        for content in (document(related=('personal/home',)),
                        document(sources=('wiki:personal/home:private-note',)),
                        document(body='See wiki:personal/home:private-note.'),
                        document(sources=('sources/absent.txt#00:12',))):
            with self.subTest(content=content):
                (self.alpha / 'call.md').write_text(content)
                self.cli('check', ok=False)

    def test_inventory_includes_raw_sources_without_top_k(self):
        (self.alpha / 'sources').mkdir()
        for i in range(30):
            (self.alpha / 'sources' / f'raw-{i}.txt').write_text(f'Synthetic {i}')
        inv = self.data('inventory', '--space', 'work/alpha')
        self.assertEqual(len(inv['files']), 32)  # .wiki.toml, call, 30 sources

    def test_symlinks_never_expose_evidence(self):
        outside = self.external('Private escaped evidence')
        (self.alpha / 'escape.md').symlink_to(outside)
        self.cli('read', 'work/team/alpha/escape.md', ok=False)
        self.assertNotIn('Private escaped evidence', self.cli('search', 'Private', '--include', 'work').stdout)
        self.cli('check', ok=False)

    def test_roots_are_not_mixed_and_deleted_space_is_not_cached(self):
        self.cli('search', 'launch')
        other = self.base / 'other'
        other.mkdir()
        space(other, 'work/empty', 'empty')
        self.assertEqual(self.data('search', 'launch', root=other)['results'], [])
        shutil.rmtree(self.alpha)
        self.assertEqual(self.data('search', 'русский')['results'], [])
        self.assertFalse(self.state.exists())  # no readonly runtime/cache writes
