"""Commit messages: what a commit changed and why.

Off by default (config ``[commit_message] enabled = false``): the message is the deterministic
subject ``wiki: OPERATION``, with --what and --why added verbatim as its body. Enabled: a writer
agent with no tools turns the explanation, the affected spaces and the staged diff into the message.
Whatever the writer returns that is not a valid message stops the commit; there is no content-free
fallback. The writer runs like research does: an isolated, tool-less ``claude -p`` with a bounded
time and output, and a child environment built from an allowlist.
"""
import contextlib
import contextvars
import json
import os
from importlib.resources import files
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unicodedata
from . import codex_agent, guard, operations, research, research_codex, settings
from .safety import WikiError

PROMPT_NAME = 'commit-message-prompt.md'
WHAT_LIMIT = 200
WHY_LIMIT = 2000
CONTEXT_LIMIT = 8000
SUBJECT_LIMIT = 72
BODY_LIMIT = 2000
DIFF_LIMIT = 60000
PROMPT_LIMIT = 16 * 1024
STDOUT_LIMIT = 64 * 1024
STDERR_LIMIT = 16 * 1024  # drained and discarded: a writer's diagnostics are never echoed
TRAILER = 'Pisar-Operation'
TYPES = ('feat', 'fix', 'docs', 'style', 'refactor', 'perf', 'test', 'build', 'ci', 'chore', 'revert')
SUBJECT = re.compile(rf'(?:{"|".join(TYPES)})(?:\([a-z0-9][a-z0-9._/-]*\))?!?: \S.*')
# Local paths (best effort): a leading slash at a word boundary, a home shorthand, a drive letter, file: and path: URLs.
ABSOLUTE_PATH = re.compile(r'(?:^|[\s"\'`(\[<=,;])(?:/[\w.@-]|~/|[A-Za-z]:\\)|\b(?:file|path):/', re.M)
SECRET_CATEGORIES = ('private-key', 'jwt', 'api-token', 'credential')
# Commands that create commits, as (command, action); their --what and --why are checked before any write.
COMMITTING = (('save', None), ('capture', None), ('triage', 'accept'), ('lesson', 'accept'),
              ('domain', 'add'), ('space', 'create'), ('space', 'move'), ('space', 'archive'), ('space', 'restore'))
MISSING = 'commit messages are written by an agent when [commit_message] enabled = true: this command needs ' \
          '--what (what changed) and --why (why it changed)'

_REQUEST = contextvars.ContextVar('pisar_commit_request', default=None)


def add_flags(parser):
    """The explanation flags of a committing command."""
    parser.add_argument('--what', help='What changed, in one short line (required when the writer is enabled)')
    parser.add_argument('--why', help='Why it changed (required when the writer is enabled)')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--context', help='Short summary of this operation\'s earlier steps, for the writer')
    source.add_argument('--context-file', help='File holding that summary (UTF-8)')


def require(what, why):
    """Refuse a commit that needs an explanation but has none (the writer is enabled)."""
    if settings.commit_message()['enabled'] and _missing(what, why):
        raise WikiError(MISSING)


def _missing(what, why):
    return not (what and what.strip() and why and why.strip())


def plain(text, name, limit):
    """TEXT stripped and checked: nonempty, bounded, no control characters, no local paths, no secrets."""
    if not isinstance(text, str) or not text.strip():
        raise WikiError(f'{name} must not be empty')
    text = text.strip()
    if len(text) > limit:
        raise WikiError(f'{name} is too long (more than {limit} characters)')
    if any(unicodedata.category(c) == 'Cc' and c not in '\n\t' for c in text):
        raise WikiError(f'{name} contains control characters')
    if ABSOLUTE_PATH.search(text):
        raise WikiError(f'{name} contains an absolute local path; write repository-relative paths')
    if _secret(text):
        raise WikiError(f'{name} contains a secret-like value; remove it')
    return text


def _secret(text):
    return any(finding['category'] in SECRET_CATEGORIES for finding in guard.scan(text))


def _context(args):
    if getattr(args, 'context_file', None) is None:
        return None if getattr(args, 'context', None) is None else plain(args.context, '--context', CONTEXT_LIMIT)
    try:
        path = operations.external_path(args.context_file)  # refuses a symlink in any component
    except WikiError:
        raise WikiError('--context-file is not a readable file') from None
    if not path.is_file():
        raise WikiError('--context-file is not a readable file')
    with path.open('rb') as stream:
        data = stream.read(4 * CONTEXT_LIMIT + 1)  # Bounded: a larger file fails the length check below.
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise WikiError('--context-file is not UTF-8 text') from None
    return plain(text, '--context-file', CONTEXT_LIMIT)


def explain(args):
    """Set the explanation of a committing command from its flags; the token goes to reset()."""
    if (getattr(args, 'command', None), getattr(args, 'action', None)) not in COMMITTING:
        return None
    what = plain(args.what, '--what', WHAT_LIMIT) if getattr(args, 'what', None) is not None else None
    why = plain(args.why, '--why', WHY_LIMIT) if getattr(args, 'why', None) is not None else None
    require(what, why)
    # The context only ever reaches the writer: with the writer disabled it is neither read nor checked.
    context = _context(args) if settings.commit_message()['enabled'] else None
    return _REQUEST.set(dict(what=what, why=why, context=context))


@contextlib.contextmanager
def request(what=None, why=None, context=None):
    """Run the block with this explanation (in-process callers; the CLI uses explain())."""
    token = _REQUEST.set(dict(what=what, why=why, context=context))
    try:
        yield
    finally:
        _REQUEST.reset(token)


def reset(token):
    if token is not None:
        _REQUEST.reset(token)


def current():
    """The explanation of the running command (all None when the command is not explained)."""
    return _REQUEST.get() or dict(what=None, why=None, context=None)


def writer_input(changes, operation, explanation):
    """The data the writer reads: explanation, spaces, paths, earlier steps and the diff."""
    parts = [f'Operation: {operation}',
             f'Affected spaces: {", ".join(changes["spaces"]) or "none"}',
             'Changed paths:\n' + '\n'.join(f'- {path}' for path in changes['paths']),
             f'What (from the calling agent): {explanation["what"]}',
             f'Why (from the calling agent): {explanation["why"]}']
    if explanation.get('context'):
        parts.append(f'Earlier steps of this operation (summary from the calling agent):\n{explanation["context"]}')
    parts.append(f'Staged diff:\n{changes["diff"]}')
    return '\n\n'.join(parts) + '\n'


def prompt_text(options):
    """The bundled writer prompt, or the absolute prompt_file of the configuration."""
    if options.get('prompt_file') is None:
        return files('pisar').joinpath(PROMPT_NAME).read_text(encoding='utf-8')
    path = Path(options['prompt_file'])
    if not path.is_file():
        raise WikiError('commit_message.prompt_file is not a readable file')
    data = path.read_bytes()
    if len(data) > PROMPT_LIMIT:
        raise WikiError(f'commit_message.prompt_file is too large (more than {PROMPT_LIMIT} bytes)')
    return data.decode('utf-8')


def _reason(text):
    """Why TEXT is not an acceptable message (a fixed phrase), or None when it is."""
    if not text:
        return 'the message is empty'
    if any(unicodedata.category(c) == 'Cc' and c not in '\n\t' for c in text):
        return 'it contains control characters'
    lines = text.split('\n')
    if len(lines[0]) > SUBJECT_LIMIT or not SUBJECT.fullmatch(lines[0]):
        return 'the subject is not a Conventional Commits line of at most 72 characters'
    if len(lines) > 1 and lines[1].strip():
        return 'the subject must be followed by a blank line'
    if len('\n'.join(lines[2:]).strip()) > BODY_LIMIT:
        return 'the body is too long'
    if any(line.lstrip().lower().startswith(f'{TRAILER.lower()}:') for line in lines):
        return f'the message must not contain the {TRAILER} trailer'
    if ABSOLUTE_PATH.search(text):
        return 'the message contains an absolute local path'
    if _secret(text):
        return 'the message contains a secret-like value'
    return None


def checked(output, operation):
    """The writer's answer as a commit message with pisar's own trailer, or a WikiError (nothing is echoed)."""
    text = output.strip()
    fenced = re.fullmatch(r'```[\w-]*\n(.*?)\n```', text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    reason = _reason(text)
    if reason:
        raise WikiError(f'commit message writer returned an invalid message ({reason}); nothing was committed')
    return f'{text}\n\n{TRAILER}: {operation}'


def run_bounded(argv, text, cwd, env, timeout):
    """Run ARGV with TEXT on stdin under one deadline of TIMEOUT seconds, output bounded (see research.run_bounded).

    The child leads its own process group, which is always killed at the end. state is 'ok', 'timeout',
    'overflow', or 'orphan' when the child exited but a descendant kept the output pipes open."""
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=cwd, env=env, start_new_session=True)

    def stop():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    out, err, overflow = bytearray(), bytearray(), threading.Event()
    threads = [threading.Thread(target=research._drain, args=(stream, limit, sink, overflow, stop), daemon=True)
               for stream, limit, sink in ((process.stdout, STDOUT_LIMIT, out), (process.stderr, STDERR_LIMIT, err))]
    threads.append(threading.Thread(target=research._feed, args=(process.stdin, text.encode('utf-8')), daemon=True))
    for thread in threads:
        thread.start()
    state = 'ok'
    try:
        process.wait(timeout=max(deadline - time.monotonic(), 0))
        grace = time.monotonic() + min(research.PIPE_GRACE, max(deadline - time.monotonic(), 0))
        for thread in threads[:2]:
            thread.join(timeout=max(grace - time.monotonic(), 0))
        if any(thread.is_alive() for thread in threads[:2]):
            state = 'orphan'
    except subprocess.TimeoutExpired:
        state = 'timeout'
    finally:
        stop()
        process.wait()
        for thread, stream in zip(threads, (process.stdout, process.stderr, process.stdin)):
            thread.join(timeout=2)
            if not thread.is_alive():
                try:
                    stream.close()
                except OSError:
                    pass
    if overflow.is_set():
        state = 'overflow'
    return research.Completed(process.returncode, bytes(out), bytes(err), state)


def _finished(done, timeout):
    """Refuse a writer run that did not finish cleanly; the messages are fixed and echo nothing."""
    if done.state == 'timeout':
        raise WikiError(f'the commit message writer did not finish within {timeout} seconds; nothing was committed')
    if done.state == 'orphan':
        raise WikiError('the commit message writer left background processes holding its output; they were '
                        'stopped; nothing was committed')
    if done.state == 'overflow':
        raise WikiError('the commit message writer output is too large; nothing was committed')
    if done.returncode != 0:
        raise WikiError(f'the commit message writer exited with status {done.returncode}; nothing was committed')


def _claude(prompt, text, options, root, base):
    """The claude backend: ``claude -p`` with no tools, no MCP, no plugins, no session, its own configuration."""
    executable = shutil.which('claude')
    if executable is None:
        raise WikiError('claude is not on PATH')
    argv = [executable, '-p', '--output-format', 'text', '--system-prompt', prompt, '--tools=',
            '--safe-mode', '--strict-mcp-config', '--no-session-persistence',
            *(['--model', options['model']] if options.get('model') else []),
            *(['--effort', options['effort']] if options.get('effort') else [])]
    config = research.config_dir(base)
    env = research.child_environment(config)
    timeout = options['timeout']
    with tempfile.TemporaryDirectory(prefix='pisar-commit-message-', dir=research.scratch_base(root, base)) as cwd:
        done = run_bounded(argv, text, cwd, env, timeout)
    _finished(done, timeout)
    return done.stdout.decode('utf-8', errors='replace')


CODEX_DIRECTORY = 'commit-message'
CODEX_ANSWER = 'answer.json'
CODEX_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['message'],
                'properties': {'message': {'type': 'string'}}}


def _codex(prompt, text, options, root, base):
    """The codex backend: ``codex exec`` with no tools (read-only, no shell, web, MCP, plugins or hooks).

    It uses the login of the main session and a private HOME (research_codex.environment); the instructions
    and the answer schema are written to the writer's own directory. The answer is a JSON object with the
    message, which the caller checks like the claude output."""
    executable = research_codex.which()
    home = codex_agent.helper_home(base, CODEX_DIRECTORY)
    instructions, schema = home / 'instructions.md', home / 'schema.json'
    for path, content in ((instructions, prompt), (schema, json.dumps(CODEX_SCHEMA))):
        path.write_text(content, encoding='utf-8')
        os.chmod(path, 0o600)
    env = research_codex.environment(base)
    timeout = options['timeout']
    with tempfile.TemporaryDirectory(prefix='pisar-commit-message-', dir=research.scratch_base(root, base)) as cwd:
        answer = Path(cwd) / CODEX_ANSWER
        argv = codex_agent.exec_argv(executable, cwd, answer, schema, instructions,
                                     options.get('model'), options.get('effort'), 'disabled')
        done = run_bounded(argv, text, cwd, env, timeout)
        output = b''
        if answer.is_file():
            with answer.open('rb') as stream:
                output = stream.read(STDOUT_LIMIT + 1)  # One byte more than the limit is the overflow mark.
    _finished(done, timeout)
    if len(output) > STDOUT_LIMIT:
        raise WikiError('the commit message writer output is too large; nothing was committed')
    try:
        value = json.loads(output.decode('utf-8'))
    except ValueError:
        value = None
    if not isinstance(value, dict) or not isinstance(value.get('message'), str):
        raise WikiError('the commit message writer returned an invalid answer; nothing was committed')
    return value['message']


BACKENDS = {'claude': _claude, 'codex': _codex}


def compose(changes, operation, explanation, options, root, base):
    """The full message of one commit, without its final newline.

    CHANGES holds paths, spaces, diff and tree; EXPLANATION holds what, why and context. Disabled, the
    deterministic message is returned and nothing runs. Enabled, the configured backend writes it."""
    what, why = explanation.get('what'), explanation.get('why')
    if not options['enabled']:
        parts = [plain(text, name, limit) for text, name, limit in ((what, '--what', WHAT_LIMIT),
                                                                    (why, '--why', WHY_LIMIT)) if text is not None]
        return '\n\n'.join([f'wiki: {operation}', *parts])
    if _missing(what, why):
        raise WikiError(MISSING)
    backend = BACKENDS[options['agent']]
    prompt = prompt_text(options)
    output = backend(prompt, writer_input(changes, operation, explanation), options, root, base)
    return checked(output, operation)
