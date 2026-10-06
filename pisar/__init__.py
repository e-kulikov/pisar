"""Offline, file-authoritative CLI for Git-backed knowledge repositories."""
from pathlib import Path
import tomllib


def _project_version():
    """pyproject.toml is the single version authority; builds embed a copy."""
    try:
        from ._version import VERSION  # generated inside release builds only
        return VERSION
    except ImportError:
        pass
    pyproject = Path(__file__).resolve().parents[1] / 'pyproject.toml'
    try:
        project = tomllib.loads(pyproject.read_text(encoding='utf-8'))['project']
        if project.get('name') == 'pisar':
            return project['version']
    except (OSError, KeyError, ValueError):
        pass
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version('pisar')
    except PackageNotFoundError:
        return 'unknown'


__version__ = _project_version()
