"""User settings: explicit flag > PISAR_* environment > config file > default.

Only PISAR_* variables configure pisar. WIKI_ROOT is never read here; it exists
solely as ruwana's legacy protocol, set for that child process (see ruwana.py).
Every resolution reads and validates the config file (see config.py), so an
invalid file is an error even when flags or the environment decide the value.
"""
import os
from pathlib import Path
from . import config

ENVIRONMENT = {'root': 'PISAR_ROOT', 'state_dir': 'PISAR_STATE_DIR', 'ruwana': 'PISAR_RUWANA_BIN'}
# Lesson reviewer and research subagents; the main session passes nothing when unset.
ROLE_DEFAULTS = {'reviewer': {'model': 'opus', 'effort': 'high'},
                 'researcher': {'model': 'sonnet', 'effort': 'medium'}}


def _environment(name):
    return os.environ.get(name) or None


def data_home():
    """XDG_DATA_HOME when absolute (relative values are ignored), else ~/.local/share."""
    value = os.environ.get('XDG_DATA_HOME')
    if value and Path(value).is_absolute():
        return Path(value)
    return Path.home() / '.local/share'


def _default(name):
    if name == 'root':
        return data_home() / 'wiki'
    if name == 'state_dir':
        return data_home() / 'pisar'
    return 'ruwana'


def resolve(name, flag=None):
    """(value, source) for root, state_dir or ruwana; source is flag|env|config|default."""
    configuration = config.load()
    if flag:
        return flag, 'flag'
    if value := _environment(ENVIRONMENT[name]):
        return value, 'env'
    if name in configuration:
        return configuration[name], 'config'
    return _default(name), 'default'


def root(flag=None):
    return resolve('root', flag)[0]


def state_dir(flag=None):
    return resolve('state_dir', flag)[0]


def ruwana(flag=None):
    return resolve('ruwana', flag)[0]


def default_agent():
    """(agent, source) used by a bare ``--agent``; agent is None when unset."""
    configuration = config.load()
    if 'default_agent' in configuration:
        return configuration['default_agent'], 'config'
    return None, 'default'


def agent_option(agent, key, role=None):
    """(value, source) of ``model`` or ``effort`` for the main session or a ROLE subagent."""
    options = config.load().get('agents', {}).get(agent, {})
    if role is not None:
        options = options.get(role, {})
    if key in options:
        return options[key], 'config'
    return ROLE_DEFAULTS.get(role, {}).get(key), 'default'


def agent_options(agent, role=None):
    """{'model': ..., 'effort': ...} for the main session (None when unset) or a ROLE subagent."""
    return {key: agent_option(agent, key, role)[0] for key in config.AGENT_KEYS}


def show(flags):
    """Every effective setting with its source; FLAGS maps setting names to raw flag values."""
    result = {}
    for name in ENVIRONMENT:
        value, source = resolve(name, flags.get(name))
        result[name] = dict(value=str(value), source=source)
    value, source = default_agent()
    result['default_agent'] = dict(value=value, source=source)
    for agent in config.AGENTS:
        for role in (None, *config.ROLES):
            prefix = '.'.join(filter(None, ('agents', agent, role)))
            for key in config.AGENT_KEYS:
                value, source = agent_option(agent, key, role)
                result[f'{prefix}.{key}'] = dict(value=value, source=source)
    file = config.path()
    return dict(file=dict(path=str(file), exists=file.exists()), settings=result)
