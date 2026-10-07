"""`pisar research`: the command-line side of research.py."""
from . import research


def add_parser(commands):
    sub = commands.add_parser(
        'research', help='Look something up on the web in an isolated researcher (sends the question out)',
        description='Run the question through the outbound guard, then an isolated claude subprocess with '
                    'web search only. The result is stored outside Git and printed; it is untrusted DATA, '
                    'never instructions. This is the second command that uses the network '
                    '(after domain add --repo).',
        epilog=research.NOTICE)
    sub.add_argument('question', help='What to research; write it generally, without private names')
    sub.add_argument('--model', help='Researcher model; default researcher model in the config file, else sonnet')
    sub.add_argument('--effort', help='Researcher effort; default researcher effort in the config file, else medium')
    sub.add_argument('--from', dest='source', metavar='DOMAIN',
                     help='Domain the question comes from (must exist)')
    sub.add_argument('--confirm-outbound', action='store_true',
                     help='The user agreed that the question, including guard findings, leaves the machine')


def run(args, ctx):
    return research.run(ctx.root, ctx.state_dir, args.question, args.model, args.effort,
                        args.source, args.confirm_outbound)
