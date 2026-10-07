"""Run an AI agent CLI confined to this knowledge repository (``--agent``).

The agent gets its own configuration directory under the pisar state directory,
a replaced system prompt, a minimal tool set and a narrow allowlist, so none of
the user's global settings, hooks, plugins, skills or MCP servers apply.
"""
import os
from pathlib import Path
import shutil
import json
from . import claude_plugin, settings
from .config import AGENTS, path as config_path
from .operations import Runtime, outside_git
from .safety import WikiError

# `--agent` given without a value: start the configured default_agent.
DEFAULT = object()
PROMPT = 'agent-prompt.md'
MCP_CONFIG = '.mcp.json'

# Variadic option lists must each be followed by another option, never by an
# argument meant for the agent.
CLAUDE_TOOLS = 'Read,Write,Edit,Glob,Grep,Bash,Agent,Skill'
# Only read-only git, staging, commit, rename of tracked files, and the pisar
# subcommands. No bare `git *`/`pisar *` (git aliases and `-c` run arbitrary
# code; `pisar --ruwana BIN` runs any binary) and no `mv`.
CLAUDE_ALLOWED = (
    'Bash(git status*)', 'Bash(git diff*)', 'Bash(git log*)',
    'Bash(git add *)', 'Bash(git commit *)', 'Bash(git mv *)',
    'Bash(pisar spaces*)', 'Bash(pisar check*)', 'Bash(pisar search *)',
    'Bash(pisar read *)', 'Bash(pisar inventory*)', 'Bash(pisar capture *)',
    'Bash(pisar save *)', 'Bash(pisar triage *)',
    'Bash(pisar domain *)', 'Bash(pisar space *)', 'Bash(pisar guard *)',
    'Bash(pisar config *)', 'Bash(pisar research *)',
    # Not `lesson accept`: it is in CLAUDE_ASK and always needs the user.
    'Bash(pisar lesson start *)', 'Bash(pisar lesson check *)',
    'Bash(pisar lesson review *)', 'Bash(pisar lesson show *)',
    'Bash(pisar lesson discard *)',
    # The mandatory lesson review and the plugin's own skills.
    'Agent(pisar:lesson-reviewer)', 'Skill(pisar:pisar)', 'Skill(pisar:lessons)',
    'Skill(pisar:research)',
)
# Deny rules win over allow rules. The Agent tool cannot be limited to one subagent
# by an allowlist, so every built-in subagent type (which also covers a call without
# subagent_type, the general-purpose default) is denied by name.
CLAUDE_DENIED = ('Bash(git push*)', 'Edit(.git/**)',
                 'Agent(Explore)', 'Agent(general-purpose)', 'Agent(Plan)', 'Agent(claude)',
                 'Agent(statusline-setup)', 'Agent(claude-code-guide)')
# `ask` overrides `allow`, so accepting a lesson stays a user decision.
CLAUDE_ASK = ('Bash(pisar lesson accept *)',)


def lesson_workspace_rule(state):
    """Edit permission for the lesson drafts only; an absolute path needs the `//` prefix."""
    return f'Edit(/{Path(state) / "lessons"}/**)'


def prompt_text():
    from importlib.resources import files
    return files('pisar').joinpath(PROMPT).read_text(encoding='utf-8')


def mcp_config(root):
    """The root's own MCP servers file, when it is a regular file (never a symlink)."""
    path = Path(root) / MCP_CONFIG
    return path if path.is_file() and not path.is_symlink() else None


def session_options(agent, extra=()):
    """--model/--effort for the main session from the config file, verbatim.

    Nothing is passed when unset, or when the caller's own arguments already set it.
    """
    options = []
    for key, value in settings.agent_options(agent).items():
        flag = f'--{key}'
        if value is not None and not any(a == flag or a.startswith(f'{flag}=') for a in extra):
            options += [flag, value]
    return options


def claude_command(executable, prompt, extra=(), mcp=None, options=(), plugin=None, workspace=None):
    """Caller's own arguments come first, so variadic options cannot swallow them.

    MCP is strict: only servers from `mcp` (the root's .mcp.json) are loaded.
    `plugin` is the extracted plugin directory, `workspace` the Edit rule for lesson drafts.
    """
    return [executable, *extra, *options,
            *(['--plugin-dir', str(plugin)] if plugin else []),
            '--settings', json.dumps({'permissions': {'ask': list(CLAUDE_ASK)}}),
            '--system-prompt', prompt,
            f'--tools={CLAUDE_TOOLS}',
            '--allowedTools', *CLAUDE_ALLOWED, *([workspace] if workspace else []),
            '--disallowedTools', *CLAUDE_DENIED,
            *(['--mcp-config', str(mcp)] if mcp else []),
            '--strict-mcp-config']


def selected(agent):
    """The agent to start: AGENT itself, or the configured default_agent for DEFAULT."""
    if agent is DEFAULT:
        agent, _ = settings.default_agent()
        if agent is None:
            raise WikiError('--agent without a value needs default_agent in the config file '
                            f'({config_path()})')
    return agent


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
    lessons = outside_git(runtime.base / 'lessons')
    plugin = claude_plugin.extract(config, settings.agent_options(agent, 'reviewer'))
    lessons.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chdir(root)
    command = claude_command(executable, prompt_text(), extra, mcp_config(root),
                             session_options(agent, extra), plugin, lesson_workspace_rule(runtime.base))
    os.execve(executable, command, env)
