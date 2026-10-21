"""Claude PreToolUse reservation guard. Tool input is data, never executed here."""
import json
import shlex
import sys
from .operations import Runtime


def run(root, state):
    try:
        request = json.load(sys.stdin)
        runtime = Runtime(state, root)
        pending = runtime.pending_sync()
        if not pending:
            return 0
        name = request.get('tool_name')
        if name == 'Bash':
            command = request.get('tool_input', {}).get('command', '')
            # Only simple bounded commands can bypass a reservation. Never
            # interpret shell composition as a read or sync continuation.
            if not any(char in command for char in ';|&<>`$()\n\r'):
                args = shlex.split(command)
                if args[:1] == ['git'] and args[1:2] in (['status'], ['diff'], ['log']):
                    from .mcp import check_git
                    check_git(args[1:], root)
                    return 0
                if args[:1] == ['pisar']:
                    from .mcp import check_pisar
                    if args[1:2] != ['sync']:
                        check_pisar(args[1:])
                    if args[1:2] in (['spaces'], ['read'], ['search'], ['inventory'], ['check']):
                        return 0
                    if args[1:2] == ['sync']:
                        from .cli import parser
                        parser().parse_args(args[1:])
                        if args[2:3] in (['status'], ['feedback-summary']):
                            return 0
                        if '--operation' in args:
                            ident = args[args.index('--operation') + 1]
                            runtime.require_unreserved(ident)
                            return 0
        runtime.require_unreserved()
    except Exception as error:
        print(f'sync reservation guard failed: {error}', file=sys.stderr)
        return 2
    return 0
