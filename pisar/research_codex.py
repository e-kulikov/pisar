"""The Codex counterpart of research.py: web research in an isolated ``codex exec``.

Same bounds, guard and record as the Claude researcher (research.py runs the process,
validates the answer and stores the record). Here the researcher is ``codex exec`` with
a private CODEX_HOME, live web search and nothing else: no shell, no file tools, no
MCP servers, no plugins, no subagents, and a read-only sandbox. The question goes on
stdin; the answer is written to a file that matches the research output schema.
"""
import json
import os
import shutil
from pathlib import Path
from . import codex_agent, research, settings
from .safety import WikiError

DIRECTORY = 'researcher'
ANSWER = 'answer.json'
SCHEMA_FILE = 'schema.json'
INSTRUCTIONS_FILE = 'instructions.md'


def prepare(base):
    """The researcher's instructions and output schema, in its own directory outside Git."""
    home = codex_agent.helper_home(base, DIRECTORY)
    instructions = home / INSTRUCTIONS_FILE
    instructions.write_text(research.PROMPT, encoding='utf-8')
    os.chmod(instructions, 0o600)
    schema = home / SCHEMA_FILE
    schema.write_text(json.dumps(research.SCHEMA), encoding='utf-8')
    os.chmod(schema, 0o600)
    return home


def environment(base):
    """The login of the main session (its CODEX_HOME) with a private HOME; nothing else of the user's."""
    main = codex_agent.helper_home(base)
    return codex_agent.exec_environment(main, main / codex_agent.HOME)


def command(executable, home, model, effort, cwd):
    """The argument list; the answer is ANSWER in CWD, the question arrives on stdin."""
    return codex_agent.exec_argv(executable, cwd, Path(cwd) / ANSWER, home / SCHEMA_FILE,
                                 home / INSTRUCTIONS_FILE, model, effort, 'live')


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
