import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest


CHECKOUT = Path(__file__).resolve().parents[1]
# Real task integration uses PISAR_TEST_RUWANA, then ruwana on PATH; otherwise it
# skips, unless PISAR_TEST_REQUIRE_RUWANA=1 makes a missing binary a failure.
_ruwana = os.environ.get('PISAR_TEST_RUWANA') or shutil.which('ruwana')
RUWANA = Path(_ruwana).absolute() if _ruwana else None
RUWANA_AVAILABLE = RUWANA is not None and RUWANA.is_file()
REQUIRE_RUWANA = os.environ.get('PISAR_TEST_REQUIRE_RUWANA') == '1'
# PISAR_TEST_EXECUTABLE runs every CLI test through a built executable (zipapp)
# from outside the checkout instead of `python -m pisar`.
EXECUTABLE = os.environ.get('PISAR_TEST_EXECUTABLE')
# Settings inherited from the developer's shell must never reach live data.
ISOLATED = ('PISAR_ROOT', 'PISAR_STATE_DIR', 'PISAR_RUWANA_BIN', 'WIKI_ROOT')


def clean_environ(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ISOLATED}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env.update(extra)
    return env


def pisar_command():
    if EXECUTABLE:
        return [str(Path(EXECUTABLE).absolute())]
    return [sys.executable, '-m', 'pisar']


def git(root, *args):
    p = subprocess.run(['git', '-C', str(root), *args], text=True,
                       capture_output=True, check=True)
    return p.stdout.strip()


def init_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 'synthetic@example.invalid')
    git(root, 'config', 'user.name', 'Synthetic fixture')


def commit_all(root):
    # Only synthetic fixture repos, never user data.
    git(root, 'add', '--all')
    git(root, 'commit', '-qm', 'fixture')


def space(root, relative, ident, kind='project'):
    path = root / relative
    path.mkdir(parents=True, exist_ok=True)
    (path / '.wiki.toml').write_text(
        f'schema_version = 1\nid = "{ident}"\nkind = "{kind}"\nstatus = "active"\n')
    return path


def document(ident='call-one', owner='alpha', related=(), sources=(), body='Discussed русский launch.'):
    return ('+++\nschema_version = 1\n'
            f'id = {json.dumps(ident)}\ntype = "meeting"\ntitle = "Launch call"\n'
            f'space_ids = {json.dumps([owner, *related])}\n'
            f'sources = {json.dumps(list(sources))}\n+++\n\n{body}\n')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pisar-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'wiki'
        init_repo(self.root)
        self.alpha = space(self.root, 'work/team/alpha', 'alpha')
        self.beta = space(self.root, 'work/team/beta', 'beta')
        self.personal = space(self.root, 'personal/10-projects/home', 'home')
        (self.alpha / 'call.md').write_text(document(related=('beta',)))
        (self.personal / 'note.md').write_text(document('private-note', 'home', body='Private launch data.'))
        commit_all(self.root)
        self.state = self.base / 'runtime'
        self.env = clean_environ(XDG_DATA_HOME=str(self.base / 'data'),
                                 XDG_CACHE_HOME=str(self.base / 'cache'))

    def require_ruwana(self):
        if not RUWANA_AVAILABLE:
            if REQUIRE_RUWANA:
                self.fail('PISAR_TEST_REQUIRE_RUWANA=1 but no real ruwana binary was found')
            self.skipTest('real ruwana binary unavailable')

    def run_pisar(self, *args, ok=True, env=None, cwd=None):
        """Run the CLI with only the given arguments, environment and directory."""
        env = dict(self.env if env is None else env)
        if cwd is not None and not EXECUTABLE:
            env['PYTHONPATH'] = str(CHECKOUT)
        p = subprocess.run([*pisar_command(), *map(str, args)],
                           cwd=cwd or (self.base if EXECUTABLE else CHECKOUT),
                           env=env, text=True, capture_output=True)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        else:
            self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def cli(self, *args, ok=True, root=None, state=None):
        return self.run_pisar('--root', root or self.root, '--state-dir', state or self.state,
                              *args, ok=ok)

    def data(self, *args, **kwargs):
        return json.loads(self.cli(*args, **kwargs).stdout)

    def external(self, content='Synthetic transcript: agreed to launch.'):
        path = self.base / 'transcript.txt'
        path.write_text(content)
        return path

    def add_submodule(self):
        remote = self.base / 'module-origin'
        init_repo(remote)
        space(remote, '.', 'module')
        (remote / 'note.md').write_text(document('module-note', 'module', body='Module evidence.'))
        commit_all(remote)
        git(self.root, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q',
            str(remote), 'work/module')
        # Cloning a submodule does not copy the origin's local author identity.
        module = self.root / 'work/module'
        git(module, 'config', 'user.email', 'synthetic@example.invalid')
        git(module, 'config', 'user.name', 'Synthetic fixture')
        commit_all(self.root)
        return module
