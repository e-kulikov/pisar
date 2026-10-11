"""The optional user configuration file ``$XDG_CONFIG_HOME/pisar/config.toml``.

Only reads and validates it; precedence over flags and environment lives in
settings.py. A missing file is an empty configuration. Unknown keys, wrong types,
empty strings and relative paths are errors naming the file and the key.
"""
import os
from pathlib import Path
import tomllib
from .safety import WikiError

AGENTS = ('claude', 'codex')
ROLES = ('reviewer', 'researcher')
AGENT_KEYS = ('model', 'effort')
# The commit-message writer ([commit_message]): off by default; timeout bounds in seconds.
COMMIT_MESSAGE_DEFAULTS = dict(enabled=False, agent='claude', model='sonnet', effort='medium', timeout=120,
                               prompt_file=None)
COMMIT_MESSAGE_TIMEOUT = (10, 600)
# The agents that can write commit messages; commit_message.BACKENDS must have exactly these names.
COMMIT_MESSAGE_AGENTS = ('claude', 'codex')


def config_home():
    """XDG_CONFIG_HOME when absolute (relative values are ignored), else ~/.config."""
    value = os.environ.get('XDG_CONFIG_HOME')
    if value and Path(value).is_absolute():
        return Path(value)
    return Path.home() / '.config'


def path():
    return config_home() / 'pisar' / 'config.toml'


def load(file=None):
    """The validated configuration as a dict; {} when the file does not exist."""
    file = Path(file or path())
    if not file.exists():
        return {}
    if not file.is_file():
        raise WikiError(f'{file}: configuration is not a regular file')
    try:
        data = tomllib.loads(file.read_text(encoding='utf-8'))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise WikiError(f'{file}: invalid TOML: {error}') from None
    _validate(file, data)
    return data


def _error(file, key, problem):
    return WikiError(f'{file}: {key}: {problem}')


def _string(file, key, value):
    if not isinstance(value, str):
        raise _error(file, key, f'must be a string, not {type(value).__name__}')
    if not value.strip():
        raise _error(file, key, 'must not be empty')


def _table(file, key, value, allowed):
    if not isinstance(value, dict):
        raise _error(file, key, f'must be a table, not {type(value).__name__}')
    for name in value:
        if name not in allowed:
            raise _error(file, f'{key}.{name}' if key else name, 'unknown key')


def _validate(file, data):
    _table(file, '', data, ('default_agent', 'research_agent', 'root', 'state_dir', 'ruwana', 'agents',
                            'commit_message'))
    if 'commit_message' in data:
        _validate_commit_message(file, data['commit_message'])
    for key in ('root', 'state_dir'):
        if key in data:
            _string(file, key, data[key])
            if not Path(data[key]).is_absolute():
                raise _error(file, key, f'must be an absolute path: {data[key]}')
    if 'ruwana' in data:
        _string(file, 'ruwana', data['ruwana'])
        # A relative path would depend on the invocation directory; allow a
        # command name (looked up on PATH) or an absolute path.
        value = data['ruwana']
        if os.sep in value and not Path(value).is_absolute():
            raise _error(file, 'ruwana', f'must be a command name or an absolute path: {value}')
    if 'default_agent' in data:
        _string(file, 'default_agent', data['default_agent'])
        if data['default_agent'] not in AGENTS:
            raise _error(file, 'default_agent', f'unsupported agent: {data["default_agent"]}')
    if 'research_agent' in data:
        _string(file, 'research_agent', data['research_agent'])
        if data['research_agent'] not in AGENTS:
            raise _error(file, 'research_agent', f'unsupported agent: {data["research_agent"]}')
    if 'agents' in data:
        _table(file, 'agents', data['agents'], AGENTS)
        for agent, options in data['agents'].items():
            key = f'agents.{agent}'
            _table(file, key, options, (*AGENT_KEYS, *ROLES))
            for name, value in options.items():
                if name in ROLES:
                    _table(file, f'{key}.{name}', value, AGENT_KEYS)
                    for option, setting in value.items():
                        _string(file, f'{key}.{name}.{option}', setting)
                else:
                    _string(file, f'{key}.{name}', value)


def _validate_commit_message(file, value):
    _table(file, 'commit_message', value, tuple(COMMIT_MESSAGE_DEFAULTS))
    if 'enabled' in value and not isinstance(value['enabled'], bool):
        raise _error(file, 'commit_message.enabled', 'must be true or false')
    if 'agent' in value:
        _string(file, 'commit_message.agent', value['agent'])
        if value['agent'] not in COMMIT_MESSAGE_AGENTS:
            raise _error(file, 'commit_message.agent', f'unsupported writer agent: {value["agent"]} '
                                                       f'(supported: {", ".join(COMMIT_MESSAGE_AGENTS)})')
    for key in ('model', 'effort'):
        if key in value:
            _string(file, f'commit_message.{key}', value[key])
    if 'timeout' in value:
        low, high = COMMIT_MESSAGE_TIMEOUT
        if type(value['timeout']) is not int or not low <= value['timeout'] <= high:
            raise _error(file, 'commit_message.timeout', f'must be an integer from {low} to {high} seconds')
    if 'prompt_file' in value:
        _string(file, 'commit_message.prompt_file', value['prompt_file'])
        if not Path(value['prompt_file']).is_absolute():
            raise _error(file, 'commit_message.prompt_file', f'must be an absolute path: {value["prompt_file"]}')
