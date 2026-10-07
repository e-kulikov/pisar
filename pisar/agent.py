"""Run an AI agent CLI confined to this knowledge repository (``--agent``).

The agent gets its own configuration directory under the pisar state directory,
a replaced system prompt, a minimal tool set and a narrow allowlist, so none of
the user's global settings, hooks, plugins, skills or MCP servers apply.
"""
import os
from pathlib import Path
import shutil
from .operations import Runtime, outside_git
from .safety import WikiError

AGENTS = ('claude',)
PROMPT = 'agent-prompt.md'
MCP_CONFIG = '.mcp.json'

# Variadic option lists must each be followed by another option, never by an
# argument meant for the agent.
CLAUDE_TOOLS = 'Read,Write,Edit,Glob,Grep,Bash'
# Only read-only git, staging, commit, rename of tracked files, and the pisar
# subcommands. No bare `git *`/`pisar *` (git aliases and `-c` run arbitrary
# code; `pisar --ruwana BIN` runs any binary) and no `mv`.
CLAUDE_ALLOWED = (
    'Bash(git status*)', 'Bash(git diff*)', 'Bash(git log*)',
    'Bash(git add *)', 'Bash(git commit *)', 'Bash(git mv *)',
    'Bash(pisar spaces*)', 'Bash(pisar check*)', 'Bash(pisar search *)',
    'Bash(pisar read *)', 'Bash(pisar inventory*)', 'Bash(pisar capture *)',
    'Bash(pisar save *)', 'Bash(pisar triage *)',
)
CLAUDE_DENIED = ('Bash(git push*)', 'Edit(.git/**)')


def prompt_text():
    from importlib.resources import files
    return files('pisar').joinpath(PROMPT).read_text(encoding='utf-8')


def mcp_config(root):
    """The root's own MCP servers file, when it is a regular file (never a symlink)."""
    path = Path(root) / MCP_CONFIG
    return path if path.is_file() and not path.is_symlink() else None


def claude_command(executable, prompt, extra=(), mcp=None):
    """Caller's own arguments come first, so variadic options cannot swallow them.

    MCP is strict: only servers from `mcp` (the root's .mcp.json) are loaded.
    """
    return [executable, *extra,
            '--system-prompt', prompt,
            f'--tools={CLAUDE_TOOLS}',
            '--allowedTools', *CLAUDE_ALLOWED,
            '--disallowedTools', *CLAUDE_DENIED,
            *(['--mcp-config', str(mcp)] if mcp else []),
            '--strict-mcp-config']


def launch(agent, root, state_dir, ruwana, extra=()):
    """Replace this process with the agent. Returns only by raising."""
    if agent not in AGENTS:
        raise WikiError(f'unsupported agent: {agent}')
    root = Path(root).resolve()
    if not root.is_dir():
        raise WikiError(f'wiki root is not a directory: {root}')
    executable = shutil.which(agent)
    if executable is None:
        raise WikiError(f'{agent} is not on PATH')
    runtime = Runtime(state_dir, root)
    config = runtime.base / 'agents' / agent
    outside_git(config)
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config, 0o700)
    env = {k: v for k, v in os.environ.items() if k != 'WIKI_ROOT'}
    env.update(CLAUDE_CONFIG_DIR=str(config), PISAR_ROOT=str(root),
               PISAR_STATE_DIR=str(runtime.base), PISAR_RUWANA_BIN=str(ruwana))
    os.chdir(root)
    command = claude_command(executable, prompt_text(), extra, mcp_config(root))
    os.execve(executable, command, env)
