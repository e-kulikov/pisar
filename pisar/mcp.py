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
from pathlib import Path
import subprocess
import sys
from . import __version__
from .codex_agent import pisar_command, server_environment
from .safety import WikiError

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
# Git: read-only inspection, staging, commit and renaming of tracked files. Options that run
# programs, read outside the repository or change its configuration are refused.
GIT = {'status': (), 'diff': ('--no-ext-diff', '--no-textconv'), 'log': ('--no-ext-diff', '--no-textconv'),
       'add': (), 'commit': (), 'mv': ()}
GIT_REFUSED = ('-c', '-C', '--config', '--git-dir', '--work-tree', '--exec', '--upload-pack',
               '--receive-pack', '--output', '--no-index', '--namespace', '--super-prefix', '--ext-diff',
               '--textconv', '--exec-path', '--attr-source', '--shallow-file', '--literal-pathspecs-only')
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
)


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


def check_git(args):
    """The argument list for `git ARGS` (the subcommand first), or Refused."""
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args) or not args:
        raise Refused('git: args must be a non-empty list of strings')
    subcommand, rest = args[0], args[1:]
    if subcommand not in GIT:
        raise Refused(f'git: not an allowed subcommand: {subcommand}')
    for arg in rest:
        if any(arg.startswith(prefix) for prefix in GIT_REFUSED):
            raise Refused(f'git: option or argument not allowed: {arg.split("=", 1)[0]}')
    return [subcommand, *GIT[subcommand], *rest]


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
        while True:
            line = self.reader.readline()
            if not line:
                return None
            if line.strip():
                try:
                    return json.loads(line)
                except ValueError:
                    self.send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}})

    def serve(self):
        while (message := self.read()) is not None:
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
        except Refused as error:
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
        """Ask the user through the client; True only for an explicit accept."""
        if 'elicitation' not in self.client:
            return False
        self.next_id += 1
        ident = f'elicit-{self.next_id}'
        self.send({'jsonrpc': '2.0', 'id': ident, 'method': 'elicitation/create',
                   'params': {'message': message, 'requestedSchema': {'type': 'object', 'properties': {}}}})
        while (answer := self.read()) is not None:
            if answer.get('id') == ident and 'method' not in answer:
                result = answer.get('result') or {}
                return answer.get('error') is None and result.get('action') == 'accept'
            if 'method' in answer:
                self.dispatch(answer)
        return False

    def accept(self, args):
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise Refused('lesson_accept: args must be a list of strings')
        if any(a in GLOBAL or a.startswith(tuple(g + '=' for g in GLOBAL)) for a in args):
            raise Refused('lesson_accept: global options are not accepted; the server sets them')
        message = ('Accept this lesson and write it into the destination space?\n'
                   f'Batch and options: lesson accept {" ".join(args)}')
        if not self.elicit(message):
            raise Refused('lesson_accept: not confirmed by the user; nothing was accepted')
        return self.capture([*self.pisar(), 'lesson', 'accept', *args])


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
