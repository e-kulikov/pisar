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
from . import codex_plugin
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
              'HOME', 'XDG_CONFIG_HOME')
# The config keys a caller may set (-c key=value), with the values they accept.
OVERRIDES = {'model': r'[A-Za-z0-9._/-]+', 'model_reasoning_effort': r'[a-z]+'}
# Native tools and features the main session does not get. Shell, subagents and plugins
# could run programs or read outside the root; the pisar server replaces the shell.
DISABLED_FEATURES = ('shell_tool', 'multi_agent', 'apps', 'plugins', 'browser_use', 'computer_use',
                     'image_generation', 'hooks', 'goals', 'view_image')
PROFILE = 'pisar'
# The tools the pisar server offers (see mcp.py); Codex shows and calls only these.
# Codex's default tool timeout is shorter than a lesson review, a research run or a user's time
# to read a lesson before accepting it; the pisar server's own bounds stay in force inside it.
SERVER_TOOL_TIMEOUT = 3600
SERVER_TOOLS = ('pisar', 'git', 'lesson_accept', 'lesson_review', 'lesson_draft_read', 'lesson_draft_write',
                'sync_file_read', 'sync_file_write')


# Codex-specific wording appended to the shared agent prompt: the tools that stand in for Bash.
TOOLS_NOTE = codex_plugin.TOOLS


class Inline(dict):
    """A TOML inline table, written as ``{ key = value }``."""


def _key(name):
    return name if NAME.fullmatch(name) else json.dumps(name)


def _value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
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


def _override(pair):
    """(key, value) of one ``key=value`` config override, when it is one of OVERRIDES; else Refused."""
    key, sep, value = pair.partition('=')
    value = value.strip('"')
    if not sep or key not in OVERRIDES or not re.fullmatch(OVERRIDES[key], value):
        raise WikiError(f'codex: this config override cannot be passed to the agent: {key}')
    return key, value


def checked_arguments(extra):
    """(overrides, nothing else): the caller may set the model (-m/--model) and the keys in OVERRIDES.

    This is an allowlist. Every other argument is refused before Codex starts, including subcommands
    (exec, review, resume, mcp, ...) and switches that load or ignore configuration, profiles, sandbox
    or approval modes, directories, images and prompts: any of them could replace the confinement.
    """
    overrides = {}
    items = list(extra)
    while items:
        arg = items.pop(0)
        if arg in ('-m', '--model') or arg.startswith('--model='):
            value = arg.split('=', 1)[1] if arg.startswith('--model=') else (items.pop(0) if items else '')
            if not re.fullmatch(OVERRIDES['model'], value):
                raise WikiError('codex: --model needs a model name')
            overrides['model'] = value
        elif arg.startswith('-m') and not arg.startswith('--') and len(arg) > 2:
            overrides['model'] = _override_value('model', arg[2:])
        elif arg in ('-c', '--config'):
            key, value = _override(items.pop(0) if items else '')
            overrides[key] = value
        elif arg.startswith('--config='):
            key, value = _override(arg.split('=', 1)[1])
            overrides[key] = value
        elif arg.startswith('-c') and not arg.startswith('--') and len(arg) > 2:
            key, value = _override(arg[2:])
            overrides[key] = value
        else:
            raise WikiError(f'codex: this argument cannot be passed to the agent: {arg.split("=", 1)[0]}')
    return overrides, []


def _override_value(key, value):
    if not re.fullmatch(OVERRIDES[key], value):
        raise WikiError(f'codex: this value cannot be passed to the agent: {key}')
    return value


def server_environment(root, state, ruwana):
    """Environment of the pisar MCP server and of every pisar or git it runs."""
    env = {k: os.environ[k] for k in SERVER_ENV if k in os.environ}
    env.update(PISAR_ROOT=str(root), PISAR_STATE_DIR=str(state), PISAR_RUWANA_BIN=str(ruwana),
               XDG_CONFIG_HOME=str(config_home()), GIT_PAGER='cat', PAGER='cat', GIT_EDITOR='true',
               GIT_TERMINAL_PROMPT='0', PYTHONDONTWRITEBYTECODE='1')
    return env


def write_config(config, root, state, ruwana, effort, mcp):
    """Write CONFIG (and the instructions it names) into the isolated CODEX_HOME."""
    instructions = config / INSTRUCTIONS
    instructions.write_text(prompt_text() + '\n' + TOOLS_NOTE, encoding='utf-8')
    os.chmod(instructions, 0o600)
    command, environment = pisar_command()
    pisar_args = ['--root', str(root), '--state-dir', str(state), '--ruwana', ruwana, 'mcp-serve']
    # The server keeps the user's HOME (Git identity, ruwana and pisar settings); only the Codex
    # processes it starts get the private home.
    server_env = {**server_environment(root, state, ruwana), **environment}
    server = {'command': command[0], 'args': [*command[1:], *pisar_args],
              'env': Inline({k: v for k, v in server_env.items()}),
              'enabled_tools': list(SERVER_TOOLS), 'tool_timeout_sec': SERVER_TOOL_TIMEOUT,
              'default_tools_approval_mode': 'approve'}
    tree = {
        'approval_policy': 'on-request',
        'web_search': 'disabled',
        'default_permissions': PROFILE,
        'model_instructions_file': str(instructions),
        **({'model_reasoning_effort': effort} if effort else {}),
        'features': {name: False for name in DISABLED_FEATURES},
        'permissions': {PROFILE: {
            # Reads: the root and what Codex needs to start; writes: the lesson drafts only.
            'filesystem': {':minimal': 'read', str(root): 'read', str(state / 'lessons'): 'write'},
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


def exec_argv(executable, cwd, answer, schema, instructions, model, effort, web_search):
    """``codex exec`` for a helper run (lesson reviewer, researcher).

    The helper uses the login of the main session (CODEX_HOME, see exec_environment) but none
    of its configuration: --ignore-user-config skips that config.toml, --ignore-rules the rules,
    and everything the helper needs is passed here: its own instructions, approval never, a
    read-only sandbox, web search as the caller says, and every feature in DISABLED_FEATURES off.
    The prompt arrives on stdin ("-"), the answer is written to ANSWER, matching SCHEMA.
    """
    features = [arg for name in DISABLED_FEATURES for arg in ('--disable', name)]
    return [executable, 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
            '--skip-git-repo-check', '-s', 'read-only', '--output-schema', str(schema),
            '-o', str(answer), '-C', str(cwd),
            '-c', f'model_instructions_file={json.dumps(str(instructions))}',
            '-c', 'approval_policy="never"', '-c', f'web_search="{web_search}"',
            *(['-c', f'model_reasoning_effort="{effort}"'] if effort else []),
            *features, *(['-m', model] if model else []), '-']


def exec_environment(config, home):
    """The helper's explicit environment: the main session's CODEX_HOME (the login), a private HOME."""
    env = {k: os.environ[k] for k in ENV_NAMES if k in os.environ}
    env.update(CODEX_HOME=str(config), HOME=str(home))
    return env


def helper_home(base, *parts):
    """A directory for a helper's files (instructions, schema), private and outside Git."""
    path = outside_git(Path(base) / 'agents' / EXECUTABLE / Path(*parts))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


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
    overrides, _ = checked_arguments(extra)
    main = settings.agent_options('codex')
    model = overrides.get('model', main['model'])
    effort = overrides.get('model_reasoning_effort', main['effort'])
    ruwana = _ruwana_path(str(ruwana))
    write_config(config, root, runtime.base, ruwana, effort, user_servers(root))
    codex_plugin.write_skills(config)
    env = environment(config, home, root, runtime.base, ruwana)
    os.chdir(root)
    os.execve(executable, [executable, '--no-daemon', *(['-m', model] if model else [])], env)
