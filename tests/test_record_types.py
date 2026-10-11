"""Per-space record types declared in .wiki.toml, on temporary fixtures only."""
from pathlib import Path
import json
import os
from unittest.mock import patch
import subprocess
import sys
import unittest

from pisar.operations import Runtime
from pisar.spacecmd import _declaration_text, _digest
from pisar.spaces import parse_type
from pisar.safety import WikiError
from pisar.spaces import Wiki
from .support import CHECKOUT, Fixture, commit_all, git, space
from .test_spacecmd import SpaceCommandFixture


MARKER = 'schema_version = 1\nid = "alpha"\nkind = "project"\nstatus = "active"\n'
IDEA = '[types.recipe-idea]\ndescription = "One idea for a recipe to try later"\n'


def document(ident, doc_type, extra='', owner='work/alpha', body='Body text.'):
    return ('+++\nschema_version = 1\n'
            f'id = "{ident}"\ntype = {json.dumps(doc_type)}\ntitle = "Idea"\n'
            f'space_ids = [{json.dumps(owner)}]\nsources = []\n{extra}+++\n\n{body}\n')


def temp_name():
    """The legacy deterministic temporary name for `space type add work/alpha recipe-idea`."""
    text = _declaration_text(parse_type('recipe-idea', dict(description='One idea')))
    op = f'space-type-add-work-alpha-recipe-idea-{_digest("work/alpha", "recipe-idea", text)}'
    return f'.pisar-{_digest(op, "work/team/alpha/.wiki.toml")}.tmp'


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
        isolated = {k: v for k, v in os.environ.items()
                    if not k.startswith(('PISAR_', 'XDG_', 'GIT_')) and k != 'EMAIL'}
        isolated.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        self.environment = patch.dict(os.environ, isolated, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        super().setUp()
        (self.base / 'home' / '.gitconfig').write_text('')
        os.environ['HOME'] = str(self.base / 'home')
        os.environ['XDG_CONFIG_HOME'] = str(self.base / 'config')
        self.env['HOME'] = str(self.base / 'home')
        self.env['XDG_CONFIG_HOME'] = str(self.base / 'config')
        self.marker = self.alpha / '.wiki.toml'
        self.original = MARKER.encode()
        self.marker.write_bytes(self.original)
        commit_changes(self.root)

    def add(self, *args, ok=True):
        result = self.cli('space', 'type', 'add', 'work/alpha', *args, ok=ok)
        return json.loads(result.stdout) if ok else result

    def listing(self, address='work/alpha'):
        return self.data('space', 'type', 'list', address)

    ARGS = ('recipe-idea', '--description', 'One idea')

    def snapshot(self, *paths):
        return (self.marker.read_bytes(), git(self.root, 'rev-parse', 'HEAD'),
                git(self.root, 'ls-files', '--stage'), git(self.root, 'status', '--porcelain'),
                tuple(os.readlink(p) if p.is_symlink() else p.read_bytes() if p.exists() else None for p in paths))

    def refused_twice(self, *paths):
        before = self.snapshot(*paths)
        for _ in range(2):
            result = self.add(*self.ARGS, ok=False)
            self.assertEqual(self.snapshot(*paths), before)
        return result

    def test_commit_failure_after_rename_preserves_reused_legacy_path(self):
        head = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'config', 'user.useConfigOnly', 'true')
        git(self.root, 'config', '--unset', 'user.name')
        git(self.root, 'config', '--unset', 'user.email')
        result = self.add(*self.ARGS, ok=False)
        self.assertIn('identity', result.stderr.lower())
        self.assertEqual(self.marker.read_text().count('[types.recipe-idea]'), 1)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        user = self.alpha / temp_name()
        self.assertFalse(user.exists())
        git(self.root, 'config', 'user.name', 'Synthetic fixture')
        git(self.root, 'config', 'user.email', 'synthetic@example.invalid')
        user.write_bytes(b'schema_version = 1\n')
        self.refused_twice(user)

    def competitor_prelude(self, content):
        # On new journals this is an ordinary user file, never a reserved output.
        return (
            'import os\nimport pisar.operations as operations\n'
            'real_store = operations.Runtime.store\n'
            'def store(self, journal):\n'
            '    real_store(self, journal)\n'
            f'    temp = journal.get("temp") or {temp_name()!r}\n'
            '    if journal["status"] == "incomplete":\n'
            '        path = os.path.join(journal["root"], os.path.dirname(journal["artifacts"][0]["path"]), temp)\n'
            '        if not os.path.lexists(path):\n'
            f'            with open(path, "wb") as f: f.write({content!r})\n'
            'operations.Runtime.store = store\n')

    def test_empty_competitor_is_preserved_across_retries(self):
        result = self.child_add(self.competitor_prelude(b''), *self.ARGS)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        if result.returncode:
            self.assertEqual(self.marker.read_bytes(), self.original)
        else:
            self.assertEqual(self.marker.read_text().count('[types.recipe-idea]'), 1)
        user = self.alpha / temp_name()
        self.assertEqual(user.read_bytes(), b'')
        self.refused_twice(user)

    def test_refused_resume_does_not_clean_a_full_leftover(self):
        leftover = self.crash_leftover()
        draft = self.alpha / 'user-draft.md'
        draft.write_bytes(b'user draft\n')
        self.refused_twice(leftover, draft)

    def test_already_final_marker_is_not_rewritten_on_resume(self):
        self.interrupted_add(*self.ARGS)
        before = int(git(self.root, 'rev-list', '--count', 'HEAD'))
        prelude = (
            'import os\nreal_replace = os.replace\n'
            'def replace(source, target, *a, **k):\n'
            f'    if str(target) == {str(self.marker)!r}: raise AssertionError("marker rewritten")\n'
            '    return real_replace(source, target, *a, **k)\n'
            'os.replace = replace\n')
        result = self.child_add(prelude, *self.ARGS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(int(git(self.root, 'rev-list', '--count', 'HEAD')), before + 1)
        self.assertEqual(self.marker.read_text().count('[types.recipe-idea]'), 1)
        self.clean()

    def test_new_add_preserves_tracked_legacy_temp_name(self):
        user = self.alpha / temp_name()
        user.write_bytes(b'my tracked draft\n')
        commit_all(self.root)
        entry = git(self.root, 'ls-files', '--stage', '--', str(user))
        self.add(*self.ARGS)
        self.assertEqual(user.read_bytes(), b'my tracked draft\n')
        self.assertEqual(git(self.root, 'ls-files', '--stage', '--', str(user)), entry)
        self.assertEqual(git(self.root, 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD'),
                         'work/team/alpha/.wiki.toml')
        self.clean()

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
        _, journal = self.journal_data()
        self.assertIn('expected', journal)
        self.assertIn('final', journal)
        self.assertNotIn('temp', journal)
        self.assertNotIn('temp_acquired', journal)
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
        self.crash_before_commit()  # the marker was written, the commit never happened
        before = int(git(self.root, 'rev-list', '--count', 'HEAD'))
        self.assertTrue(self.add('recipe-idea', '--description', 'One idea')['changed'])
        self.assertEqual(int(git(self.root, 'rev-list', '--count', 'HEAD')), before + 1)
        self.clean()
        self.assertEqual(self.marker.read_text().count('[types.recipe-idea]'), 1)

    def journals(self):
        return list((Runtime(self.state, self.root).path / 'operations').glob('space-type-add-*.json'))

    def crash_before_commit(self):
        """Leave the journals as a crash before any commit does: pre-operation heads, no commits, the working
        files as they were written. Every repository that was committed to is reset, index included."""
        for path in self.journals():
            data = json.loads(path.read_text())
            for entry in data['commits']:
                repo = Path(entry['repo'])
                before = git(repo, 'rev-parse', entry['commit'] + '^')
                git(repo, 'reset', '-q', '--mixed', before)
                data['heads'][entry['repo']] = before
            data.update(commits=[], pending=None, status='incomplete')
            path.write_text(json.dumps(data))

    def crash_after_the_branch_moved(self):
        """Leave the root as a crash between update-ref and the checkpoint does: the pending record names the new
        commit, the journal still has the pre-operation head and the index keeps the pre-operation entries."""
        for path in self.journals():
            data = json.loads(path.read_text())
            (entry,) = data['commits']
            before = git(self.root, 'rev-parse', entry['commit'] + '^')
            git(self.root, 'read-tree', before)
            data['pending'] = dict(repo=entry['repo'], commit=entry['commit'], parent=before)
            data['heads'][entry['repo']] = before
            data.update(commits=[], status='incomplete')
            path.write_text(json.dumps(data))

    def interrupted_add(self, *args):
        """Interrupt an add after its marker write: HEAD back to the old commit, file and journal kept."""
        self.add(*args)
        self.crash_before_commit()

    def test_staged_user_edit_to_the_marker_is_refused_on_resume(self):
        args = ('recipe-idea', '--description', 'One idea')
        self.interrupted_add(*args)
        final = self.marker.read_bytes()
        # The user stages a comment, then only the working file is restored to the journaled bytes.
        self.marker.write_bytes(final + b'# user comment\n')
        git(self.root, 'add', '--', 'work/team/alpha/.wiki.toml')
        self.marker.write_bytes(final)
        staged = git(self.root, 'show', ':work/team/alpha/.wiki.toml')
        head = git(self.root, 'rev-parse', 'HEAD')
        result = self.add(*args, ok=False)
        self.assertIn('staged', result.stderr)
        self.assertEqual(git(self.root, 'show', ':work/team/alpha/.wiki.toml'), staged)
        self.assertEqual(self.marker.read_bytes(), final)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_stale_index_after_a_head_update_still_resumes(self):
        args = ('recipe-idea', '--description', 'One idea')
        self.add(*args)
        # A crash after update-ref leaves the index at the pre-operation entries while HEAD is new.
        self.crash_after_the_branch_moved()
        before = int(git(self.root, 'rev-list', '--count', 'HEAD'))
        self.add(*args)
        self.assertEqual(int(git(self.root, 'rev-list', '--count', 'HEAD')), before)
        self.clean()

    def crash_before_the_rename(self, *args):
        """Run the add in a child process that exits (no cleanup runs) just before the marker is renamed.

        os._exit skips every finally block, exactly like a SIGKILL at that point, and touches no other process."""
        code = (
            'import os, sys\n'
            'import pisar.operations as operations\n'
            'real = os.replace\n'
            'def replace(source, target, *a, **k):\n'
            f'    if str(target) == {str(self.marker)!r}:\n'
            '        os._exit(9)\n'
            '    return real(source, target, *a, **k)\n'
            'os.replace = replace\n'
            'from pisar.cli import main\n'
            'sys.exit(main(sys.argv[1:]))\n')
        return subprocess.run(
            [sys.executable, '-c', code, '--root', str(self.root), '--state-dir', str(self.state),
             'space', 'type', 'add', 'work/alpha', *args],
            cwd=CHECKOUT, env={**self.env, 'PYTHONPATH': str(CHECKOUT)}, text=True, capture_output=True)

    def test_hard_crash_siblings_are_preserved_until_explicit_resolution(self):
        for window in ('exclusive-create', 'partial-write', 'pre-rename'):
            with self.subTest(window=window):
                before = int(git(self.root, 'rev-list', '--count', 'HEAD'))
                head = git(self.root, 'rev-parse', 'HEAD')
                try:
                    prelude = self.crash_prelude(window)
                    crashed = self.child_add(prelude, *self.ARGS)
                    self.assertEqual(crashed.returncode, 9, crashed.stderr)
                    self.assertEqual(self.marker.read_bytes(), self.original)
                    self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
                    (leftover,) = self.alpha.glob('.pisar-*')
                    final = self.journal_data()[1]['final'].encode()
                    expected = {'exclusive-create': b'', 'partial-write': final[:len(final)//2],
                                'pre-rename': final}[window]
                    self.assertEqual(leftover.read_bytes(), expected)
                    result = self.refused_twice(leftover)
                    self.assertIn(leftover.name, result.stderr)
                    saved = self.base / 'saved-leftover'
                    leftover.rename(saved)
                    self.add(*self.ARGS)
                    self.assertEqual(saved.read_bytes(), expected)
                    self.assertEqual(int(git(self.root, 'rev-list', '--count', 'HEAD')), before + 1)
                    self.assertEqual(self.marker.read_text().count('[types.recipe-idea]'), 1)
                    self.clean()
                finally:
                    # Reset only synthetic data, even when a baseline subtest fails.
                    git(self.root, 'reset', '--hard', head)
                    for path in self.alpha.glob('.pisar-*'):
                        path.unlink()
                    self.journal_data()[0].unlink()

    def crash_prelude(self, window):
        return f'''import os
from pathlib import Path
marker = {str(self.marker)!r}
parent = str(Path(marker).parent)
window = {window!r}
fds = set()
real_open, real_write, real_fdopen, real_replace = os.open, os.write, os.fdopen, os.replace
def opening(path, flags, *a, **k):
    fd = real_open(path, flags, *a, **k)
    p = Path(path)
    if str(p.parent) == parent and p.name.startswith('.pisar-') and flags & os.O_CREAT:
        fds.add(fd)
        if window == 'exclusive-create': os._exit(9)
    return fd
def writing(fd, data):
    if fd in fds and window == 'partial-write':
        real_write(fd, bytes(data)[:len(data)//2])
        os._exit(9)
    return real_write(fd, data)
class Stream:
    def __init__(self, stream): self.stream = stream
    def __enter__(self): self.stream.__enter__(); return self
    def __exit__(self, *a): return self.stream.__exit__(*a)
    def __getattr__(self, name): return getattr(self.stream, name)
    def write(self, data):
        self.stream.write(data[:len(data)//2])
        self.stream.flush()
        os._exit(9)
def fdopen(fd, *a, **k):
    stream = real_fdopen(fd, *a, **k)
    return Stream(stream) if fd in fds and window == 'partial-write' else stream
def replace(source, target, *a, **k):
    if str(target) == marker and window == 'pre-rename': os._exit(9)
    return real_replace(source, target, *a, **k)
os.open, os.write, os.fdopen, os.replace = opening, writing, fdopen, replace
'''

    def journal_data(self):
        (path,) = (Runtime(self.state, self.root).path / 'operations').glob('space-type-add-*.json')
        return path, json.loads(path.read_text())

    def legacy_interrupted_add(self, content, acquired=False):
        """Synthetic old-format compatibility fixture, not a real crash."""
        self.interrupted_add(*self.ARGS)
        path, journal = self.journal_data()
        journal['temp'] = temp_name()
        journal.pop('temp_acquired', None)
        if acquired is not None:
            journal['temp_acquired'] = acquired
        journal['status'] = 'incomplete'
        self.marker.write_bytes(self.original)
        path.write_text(json.dumps(journal))
        leftover = self.alpha / temp_name()
        if content is not None:
            leftover.write_bytes(content)
        return leftover

    def test_unrelated_temp_looking_user_file_is_never_removed(self):
        user = self.alpha / '.pisar-notes.tmp'
        user.write_text('my own draft\n')
        commit_all(self.root)
        user.write_text('my draft, edited\n')
        original = self.marker.read_bytes()
        self.add('recipe-idea', '--description', 'One idea', ok=False)
        self.assertEqual(user.read_text(), 'my draft, edited\n')
        self.assertEqual(self.marker.read_bytes(), original)

    def crash_leftover(self):
        """A type-add killed before its rename: one temporary sibling is left beside the marker."""
        self.crash_before_the_rename('recipe-idea', '--description', 'One idea')
        (leftover,) = self.alpha.glob('.pisar-*')
        return leftover

    def child_add(self, prelude, *args):
        """Run `space type add` in a child process after PRELUDE (test code patching one call site). The
        child may exit with os._exit, which skips every cleanup exactly as a SIGKILL at that point would."""
        code = prelude + 'from pisar.cli import main\nimport sys\nsys.exit(main(sys.argv[1:]))\n'
        return subprocess.run(
            [sys.executable, '-c', code, '--root', str(self.root), '--state-dir', str(self.state),
             'space', 'type', 'add', 'work/alpha', *args],
            cwd=CHECKOUT, env={**self.env, 'PYTHONPATH': str(CHECKOUT)}, text=True, capture_output=True)

    def test_competing_file_created_after_the_journal_is_refused_and_kept(self):
        result = self.child_add(self.competitor_prelude(b'independent user draft\n'), *self.ARGS)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        if result.returncode:
            self.assertEqual(self.marker.read_bytes(), self.original)
        else:
            self.assertEqual(self.marker.read_text().count('[types.recipe-idea]'), 1)
        competing = self.alpha / temp_name()
        self.assertEqual(competing.read_bytes(), b'independent user draft\n')
        self.refused_twice(competing)

    def test_refused_collision_with_a_prefix_match_is_kept_across_retries(self):
        result = self.child_add(self.competitor_prelude(b'schema_version = 1\n'), *self.ARGS)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        if result.returncode:
            self.assertEqual(self.marker.read_bytes(), self.original)
        else:
            self.assertEqual(self.marker.read_text().count('[types.recipe-idea]'), 1)
        competing = self.alpha / temp_name()
        self.assertEqual(competing.read_bytes(), b'schema_version = 1\n')
        self.refused_twice(competing)

    def test_old_style_journal_without_the_acquisition_record_keeps_a_nonempty_leftover(self):
        leftover = self.legacy_interrupted_add(b'schema_version = 1\n', acquired=None)
        self.refused_twice(leftover)

    def test_legacy_competitor_matrix_is_preserved_across_retries(self):
        for acquired in (None, False, True):
            for content in (b'independent user draft\n', b'schema_version = 1\n',
                            self.original + b'\n[types.recipe-idea]\ndescription = "One idea"\n'):
                with self.subTest(acquired=acquired, content=content):
                    head = git(self.root, 'rev-parse', 'HEAD')
                    try:
                        leftover = self.legacy_interrupted_add(content, acquired)
                        self.refused_twice(leftover)
                    finally:
                        if leftover.exists(): leftover.unlink()
                        git(self.root, 'reset', '--hard', head)
                        self.journal_data()[0].unlink()

    def test_tracked_user_file_at_the_temp_path_is_preserved_on_new_add(self):
        self.test_new_add_preserves_tracked_legacy_temp_name()

    def test_ignored_user_file_at_the_temp_path_is_preserved_on_new_add(self):
        (self.root / '.gitignore').write_text('.pisar-*\n')
        commit_all(self.root)
        user = self.alpha / temp_name()
        user.write_bytes(b'my ignored notes\n')
        self.add(*self.ARGS)
        self.assertEqual(user.read_bytes(), b'my ignored notes\n')
        self.clean()

    def test_symlink_at_the_temp_path_is_preserved_on_new_add(self):
        (self.root / '.gitignore').write_text('.pisar-*\n')
        commit_all(self.root)
        target = self.base / 'external.txt'
        target.write_bytes(b'outside the wiki\n')
        user = self.alpha / temp_name()
        user.symlink_to(target)
        self.add(*self.ARGS)
        self.assertTrue(user.is_symlink())
        self.assertEqual(os.readlink(user), str(target))
        self.assertEqual(target.read_bytes(), b'outside the wiki\n')
        self.clean()

    def test_occupied_legacy_paths_are_refused_without_changes(self):
        for kind in ('tracked', 'ignored', 'symlink', 'directory'):
            with self.subTest(kind=kind):
                (self.root / '.gitignore').write_text('.pisar-*\n')
                commit_changes(self.root)
                target = self.base / 'external.txt'
                target.write_bytes(b'external bytes\n')
                user = self.alpha / temp_name()
                if kind == 'tracked':
                    user.write_bytes(b'tracked draft\n')
                    git(self.root, 'add', '-f', '--', str(user))
                    git(self.root, 'commit', '-qm', 'fixture tracked draft')
                self.legacy_interrupted_add(None)
                if kind == 'ignored': user.write_bytes(b'ignored draft\n')
                if kind == 'symlink': user.symlink_to(target)
                if kind == 'directory': user.mkdir()
                before = self.snapshot() if kind == 'directory' else self.snapshot(user)
                for _ in range(2):
                    result = self.add(*self.ARGS, ok=False)
                    self.assertIn('temporary path', result.stderr)
                    self.assertEqual(self.snapshot() if kind == 'directory' else self.snapshot(user), before)
                    self.assertEqual(target.read_bytes(), b'external bytes\n')
                    if kind == 'directory': self.assertTrue(user.is_dir())
                if kind == 'directory': user.rmdir()
                else: user.unlink()
                if kind == 'tracked': commit_all(self.root)
                self.journal_data()[0].unlink()

    def test_malformed_legacy_temp_names_are_refused(self):
        self.interrupted_add(*self.ARGS)
        path, journal = self.journal_data()
        for temp in ('', '.', '..', '../draft', 'nested/draft', 'nested\\draft', 42):
            with self.subTest(temp=temp):
                journal['temp'] = temp
                path.write_text(json.dumps(journal))
                self.refused_twice()

    def test_leftover_that_became_a_tracked_user_file_is_kept_on_retry(self):
        leftover = self.crash_leftover()
        leftover.write_text('my draft, committed by me\n')
        commit_all(self.root)
        head = git(self.root, 'rev-parse', 'HEAD')
        self.add('recipe-idea', '--description', 'One idea', ok=False)
        self.assertEqual(leftover.read_text(), 'my draft, committed by me\n')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.marker.read_text().count('[types.recipe-idea]'), 0)

    def test_untracked_leftover_with_foreign_bytes_is_kept_on_retry(self):
        leftover = self.crash_leftover()
        leftover.write_text('not what pisar wrote\n')
        self.add('recipe-idea', '--description', 'One idea', ok=False)
        self.assertEqual(leftover.read_text(), 'not what pisar wrote\n')

    def test_unresolved_three_way_merge_in_the_marker_is_refused_and_kept(self):
        original = self.marker.read_bytes()
        self.interrupted_add('recipe-idea', '--description', 'One idea')
        final = self.marker.read_bytes()
        # The intended marker is on disk; the index holds a 3-way conflict (stages 1, 2 and 3).
        blobs = []
        for content in (original, final + b'# user comment\n', original + b'# other comment\n'):
            blobs.append(subprocess.run(['git', '-C', str(self.root), 'hash-object', '-w', '--stdin'],
                                        input=content, capture_output=True, check=True).stdout.decode().strip())
        git(self.root, 'rm', '--cached', '-q', '--', 'work/team/alpha/.wiki.toml')
        lines = ''.join(f'100644 {blob} {stage}\twork/team/alpha/.wiki.toml\n'
                        for stage, blob in zip((1, 2, 3), blobs))
        subprocess.run(['git', '-C', str(self.root), 'update-index', '--index-info'], input=lines,
                       text=True, check=True)
        staged = git(self.root, 'ls-files', '-u', '--', 'work/team/alpha/.wiki.toml')
        head = git(self.root, 'rev-parse', 'HEAD')
        result = self.add('recipe-idea', '--description', 'One idea', ok=False)
        self.assertIn('unresolved merge', result.stderr)
        self.assertEqual(git(self.root, 'ls-files', '-u', '--', 'work/team/alpha/.wiki.toml'), staged)
        self.assertEqual(self.marker.read_bytes(), final)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_real_cached_three_way_conflict_is_preserved_on_resume(self):
        relative = 'work/team/alpha/.wiki.toml'
        self.marker.write_bytes(self.original.replace(b'kind = "project"', b'kind = "resource"'))
        patch_bytes = subprocess.run(['git', '-C', str(self.root), 'diff', '--full-index', '--binary'],
                                     capture_output=True, check=True).stdout
        self.marker.write_bytes(self.original)
        self.interrupted_add(*self.ARGS)
        final = self.marker.read_bytes()
        self.marker.write_bytes(final.replace(b'kind = "project"', b'kind = "area"'))
        git(self.root, 'add', '--', relative)
        applied = subprocess.run(['git', '-C', str(self.root), 'apply', '--3way', '--cached'],
                                 input=patch_bytes, capture_output=True)
        self.assertNotEqual(applied.returncode, 0, applied.stderr)
        stages = git(self.root, 'ls-files', '--stage', '--', relative)
        self.assertEqual([line.split('\t')[0].split()[2] for line in stages.splitlines()], ['1', '2', '3'])
        self.marker.write_bytes(final)
        self.assertIn('unresolved merge', self.refused_twice().stderr)

    def module_interruption(self, module, window):
        prelude = (
            'import os\nimport pisar.gitops as gitops\nreal_commit = gitops.commit\n'
            'def commit(repo, *a, **k):\n'
            '    result = real_commit(repo, *a, **k)\n'
            f'    if str(repo) == {str(module if window == "child" else self.root)!r}: os._exit(9)\n'
            '    return result\n'
            'gitops.commit = commit\n')
        code = prelude + 'from pisar.cli import main\nimport sys\nsys.exit(main(sys.argv[1:]))\n'
        return subprocess.run(
            [sys.executable, '-c', code, '--root', str(self.root), '--state-dir', str(self.state),
             'space', 'type', 'add', 'work/module', *self.ARGS],
            cwd=CHECKOUT, env={**self.env, 'PYTHONPATH': str(CHECKOUT)}, capture_output=True, text=True)

    def test_ancestor_stale_indexes_resume_without_duplicate_commits(self):
        module = self.add_submodule()
        for window in ('child', 'parent'):
            with self.subTest(window=window):
                parent_head = git(self.root, 'rev-parse', 'HEAD')
                child_head = git(module, 'rev-parse', 'HEAD')
                parent_count = int(git(self.root, 'rev-list', '--count', 'HEAD'))
                child_count = int(git(module, 'rev-list', '--count', 'HEAD'))
                crashed = self.module_interruption(module, window)
                self.assertEqual(crashed.returncode, 9, crashed.stderr)
                git(module, 'read-tree', child_head)
                git(self.root, 'read-tree', parent_head)
                self.data('space', 'type', 'add', 'work/module', *self.ARGS)
                self.assertEqual(int(git(module, 'rev-list', '--count', 'HEAD')), child_count + 1)
                self.assertEqual(int(git(self.root, 'rev-list', '--count', 'HEAD')), parent_count + 1)
                self.clean(module)
                self.clean()
                self.assertEqual((module / '.wiki.toml').read_text().count('[types.recipe-idea]'), 1)
                git(module, 'reset', '--hard', child_head)
                git(self.root, 'reset', '--hard', parent_head)
                self.journal_data()[0].unlink()

    def test_ancestor_unmerged_gitlink_is_refused_and_preserved(self):
        module = self.add_submodule()
        crashed = self.module_interruption(module, 'child')
        self.assertEqual(crashed.returncode, 9, crashed.stderr)
        old = git(module, 'rev-parse', 'HEAD~1')
        new = git(module, 'rev-parse', 'HEAD')
        git(self.root, 'rm', '--cached', '-q', '--', 'work/module')
        lines = ''.join(f'160000 {sha} {stage}\twork/module\n'
                        for stage, sha in ((1, old), (2, new), (3, old)))
        subprocess.run(['git', '-C', str(self.root), 'update-index', '--index-info'],
                       input=lines, text=True, check=True)
        child_before = (git(module, 'rev-parse', 'HEAD'), git(module, 'ls-files', '--stage'),
                        git(module, 'status', '--porcelain'), (module / '.wiki.toml').read_bytes())
        before = self.snapshot()
        for _ in range(2):
            result = self.cli('space', 'type', 'add', 'work/module', *self.ARGS, ok=False)
            self.assertIn('unresolved merge', result.stderr)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual((git(module, 'rev-parse', 'HEAD'), git(module, 'ls-files', '--stage'),
                              git(module, 'status', '--porcelain'), (module / '.wiki.toml').read_bytes()),
                             child_before)

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
