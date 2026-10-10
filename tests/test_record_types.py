"""Per-space record types declared in .wiki.toml, on temporary fixtures only."""
import json
import unittest

from pisar.operations import Runtime
from pisar.safety import WikiError
from pisar.spaces import Wiki
from .support import Fixture, commit_all, git, space
from .test_spacecmd import SpaceCommandFixture


MARKER = 'schema_version = 1\nid = "alpha"\nkind = "project"\nstatus = "active"\n'
IDEA = '[types.recipe-idea]\ndescription = "One idea for a recipe to try later"\n'


def document(ident, doc_type, extra='', owner='work/alpha', body='Body text.'):
    return ('+++\nschema_version = 1\n'
            f'id = "{ident}"\ntype = {json.dumps(doc_type)}\ntitle = "Idea"\n'
            f'space_ids = [{json.dumps(owner)}]\nsources = []\n{extra}+++\n\n{body}\n')


def commit_changes(root):
    if git(root, 'status', '--porcelain'):
        commit_all(root)


class RecordTypeFixture(Fixture):
    def declare(self, text, path=None):
        """Replace the alpha space marker with MARKER plus TEXT, committed."""
        path = path or self.alpha
        target = path / '.wiki.toml'
        target.write_text(MARKER.replace('"alpha"', f'"{path.name}"') + text)
        commit_changes(self.root)

    def wiki_errors(self):
        return Wiki(self.root).errors

    def write(self, name, text, path=None):
        (path or self.alpha / 'docs').mkdir(parents=True, exist_ok=True)
        target = (path or self.alpha / 'docs') / f'{name}.md'
        target.write_text(text)
        return target

    def check(self, ok=True):
        return self.data('check', ok=ok)

    def errors(self):
        return self.check(ok=False)['errors']


class DeclaredTypeTests(RecordTypeFixture):
    def test_declared_type_is_accepted(self):
        self.declare(IDEA)
        self.write('idea', document('idea', 'recipe-idea'))
        self.assertTrue(self.check()['ok'])

    def test_system_types_need_no_declaration(self):
        self.declare('')
        self.write('meet', document('meet', 'meeting'))
        self.write('src', document('src', 'source'))
        self.assertTrue(self.check()['ok'])

    def test_undeclared_type_is_rejected_and_lists_the_declared_types(self):
        self.declare(IDEA)
        self.write('movie', document('movie', 'movie'))
        message = '\n'.join(self.errors())
        self.assertIn("unsupported document type 'movie'", message)
        self.assertIn('work/alpha declares recipe-idea', message)
        self.assertIn('pisar space type add work/alpha', message)
        self.assertIn('docs/movie.md', message)

    def test_empty_declaration_rejects_every_typed_document(self):
        self.declare('')
        self.write('note', document('note', 'note'))
        message = '\n'.join(self.errors())
        self.assertIn('declares (none)', message)

    def test_undeclared_type_is_not_accepted_from_another_space(self):
        self.declare(IDEA)
        self.declare('', path=self.beta)
        self.write('idea', document('idea', 'recipe-idea', owner='work/beta'), path=self.beta)
        self.assertFalse(self.check(ok=False)['ok'])
        self.assertIn('work/beta declares (none)', '\n'.join(self.errors()))

    def test_non_string_type_is_a_clean_error(self):
        self.declare(IDEA)
        self.write('odd', document('odd', 'recipe-idea').replace('type = "recipe-idea"', 'type = ["recipe-idea"]'))
        self.assertIn('document type must be a string', '\n'.join(self.errors()))


class FieldTests(RecordTypeFixture):
    FIELDS = ('[types.recipe-idea.fields.cuisine]\ntype = "string"\nrequired = true\n'
              '[types.recipe-idea.fields.serves]\ntype = "integer"\n'
              '[types.recipe-idea.fields.vegan]\ntype = "boolean"\n'
              '[types.recipe-idea.fields.tried]\ntype = "date"\n'
              '[types.recipe-idea.fields.tags]\ntype = "list"\nvalues = ["quick", "rich", 3]\n'
              '[types.recipe-idea.fields.season]\nvalues = ["spring", "autumn"]\n')

    def test_valid_fields_pass(self):
        self.declare(IDEA + self.FIELDS)
        self.write('ok', document('ok', 'recipe-idea', extra=(
            'cuisine = "thai"\nserves = 4\nvegan = false\ntried = "2026-10-01"\n'
            'tags = ["quick", 3]\nseason = "spring"\nextra = "undeclared fields stay allowed"\n')))
        self.assertTrue(self.check()['ok'])

    def test_each_field_rule_is_enforced(self):
        cases = {
            'missing-required': ('', 'required field cuisine is missing'),
            'wrong-string': ('cuisine = 1\n', 'field cuisine must be a string'),
            'empty-required-is-present': ('cuisine = ""\n', None),
            'integer-not-bool': ('cuisine = "x"\nserves = true\n', 'field serves must be an integer'),
            'integer-not-string': ('cuisine = "x"\nserves = "4"\n', 'field serves must be an integer'),
            'boolean-not-int': ('cuisine = "x"\nvegan = 0\n', 'field vegan must be a boolean'),
            'date-not-timestamp': ('cuisine = "x"\ntried = "2026-10-01T10:00:00Z"\n',
                                   'field tried must be a quoted calendar date'),
            'date-impossible': ('cuisine = "x"\ntried = "2026-02-30"\n', 'field tried must be a quoted calendar date'),
            'date-not-toml-date': ('cuisine = "x"\ntried = 2026-10-01\n', 'field tried must be a quoted calendar date'),
            'list-not-string': ('cuisine = "x"\ntags = "quick"\n', 'field tags must be a list'),
            'list-element-not-allowed': ('cuisine = "x"\ntags = ["quick", "slow"]\n',
                                         'field tags contains a value not in its allowed values'),
            'list-int-not-string': ('cuisine = "x"\ntags = ["quick", "3", 4]\n',
                                    'field tags contains a value not in its allowed values'),
            'value-not-allowed': ('cuisine = "x"\nseason = "summer"\n',
                                  'field season must be one of: spring, autumn'),
            'value-int-not-string': ('cuisine = "x"\nseason = 1\n', 'field season must be a string'),
        }
        self.declare(IDEA + self.FIELDS)
        for name, (extra, message) in cases.items():
            with self.subTest(name):
                target = self.write(name, document(name, 'recipe-idea', extra=extra))
                errors = [e for e in self.check(ok=message is None)['errors'] if target.name in e]
                if message is None:
                    self.assertEqual(errors, [])
                else:
                    self.assertEqual(len(errors), 1, errors)
                    self.assertIn(message, errors[0])
                target.unlink()

    def test_base_fields_cannot_be_redeclared(self):
        self.declare(IDEA + '[types.recipe-idea.fields.title]\ntype = "string"\n')
        self.assertIn('types.recipe-idea.fields.title: reserved field name', '\n'.join(self.wiki_errors()))


class MalformedDeclarationTests(RecordTypeFixture):
    BAD = {
        'types-not-table': ('types = "recipe"\n', 'types must be a table'),
        'missing-description': ('[types.idea]\n', 'types.idea.description: required'),
        'empty-description': ('[types.idea]\ndescription = "  "\n', 'types.idea.description: nonempty'),
        'unknown-type-key': ('[types.idea]\ndescription = "x"\ncolour = "red"\n',
                             'unknown key in types.idea: colour'),
        'bad-name': ('[types.Recipe]\ndescription = "x"\n', 'types.Recipe: name must match'),
        'reserved-meeting': ('[types.meeting]\ndescription = "x"\n', 'types.meeting: reserved system type'),
        'reserved-source': ('[types.source]\ndescription = "x"\n', 'types.source: reserved system type'),
        'template-not-string': ('[types.idea]\ndescription = "x"\ntemplate = 3\n',
                                'types.idea.template: expected a string'),
        'fields-not-table': ('[types.idea]\ndescription = "x"\nfields = []\n',
                             'types.idea.fields: expected a table'),
        'unknown-field-key': ('[types.idea]\ndescription = "x"\n[types.idea.fields.a]\ncolour = 1\n',
                              'unknown key in types.idea.fields.a: colour'),
        'field-unknown-type': ('[types.idea]\ndescription = "x"\n[types.idea.fields.a]\ntype = "float"\n',
                               'types.idea.fields.a.type: unsupported field type'),
        'required-not-bool': ('[types.idea]\ndescription = "x"\n[types.idea.fields.a]\nrequired = "yes"\n',
                              'types.idea.fields.a.required: expected true or false'),
        'bool-values': ('[types.idea]\ndescription = "x"\n[types.idea.fields.a]\ntype = "boolean"\n'
                        'values = [true]\n', 'types.idea.fields.a.values: boolean fields take no values'),
        'values-not-array': ('[types.idea]\ndescription = "x"\n[types.idea.fields.a]\nvalues = "x"\n',
                             'types.idea.fields.a.values: expected an array'),
        'values-float': ('[types.idea]\ndescription = "x"\n[types.idea.fields.a]\nvalues = [1.5]\n',
                         'types.idea.fields.a.values: expected strings or integers'),
        'values-mixed-for-string': ('[types.idea]\ndescription = "x"\n[types.idea.fields.a]\n'
                                    'values = ["x", 1]\n',
                                    'types.idea.fields.a.values: string fields take string values'),
        'values-bool-element': ('[types.idea]\ndescription = "x"\n[types.idea.fields.a]\n'
                                'values = [true]\n',
                                'types.idea.fields.a.values: expected strings or integers'),
        'fields-bad-name': ('[types.idea]\ndescription = "x"\n[types.idea.fields.""]\n',
                            'types.idea.fields: field name must be nonempty'),
        'types-scalar-entry': ('[types]\nidea = "x"\n', 'types.idea: expected a table'),
        'field-padded-name': ('[types.idea]\ndescription = "x"\n[types.idea.fields." cuisine "]\n',
                              'types.idea.fields. cuisine : field name must not have surrounding whitespace'),
    }

    def test_malformed_declarations_fail_closed(self):
        for name, (text, message) in self.BAD.items():
            with self.subTest(name):
                self.declare(text)
                problems = self.wiki_errors()
                self.assertTrue(any(message in p for p in problems), problems)
                self.assertTrue(any(p.startswith('work/team/alpha/.wiki.toml: ') for p in problems), problems)
                with self.assertRaises(WikiError):
                    Wiki(self.root).space('work/alpha')
                self.assertFalse(self.check(ok=False)['ok'])
                self.assertNotIn('work/alpha', [s.address for s in Wiki(self.root).spaces])

    def test_malformed_nested_space_is_not_a_write_target(self):
        child = space(self.root, 'work/team/alpha/child', 'child')
        self.declare(IDEA)
        child.joinpath('.wiki.toml').write_text(
            'schema_version = 1\nid = "child"\nkind = "project"\nstatus = "active"\n'
            '[types.recipe-idea]\n')
        commit_all(self.root)
        self.assertTrue(any('child' in p and 'description' in p for p in self.wiki_errors()))
        with self.assertRaises(WikiError):
            Wiki(self.root).space('work/child')
        self.assertFalse(self.check(ok=False)['ok'])

    def test_empty_types_table_is_valid(self):
        self.declare('[types]\n')
        self.assertEqual(self.wiki_errors(), [])
        self.assertTrue(self.check()['ok'])


class TypeCommandTests(SpaceCommandFixture, RecordTypeFixture):
    """`pisar space type list|add` (SpaceCommandFixture supplies the synthetic identity)."""

    def setUp(self):
        super().setUp()
        self.marker = self.alpha / '.wiki.toml'
        self.original = MARKER.encode()
        self.marker.write_bytes(self.original)
        commit_changes(self.root)

    def add(self, *args, ok=True):
        result = self.cli('space', 'type', 'add', 'work/alpha', *args, ok=ok)
        return json.loads(result.stdout) if ok else result

    def listing(self, address='work/alpha'):
        return self.data('space', 'type', 'list', address)

    def test_list_reports_system_and_declared_types(self):
        self.declare(IDEA + '[types.recipe-idea.fields.cuisine]\nrequired = true\n')
        result = self.listing()
        self.assertEqual(result['space'], 'work/alpha')
        self.assertEqual(result['system_types'], ['meeting', 'source'])
        (idea,) = result['declared_types']
        self.assertEqual(idea['name'], 'recipe-idea')
        self.assertEqual(idea['description'], 'One idea for a recipe to try later')
        self.assertIsNone(idea['template'])
        self.assertEqual(idea['fields'], [dict(name='cuisine', type='string', required=True, values=None)])

    def test_add_writes_one_journaled_commit_and_keeps_existing_bytes(self):
        self.marker.write_bytes(MARKER.encode() + b'# keep this comment\r\n')
        commit_all(self.root)
        before = int(git(self.root, 'rev-list', '--count', 'HEAD'))
        result = self.add('recipe-idea',
                          '--description', 'One idea for a recipe to try later',
                          '--field', 'cuisine:string:required')
        self.assertTrue(result['changed'])
        self.assertEqual(int(git(self.root, 'rev-list', '--count', 'HEAD')), before + 1)
        self.assertEqual(git(self.root, 'log', '-1', '--format=%s').split(':')[0], 'wiki')
        self.clean()
        text = self.marker.read_bytes()
        self.assertTrue(text.startswith(MARKER.encode() + b'# keep this comment\r\n'))
        self.write('idea', document('idea', 'recipe-idea', extra='cuisine = "thai"\n'))
        self.assertTrue(self.check()['ok'])
        self.assertEqual(self.listing()['declared_types'][0]['fields'][0]['required'], True)

    def test_add_stores_template_verbatim_from_a_file(self):
        template = 'Title "quoted" \\ back\tslash\n\n## Ingredients  \n- ünïcode ✓\r\n'
        source = self.base / 'template.md'
        source.write_bytes(template.encode())
        self.add('recipe-idea', '--description', 'One idea', '--template-file', str(source))
        self.clean()
        stored = self.listing()['declared_types'][0]['template']
        self.assertEqual(stored, template)
        self.write('idea', document('idea', 'recipe-idea'))
        self.assertTrue(self.check()['ok'])

    def test_add_is_idempotent(self):
        self.add('recipe-idea', '--description', 'One idea')
        before = int(git(self.root, 'rev-list', '--count', 'HEAD'))
        again = self.add('recipe-idea', '--description', 'One idea')
        self.assertFalse(again['changed'])
        self.assertEqual(int(git(self.root, 'rev-list', '--count', 'HEAD')), before)
        self.clean()

    def test_identical_declaration_in_other_spelling_is_a_no_op(self):
        self.declare(IDEA + '[types.recipe-idea.fields.cuisine]\ntype = "string"\nrequired = false\n')
        before = int(git(self.root, 'rev-list', '--count', 'HEAD'))
        again = self.add('recipe-idea', '--description', 'One idea for a recipe to try later',
                         '--field', 'cuisine:string')
        self.assertFalse(again['changed'])
        self.assertEqual(int(git(self.root, 'rev-list', '--count', 'HEAD')), before)

    def test_conflicting_redeclaration_is_refused_and_changes_nothing(self):
        self.add('recipe-idea', '--description', 'One idea')
        before = git(self.root, 'rev-parse', 'HEAD')
        text = self.marker.read_bytes()
        self.add('recipe-idea', '--description', 'Another meaning', ok=False)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), before)
        self.assertEqual(self.marker.read_bytes(), text)

    def test_dirty_tree_is_refused_before_anything_is_written(self):
        (self.alpha / 'docs.md').write_text('user edit\n')
        text = self.marker.read_bytes()
        self.add('recipe-idea', '--description', 'One idea', ok=False)
        self.assertEqual(self.marker.read_bytes(), text)

    def test_dirty_tree_is_refused_even_for_an_identical_declaration(self):
        self.add('recipe-idea', '--description', 'One idea')
        (self.alpha / 'draft.md').write_text('user edit\n')
        self.add('recipe-idea', '--description', 'One idea', ok=False)

    def test_refuses_reserved_and_invalid_names_and_bad_fields(self):
        for args in (('meeting', '--description', 'x'),
                     ('Recipe', '--description', 'x'),
                     ('idea', '--description', '  '),
                     ('idea', '--description', 'x', '--field', 'a'),
                     ('idea', '--description', 'x', '--field', 'a:float'),
                     ('idea', '--description', 'x', '--field', 'a:string:maybe'),
                     ('idea', '--description', 'x', '--field', 'a:string', '--field', 'a:integer'),
                     ('idea', '--description', 'x', '--field', ' cuisine :string'),
                     ('idea', '--description', 'x', '--field', 'title:string')):
            with self.subTest(args):
                self.add(*args, ok=False)
                self.assertEqual(self.marker.read_bytes(), self.original)
        self.clean()

    def test_marker_without_a_final_newline_gets_a_separated_table(self):
        self.marker.write_bytes(self.original.rstrip(b'\n'))
        commit_all(self.root)
        self.add('recipe-idea', '--description', 'One idea')
        self.clean()
        text = self.marker.read_text()
        self.assertTrue(text.startswith(MARKER.rstrip('\n') + '\n\n[types.recipe-idea]\n'), text)
        self.assertEqual(self.listing()['declared_types'][0]['name'], 'recipe-idea')

    def test_inline_types_table_cannot_be_appended_to_and_nothing_changes(self):
        self.marker.write_bytes(self.original + b'types = {}\n')
        commit_all(self.root)
        head = git(self.root, 'rev-parse', 'HEAD')
        text = self.marker.read_bytes()
        result = self.add('recipe-idea', '--description', 'One idea', ok=False)
        self.assertIn('cannot append [types.recipe-idea]', result.stderr)
        self.assertEqual(self.marker.read_bytes(), text)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.clean()

    def test_template_inside_the_wiki_is_refused(self):
        inside = self.alpha / 'template.md'
        inside.write_text('## Idea\n')
        commit_all(self.root)
        self.add('idea', '--description', 'x', '--template-file', str(inside), ok=False)
        self.assertEqual(self.marker.read_bytes(), self.original)

    def test_symlinked_template_is_refused(self):
        real = self.base / 'real.md'
        real.write_text('## Idea\n')
        link = self.base / 'link.md'
        link.symlink_to(real)
        self.add('idea', '--description', 'x', '--template-file', str(link), ok=False)
        self.assertEqual(self.marker.read_bytes(), self.original)

    def test_unknown_space_is_refused(self):
        self.cli('space', 'type', 'add', 'work/missing', 'idea', '--description', 'x', ok=False)

    def test_interrupted_add_after_the_marker_write_retries_without_duplicates(self):
        self.add('recipe-idea', '--description', 'One idea')
        # Crash after the marker was written but before the commit: HEAD back, file kept.
        git(self.root, 'reset', '-q', '--mixed', 'HEAD~1')
        runtime = Runtime(self.state, self.root)
        for journal in (runtime.path / 'operations').glob('space-type-add-*.json'):
            data = json.loads(journal.read_text())
            data['status'] = 'incomplete'
            journal.write_text(json.dumps(data))
        before = int(git(self.root, 'rev-list', '--count', 'HEAD'))
        self.assertTrue(self.add('recipe-idea', '--description', 'One idea')['changed'])
        self.assertEqual(int(git(self.root, 'rev-list', '--count', 'HEAD')), before + 1)
        self.clean()
        self.assertEqual(self.marker.read_text().count('[types.recipe-idea]'), 1)

    def test_nested_owner_commits_the_child_then_the_parent_gitlink(self):
        module = self.add_submodule()
        parent_before = git(self.root, 'rev-parse', 'HEAD')
        child_before = git(module, 'rev-parse', 'HEAD')
        self.data('space', 'type', 'add', 'work/module', 'idea', '--description', 'Module idea')
        self.assertNotEqual(git(module, 'rev-parse', 'HEAD'), child_before)
        self.assertEqual(git(module, 'log', '-1', '--format=%s').split(':')[0], 'wiki')
        self.assertNotEqual(git(self.root, 'rev-parse', 'HEAD'), parent_before)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(self.listing('work/module')['declared_types'][0]['name'], 'idea')


if __name__ == '__main__':
    unittest.main()
