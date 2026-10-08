"""`pisar config show`: the effective settings and where each one comes from."""
from . import settings


def add_parser(commands):
    config = commands.add_parser('config', help='Inspect the effective configuration')
    actions = config.add_subparsers(dest='action', required=True)
    actions.add_parser('show', help='Print each effective setting and its source '
                                    '(flag, env, config or default)')


def run(args, ctx):
    """CTX.flags holds the raw global options, before any default was applied."""
    return settings.show(ctx.flags)
