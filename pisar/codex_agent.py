"""Run the Codex agent confined to this knowledge repository (``--agent codex``).

Codex gets its own CODEX_HOME and HOME under the pisar state directory, so no
user configuration, skills, hooks or MCP servers apply. Its configuration is
generated here: the pisar agent prompt as instructions, a named permissions
profile that can write only the lesson drafts, no shell tool and no web search
in the main session, and MCP servers from the root's own .mcp.json plus the
pisar server that carries the allowed operations (see mcp.py).
"""
import json
import os
from pathlib import Path
import re
import shutil
import sys
from . import settings
from .agent import mcp_config, prompt_text
from .operations import Runtime, outside_git
from .config import config_home
from .safety import WikiError

EXECUTABLE = 'codex'
CONFIG = 'config.toml'
INSTRUCTIONS = 'instructions.md'
HOME = 'home'
SERVER = 'pisar'
NAME = re.compile(r'[A-Za-z0-9_-]+')
# The child environment: only what Codex needs to run, reach its API and find programs.
# Names are listed one by one; ambient settings such as WIKI_ROOT are never passed.
ENV_NAMES = ('PATH', 'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'TERM', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
             'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy',
             'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_API_KEY')
# The MCP server's environment: the pisar locations, Git and the user's configuration
# directory (pisar settings), never the Codex home. Written into the Codex configuration.
SERVER_ENV = ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TERM', 'PYTHONPATH', 'PYTHONDONTWRITEBYTECODE',
              'XDG_CONFIG_HOME', 'GIT_CONFIG_GLOBAL')
# Options that would change the confinement; caller arguments may not pass them. A model
# (-m/--model) may be passed and replaces the configured one.
REFUSED = frozenset(('--config', '--enable', '--disable', '--profile', '--sandbox', '--add-dir', '--cd',
                     '--remote', '--worktree', '--approve-for-me', '--oss', '--local-provider',
                     '--ask-for-approval', '--approval', '--search', '--full-auto', '--yolo'))
REFUSED_SHORT = frozenset(('-c', '-p', '-s', '-C', '-a'))
REFUSED_PREFIX = ('--dangerously',)
# Native tools and features the main session does not get. Shell, subagents and plugins
# could run programs or read outside the root; the pisar server replaces the shell.
DISABLED_FEATURES = ('shell_tool', 'multi_agent', 'apps', 'plugins', 'browser_use', 'computer_use',
                     'image_generation', 'hooks', 'goals')
PROFILE = 'pisar'


class Inline(dict):
    """A TOML inline table, written as ``{ key = value }``."""


def _key(name):
    return name if NAME.fullmatch(name) else json.dumps(name)


def _value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_value(v) for v in value) + ']'
    if isinstance(value, Inline):
        return '{ ' + ', '.join(f'{_key(k)} = {_value(v)}' for k, v in value.items()) + ' }' if value else '{}'
    raise WikiError(f'configuration value cannot be written: {value!r}')


def render(tree, prefix=()):
    """TOML text for TREE: scalars first, then tables (dicts) with their headers."""
    lines = []
    tables = []
    for key, value in tree.items():
        if isinstance(value, dict) and not isinstance(value, Inline):
            tables.append((key, value))
        elif value is not None:
            lines.append(f'{_key(key)} = {_value(value)}')
    text = '\n'.join(lines) + '\n' if lines else ''
    for key, value in tables:
        header = '.'.join(_key(k) for k in (*prefix, key))
        text += f'\n[{header}]\n' + render(value, (*prefix, key))
    return text


def user_servers(root):
    """MCP servers of the root's own .mcp.json, translated to Codex tables (never a symlink)."""
    path = mcp_config(root)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise WikiError(f'{path}: not valid JSON') from None
    servers = data.get('mcpServers', {}) if isinstance(data, dict) else {}
    if not isinstance(servers, dict):
        raise WikiError(f'{path}: mcpServers must be an object')
    result = {}
    for name, spec in servers.items():
        if not NAME.fullmatch(name) or name == SERVER:
            raise WikiError(f'{path}: MCP server name is not allowed: {name}')
        if not isinstance(spec, dict):
            raise WikiError(f'{path}: MCP server {name} must be an object')
        kind = spec.get('type', 'stdio' if 'command' in spec else 'http')
        if kind == 'stdio' and isinstance(spec.get('command'), str):
            table = {'command': spec['command'], 'args': [str(a) for a in spec.get('args', [])]}
            env = spec.get('env', {})
            if env:
                table['env'] = Inline({str(k): str(v) for k, v in env.items()})
        elif kind in ('http', 'sse') and isinstance(spec.get('url'), str):
            table = {'url': spec['url']}
            headers = spec.get('headers', {})
            if headers:
                table['http_headers'] = Inline({str(k): str(v) for k, v in headers.items()})
        else:
            raise WikiError(f'{path}: MCP server {name} has no usable command or url')
        result[name] = table
    return result


def pisar_command():
    """How to start this pisar again: the zipapp, or the package through ``python -m``."""
    import pisar
    location = Path(pisar.__file__).resolve().parents[1]
    if location.is_file():
        return [sys.executable, str(location)], {}
    return [sys.executable, '-m', 'pisar'], {'PYTHONPATH': str(location), 'PYTHONDONTWRITEBYTECODE': '1'}


def _absolute(path):
    return path if os.path.isabs(path) else os.path.abspath(path)


def _refused(arg):
    name = arg.split('=', 1)[0]
    return (name in REFUSED or arg.startswith(REFUSED_PREFIX)
            or (not arg.startswith('--') and arg[:2] in REFUSED_SHORT))


def checked_arguments(extra):
    """(model or None, arguments to pass). Refuses anything that would widen the confinement."""
    model, passed = None, []
    items = list(extra)
    while items:
        arg = items.pop(0)
        if arg in ('-m', '--model') or arg.startswith('--model='):
            value = arg.split('=', 1)[1] if arg.startswith('--model=') else (items.pop(0) if items else '')
            if not value:
                raise WikiError('codex: --model needs a value')
            model = value
        elif arg.startswith('-m') and not arg.startswith('--') and len(arg) > 2:
            model = arg[2:]
        elif _refused(arg):
            raise WikiError(f'codex: this option cannot be passed to the agent: {arg.split("=", 1)[0]}')
        else:
            passed.append(arg)
    return model, passed


def git_config():
    """The user's global Git configuration, so commits keep the user's identity; nothing else."""
    real = Path(os.path.expanduser('~')) / '.gitconfig'
    return str(real) if real.is_file() else os.devnull


def server_environment(root, state, ruwana):
    """Environment of the pisar MCP server and of every pisar or git it runs."""
    env = {k: os.environ[k] for k in SERVER_ENV if k in os.environ}
    env.update(PISAR_ROOT=str(root), PISAR_STATE_DIR=str(state), PISAR_RUWANA_BIN=str(ruwana),
               XDG_CONFIG_HOME=str(config_home()),
               GIT_CONFIG_GLOBAL=os.environ.get('GIT_CONFIG_GLOBAL') or git_config(),
               GIT_CONFIG_NOSYSTEM='1', GIT_PAGER='cat', PAGER='cat', GIT_EDITOR='true',
               GIT_TERMINAL_PROMPT='0', PYTHONDONTWRITEBYTECODE='1')
    return env


def write_config(config, root, state, ruwana, effort, mcp):
    """Write CONFIG (and the instructions it names) into the isolated CODEX_HOME."""
    instructions = config / INSTRUCTIONS
    instructions.write_text(prompt_text(), encoding='utf-8')
    os.chmod(instructions, 0o600)
    command, environment = pisar_command()
    pisar_args = ['--root', str(root), '--state-dir', str(state), '--ruwana', ruwana, 'mcp-serve']
    server_env = {**server_environment(root, state, ruwana), **environment,
                  'HOME': str(config / HOME)}
    server = {'command': command[0], 'args': [*command[1:], *pisar_args],
              'env': Inline({k: v for k, v in server_env.items()})}
    tree = {
        'approval_policy': 'on-request',
        'web_search': 'disabled',
        'default_permissions': PROFILE,
        'model_instructions_file': str(instructions),
        **({'model_reasoning_effort': effort} if effort else {}),
        'features': {name: False for name in DISABLED_FEATURES},
        'permissions': {PROFILE: {
            'filesystem': {':root': 'read', str(state / 'lessons'): 'write'},
            'network': {'enabled': False},
        }},
        'mcp_servers': {**mcp, SERVER: server},
    }
    file = config / CONFIG
    file.write_text(render(tree), encoding='utf-8')
    os.chmod(file, 0o600)


def environment(config, home, root, state, ruwana):
    """The explicit child environment of Codex: an allowlist plus the pisar and Codex locations."""
    env = {k: os.environ[k] for k in ENV_NAMES if k in os.environ}
    env.update(server_environment(root, state, ruwana), CODEX_HOME=str(config), HOME=str(home))
    return env


def _ruwana_path(ruwana):
    """Absolute before Codex starts children in the root; a bare name is looked up on PATH."""
    if os.sep not in ruwana:
        ruwana = shutil.which(ruwana) or ruwana
    return _absolute(ruwana)


def launch(root, state_dir, ruwana, extra=()):
    """Replace this process with Codex. Returns only by raising."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise WikiError(f'wiki root is not a directory: {root}')
    executable = shutil.which(EXECUTABLE)
    if executable is None:
        raise WikiError(f'{EXECUTABLE} is not on PATH')
    runtime = Runtime(state_dir, root)
    config = outside_git(runtime.base / 'agents' / EXECUTABLE)
    home = config / HOME
    lessons = outside_git(runtime.base / 'lessons')
    for directory in (config, home, lessons):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    override, passed = checked_arguments(extra)
    main = settings.agent_options('codex')
    model = override or main['model']
    ruwana = _ruwana_path(str(ruwana))
    write_config(config, root, runtime.base, ruwana, main['effort'], user_servers(root))
    env = environment(config, home, root, runtime.base, ruwana)
    os.chdir(root)
    os.execve(executable, [executable, '--no-daemon', *(['-m', model] if model else []), *passed], env)
