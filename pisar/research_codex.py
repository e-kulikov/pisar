"""The Codex counterpart of research.py: web research in an isolated ``codex exec``.

Same bounds, guard and record as the Claude researcher (research.py runs the process,
validates the answer and stores the record). Here the researcher is ``codex exec`` with
a private CODEX_HOME, live web search and nothing else: no shell, no file tools, no
MCP servers, no plugins, no subagents, and a read-only sandbox. The question goes on
stdin; the answer is written to a file that matches the research output schema.
"""
import json
import os
from pathlib import Path
import shutil
from . import codex_agent, research, settings
from .operations import outside_git
from .safety import WikiError

HOME = 'researcher'
ANSWER = 'answer.json'
SCHEMA_FILE = 'schema.json'
INSTRUCTIONS_FILE = 'instructions.md'


def _write(path, text):
    path.write_text(text, encoding='utf-8')
    os.chmod(path, 0o600)


def prepare(base, model, effort):
    """The researcher's CODEX_HOME under the runtime directory BASE, with its configuration and schema."""
    home = outside_git(Path(base) / 'agents' / codex_agent.EXECUTABLE / HOME)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    instructions = home / INSTRUCTIONS_FILE
    _write(instructions, research.PROMPT)
    features = {name: False for name in codex_agent.DISABLED_FEATURES}
    tree = {'approval_policy': 'never', 'sandbox_mode': 'read-only', 'web_search': 'live',
            'model_instructions_file': str(instructions),
            **({'model_reasoning_effort': effort} if effort else {}),
            'features': features, 'mcp_servers': {}}
    _write(home / codex_agent.CONFIG, codex_agent.render(tree))
    _write(home / SCHEMA_FILE, json.dumps(research.SCHEMA))
    return home


def environment(home):
    """The explicit child environment: an allowlist, and the researcher's own CODEX_HOME and HOME."""
    env = {k: os.environ[k] for k in codex_agent.ENV_NAMES if k in os.environ}
    env.update(CODEX_HOME=str(home), HOME=str(home))
    return env


def command(executable, home, model, cwd):
    """The argument list; the answer file is ANSWER in CWD, the question arrives on stdin."""
    return [executable, 'exec', '--ephemeral', '--skip-git-repo-check', '-s', 'read-only',
            '--output-schema', str(home / SCHEMA_FILE), '-o', str(Path(cwd) / ANSWER), '-C', cwd,
            *(['-m', model] if model else []), '-']


def answer(output):
    """The model's answer (a JSON object written by codex exec), or a clear error."""
    try:
        value = json.loads(output.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise WikiError('codex output is not valid JSON') from None
    if not isinstance(value, dict):
        raise WikiError('codex output is not valid JSON: expected an object')
    return value


def which():
    executable = shutil.which(codex_agent.EXECUTABLE)
    if executable is None:
        raise WikiError(f'{codex_agent.EXECUTABLE} is not on PATH')
    return executable


def model_and_effort(model, effort):
    return (model or settings.agent_option('codex', 'model', 'researcher')[0],
            effort or settings.agent_option('codex', 'effort', 'researcher')[0])
