"""``pisar mcp-serve``: the stdio MCP server through which the Codex agent acts.

Codex's own shell is disabled in the agent session (see codex_agent.py), so every
operation the Claude agent may run through Bash is offered here as a tool that
checks its arguments first. Nothing is ever run through a shell: each allowed
operation is one fixed program (this pisar, or git) with a validated argument
list. Tools are the same list as CLAUDE_ALLOWED in agent.py, plus
``lesson_accept``, which the user must confirm through an MCP elicitation.
"""
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from . import __version__
from . import codex_reviewer, lesson
from .codex_agent import pisar_command, server_environment
from .operations import atomic_bytes
from .safety import WikiError, concrete_id, sha256
from .spaces import Wiki

PROTOCOL = '2025-06-18'
MAX_OUTPUT = 1024 * 1024
TIMEOUT = 1800
# The pisar subcommands the agent may run, as argument prefixes. `lesson accept` is not here:
# it is `lesson_accept`, which always asks the user (see Server.accept).
PISAR = (('spaces',), ('check',), ('search',), ('read',), ('inventory',), ('capture',), ('save',),
         ('triage',), ('domain',), ('space',), ('guard',), ('config',), ('research',),
         ('lesson', 'start'), ('lesson', 'check'), ('lesson', 'review'), ('lesson', 'show'),
         ('lesson', 'discard'))
ACCEPT = ('lesson', 'accept')
# Global options belong to this server; the agent may never pass them, anywhere in its arguments.
GLOBAL = ('--root', '--state-dir', '--ruwana', '--agent', '--skill', '--version')
# Git: each subcommand has its own list of options. Only these exact spellings (or the
# patterns) are passed; git also accepts unambiguous prefixes of long options, so nothing is
# matched by prefix. A value-taking option consumes the next argument. Read-only subcommands
# always get --no-ext-diff --no-textconv (no helper program runs); -c, -C and every other
# option are refused, as is anything that reads files or runs programs (-F, -O, --output,
# --no-index, --exec-path, --interactive, ...).
GIT = {
    'status': dict(
        exact={'-s', '--short', '-b', '--branch', '--porcelain', '-z', '-v', '--verbose', '--long',
               '--ahead-behind', '--no-ahead-behind', '--no-renames', '--renames', '--column',
               '--no-column', '--ignored', '-u', '--untracked-files'},
        values=set(), patterns=(r'-u(no|normal|all)', r'--untracked-files=(no|normal|all)',
                                r'--porcelain=v[12]', r'--ignored=(traditional|matching|no)'),
        fixed=()),
    'diff': dict(
        exact={'--stat', '--shortstat', '--numstat', '--name-only', '--name-status', '--cached',
               '--staged', '-p', '-u', '--patch', '--no-color', '--no-prefix', '--compact-summary',
               '--raw', '--exit-code', '--quiet'},
        values=set(), patterns=(r'-U\d+', r'--unified=\d+'),
        fixed=('--no-ext-diff', '--no-textconv')),
    'log': dict(
        exact={'--oneline', '--stat', '--shortstat', '--numstat', '--name-only', '--name-status', '-p',
               '--patch', '--graph', '--decorate', '--no-decorate', '--reverse', '--first-parent',
               '--no-merges', '--merges', '--all', '--no-color', '--abbrev-commit', '--no-abbrev-commit',
               '--raw'},
        values={'-n', '--max-count', '--since', '--until', '--after', '--before', '--author', '--grep',
                '--format', '--pretty', '--date'},
        patterns=(r'-\d+', r'--max-count=\d+', r'--(since|until|after|before|author|grep|format|pretty|date)=.*'),
        fixed=('--no-ext-diff', '--no-textconv')),
    'add': dict(
        exact={'-A', '--all', '-u', '--update', '-N', '--intent-to-add', '-f', '--force', '-v', '--verbose',
               '--dry-run', '-n', '--ignore-removal'},
        values=set(), patterns=(), fixed=()),
    'commit': dict(
        exact={'-a', '--all', '--amend', '--no-edit', '--allow-empty', '--no-verify', '-n', '-q', '--quiet',
               '-v', '--verbose', '-o', '--only', '-i', '--include'},
        values={'-m', '--message'}, patterns=(r'--message=.*',), fixed=()),
    'mv': dict(
        exact={'-f', '--force', '-k', '-n', '--dry-run', '-v', '--verbose'},
        values=set(), patterns=(), fixed=()),
}
# Refused for every subcommand, before its own list is consulted.
GIT_BLOCKED = ('-c', '-C', '--config', '--git-dir', '--work-tree', '--exec')
SCHEMA_ARGS = {'type': 'object', 'properties': {'args': {'type': 'array', 'items': {'type': 'string'}}},
               'required': ['args'], 'additionalProperties': False}


def _tool(name, description, schema=SCHEMA_ARGS):
    return {'name': name, 'description': description, 'inputSchema': schema}


TOOLS = (
    _tool('pisar', 'Run an allowed pisar command. Pass the arguments after `pisar` as a list, '
                   'e.g. ["search", "--space", "work/alpha", "launch"]. Global options are never accepted.'),
    _tool('git', 'Run git in the knowledge root. Allowed: status, diff, log (read-only), add, commit, mv. '
                 'Arguments follow the subcommand, e.g. ["commit", "-m", "Message"].'),
    _tool('lesson_accept', 'Write a reviewed lesson revision into its destination space. The user is asked '
                           'to confirm first; the arguments are those of `pisar lesson accept`, e.g. '
                           '["--batch", "ID", "--keep", "F1"].'),
    _tool('lesson_review', 'Have the senior reviewer check the current revision of a lesson batch and record '
                           'its verdict. Arguments: ["--batch", "ID"].'),
    _tool('lesson_draft_read', 'Read the draft of an open lesson batch: its text and sha256.',
          {'type': 'object', 'properties': {'batch': {'type': 'string'}}, 'required': ['batch'],
           'additionalProperties': False}),
    _tool('lesson_draft_write', 'Replace the whole draft of an open lesson batch. Pass the sha256 of the text '
                                'you last read; the write is refused if the draft changed since.',
          {'type': 'object', 'properties': {'batch': {'type': 'string'}, 'sha256': {'type': 'string'},
                                            'text': {'type': 'string'}},
           'required': ['batch', 'sha256', 'text'], 'additionalProperties': False}),
)
TOOL_NAMES = tuple(tool['name'] for tool in TOOLS)
DRAFT_LIMIT = 256 * 1024  # bytes of one draft


class Refused(WikiError):
    """An operation this server does not run; the message is safe to show the agent."""


def check_pisar(args):
    """The argument list for `pisar ARGS`, or Refused. Global options are refused anywhere."""
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args) or not args:
        raise Refused('pisar: args must be a non-empty list of strings')
    if any(a in GLOBAL or a.startswith(tuple(g + '=' for g in GLOBAL)) for a in args):
        raise Refused('pisar: global options are not accepted; the server sets them')
    if tuple(args[:2]) == ACCEPT:
        raise Refused('pisar: use the lesson_accept tool; accepting needs the user\'s confirmation')
    if not any(tuple(args[:len(prefix)]) == prefix for prefix in PISAR):
        raise Refused(f'pisar: not an allowed command: {args[0]}')
    return list(args)


def _git_option(rule, token, following):
    """(tokens to pass, following arguments left) for one option token; Refused when not allowed."""
    name = token.split('=', 1)[0]
    if token in rule['exact']:
        return [token], following
    if token in rule['values'] and following:
        return [token, following[0]], following[1:]
    if '=' in token and name in rule['values'] and any(re.fullmatch(p, token) for p in rule['patterns']):
        return [token], following
    if any(re.fullmatch(p, token) for p in rule['patterns']):
        return [token], following
    raise Refused(f'git: option not allowed: {name}')


def check_git(args):
    """The argument list for `git ARGS` (the subcommand first), or Refused."""
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args) or not args:
        raise Refused('git: args must be a non-empty list of strings')
    subcommand, rest = args[0], args[1:]
    if subcommand not in GIT:
        raise Refused(f'git: not an allowed subcommand: {subcommand}')
    rule = GIT[subcommand]
    checked = []
    while rest:
        token = rest.pop(0)
        if token == '--':
            checked.append(token)
            checked.extend(rest)  # pathspecs only after this point
            break
        if not token.startswith('-'):
            checked.append(token)
            continue
        if token in GIT_BLOCKED or any(token.startswith(blocked + '=') for blocked in GIT_BLOCKED):
            raise Refused(f'git: option not allowed: {token.split("=", 1)[0]}')
        passed, rest = _git_option(rule, token, rest)
        checked.extend(passed)
    return [subcommand, *rule['fixed'], *checked]


class Server:
    """A line-delimited JSON-RPC 2.0 peer on stdin and stdout (MCP stdio transport)."""

    def __init__(self, root, state, ruwana, reader, writer, environment):
        self.root = Path(root)
        self.state = Path(state)
        self.ruwana = ruwana
        self.reader = reader
        self.writer = writer
        self.environment = environment
        self.client = {}
        self.next_id = 0

    # Transport

    def send(self, message):
        self.writer.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.writer.flush()

    def read(self):
        """The next message that is a JSON object, or EOF at the end of the connection.

        Anything else on a line (not JSON, or JSON that is not an object) gets an error answer and
        reading goes on: `null` is not the end of input.
        """
        while True:
            line = self.reader.readline()
            if not line:
                return EOF
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}})
                continue
            if isinstance(message, dict):
                return message
            self.send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'invalid request'}})

    def serve(self):
        while (message := self.read()) is not EOF:
            self.dispatch(message)

    def dispatch(self, message):
        if not isinstance(message, dict) or 'method' not in message:
            return  # a response nobody waits for, or junk
        ident = message.get('id')
        method = message['method']
        if ident is None:
            return  # notifications need no answer
        try:
            result = self.call(method, message.get('params') or {})
        except _Unknown:
            self.send({'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32601, 'message': f'unknown method: {method}'}})
            return
        except (WikiError, ValueError, TypeError) as error:
            self.send({'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32602, 'message': str(error)}})
            return
        except Exception:  # malformed parameters (not objects, ...): an error answer, never a dead server
            self.send({'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32602, 'message': 'invalid parameters'}})
            return
        self.send({'jsonrpc': '2.0', 'id': ident, 'result': result})

    def call(self, method, params):
        if method == 'initialize':
            self.client = params.get('capabilities') or {}
            return {'protocolVersion': PROTOCOL, 'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'pisar', 'version': __version__}}
        if method == 'ping':
            return {}
        if method == 'tools/list':
            return {'tools': list(TOOLS)}
        if method == 'tools/call':
            return self.tool(params.get('name'), params.get('arguments') or {})
        raise _Unknown(method)

    # Tools

    def tool(self, name, arguments):
        args = arguments.get('args')
        try:
            if name == 'pisar':
                return self.text(self.capture([*self.pisar(), *check_pisar(args)]))
            if name == 'git':
                return self.text(self.capture(['git', '-C', str(self.root), '-c', 'core.fsmonitor=false',
                                               '-c', 'core.pager=cat', *check_git(args)]))
            if name == 'lesson_accept':
                return self.text(self.accept(args))
            if name == 'lesson_review':
                return self.text(self.review(args))
            if name in ('lesson_draft_read', 'lesson_draft_write'):
                return self.text(self.draft(name, arguments))
        except WikiError as error:
            return self.text(str(error), error=True)
        raise _Unknown(f'tool {name}')

    @staticmethod
    def text(output, error=False):
        result = {'content': [{'type': 'text', 'text': output}]}
        if error:
            result['isError'] = True
        return result

    def pisar(self):
        """This pisar again, with the server's own root, state directory and ruwana."""
        command, _ = pisar_command()
        return [*command, '--root', str(self.root), '--state-dir', str(self.state), '--ruwana', self.ruwana]

    def capture(self, command):
        """Output of COMMAND; Refused with its output when it fails or does not finish."""
        try:
            done = subprocess.run(command, cwd=self.root, env=dict(self.environment), stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise Refused('the command did not finish in time') from None
        out = done.stdout[:MAX_OUTPUT].decode('utf-8', errors='replace')
        if done.returncode != 0:
            raise Refused(out + done.stderr[:8000].decode('utf-8', errors='replace'))
        return out

    def elicit(self, message):
        """Ask the user through the client; True only for an explicit accept.

        While the question is pending, every other request is refused without running, and
        answers for other ids are ignored; the end of the connection counts as no.
        """
        if 'elicitation' not in self.client:
            return False
        self.next_id += 1
        ident = f'elicit-{self.next_id}'
        self.send({'jsonrpc': '2.0', 'id': ident, 'method': 'elicitation/create',
                   'params': {'message': message, 'requestedSchema': {'type': 'object', 'properties': {}}}})
        while (answer := self.read()) is not EOF:
            if 'method' in answer:
                if answer.get('id') is not None:
                    self.send({'jsonrpc': '2.0', 'id': answer['id'], 'error': {
                        'code': -32000, 'message': 'a confirmation is pending; try again later'}})
                continue
            if answer.get('id') != ident:
                continue
            if answer.get('error') is not None or not isinstance(answer.get('result'), dict):
                return False
            return answer['result'].get('action') == 'accept'
        return False

    def draft(self, name, arguments):
        """The draft of one open lesson batch: read it, or replace it when the digest still matches.

        The write takes the batch lock first and reads everything it decides on inside it, so a
        user's edit or a close made while it waits is seen, never overwritten.
        """
        allowed = {'batch'} if name == 'lesson_draft_read' else {'batch', 'sha256', 'text'}
        if set(arguments) - allowed:
            raise Refused(f'{name}: unknown arguments: {", ".join(sorted(set(arguments) - allowed))}')
        batch = arguments.get('batch')
        if not isinstance(batch, str):
            raise Refused(f'{name}: batch must be a lesson batch id')
        handle = lesson.Batch(Wiki(self.root), self.state, concrete_id(batch, 'batch'))
        path = handle.file('draft.md')
        if name == 'lesson_draft_read':
            handle.load()
            return json.dumps(self.draft_record(batch, path), ensure_ascii=False, indent=2)
        text, digest = arguments.get('text'), arguments.get('sha256')
        if not isinstance(text, str) or not isinstance(digest, str):
            raise Refused(f'{name}: text and sha256 are required')
        data = text.encode('utf-8')
        if len(data) > DRAFT_LIMIT:
            raise Refused(f'{name}: the draft is too large (more than {DRAFT_LIMIT} bytes)')
        with handle.runtime.lock():
            meta = handle.load()
            handle.require_open(meta)
            handle.require_not_accepting()
            if os.path.islink(path):
                raise Refused('the draft is not a regular file')
            current = path.read_bytes() if path.is_file() else b''
            if sha256(current) != digest:
                raise Refused(f'{name}: the draft changed since it was read; read it again and merge')
            atomic_bytes(path, data)
        return json.dumps({'batch': batch, 'sha256': sha256(data)}, ensure_ascii=False, indent=2)

    @staticmethod
    def draft_record(batch, path):
        if os.path.islink(path):
            raise Refused('the draft is not a regular file')
        current = path.read_bytes() if path.is_file() else b''
        try:
            text = current.decode('utf-8')
        except UnicodeDecodeError:
            raise Refused('the draft is not UTF-8') from None
        return {'batch': batch, 'sha256': sha256(current), 'text': text}

    def review(self, args):
        if not isinstance(args, list) or len(args) != 2 or args[0] != '--batch' or not args[1]:
            raise Refused('lesson_review: arguments are ["--batch", "ID"]')
        result = codex_reviewer.review(self.root, self.state, args[1])
        return json.dumps(result, ensure_ascii=False, indent=2)

    def accept(self, args):
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise Refused('lesson_accept: args must be a list of strings')
        if any(a in GLOBAL or a.startswith(tuple(g + '=' for g in GLOBAL)) for a in args):
            raise Refused('lesson_accept: global options are not accepted; the server sets them')
        message = ('Accept this lesson and write it into the destination space?\n'
                   f'Batch and options: lesson accept {" ".join(args)}')
        if 'elicitation' not in self.client:
            raise Refused('lesson_accept: this client cannot ask for confirmation, so nothing was accepted')
        if not self.elicit(message):
            raise Refused('lesson_accept: not confirmed by the user; nothing was accepted')
        return self.capture([*self.pisar(), 'lesson', 'accept', *args])


EOF = object()  # the read loop's end of input; no JSON value is ever this object


class _Unknown(Exception):
    pass


def serve(root, state, ruwana, reader=None, writer=None, environment=None):
    """Serve on stdin/stdout until the client closes the connection."""
    env = environment if environment is not None else server_environment(root, state, ruwana)
    server = Server(root, state, ruwana, reader or sys.stdin, writer or sys.stdout, env)
    server.serve()
    return 0


def add_parser(commands):
    commands.add_parser('mcp-serve', help='Serve the MCP tools for the Codex agent on stdin and stdout '
                                          '(started by --agent codex, not by hand)')


def run(args):
    return serve(args.root, args.state_dir, args.ruwana)
