"""User settings: explicit flags override PISAR_* environment, then defaults.

Only PISAR_* variables configure pisar. WIKI_ROOT is never read here; it exists
solely as ruwana's legacy protocol, set for that child process (see ruwana.py).
"""
import os
from pathlib import Path


def _environment(name):
    return os.environ.get(name) or None


def data_home():
    """XDG_DATA_HOME when absolute (relative values are ignored), else ~/.local/share."""
    value = os.environ.get('XDG_DATA_HOME')
    if value and Path(value).is_absolute():
        return Path(value)
    return Path.home() / '.local/share'


def root(flag=None):
    return flag or _environment('PISAR_ROOT') or data_home() / 'wiki'


def state_dir(flag=None):
    return flag or _environment('PISAR_STATE_DIR') or data_home() / 'pisar'


def ruwana(flag=None):
    return flag or _environment('PISAR_RUWANA_BIN') or 'ruwana'
