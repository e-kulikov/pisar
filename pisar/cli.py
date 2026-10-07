import argparse
import json
import sys
from types import SimpleNamespace
from . import __version__, agent, config_command, guard, settings, spacecmd
from .documents import check, resolve
from .safety import WikiError, sha256
from .spaces import Wiki
from .search import inventory, search


class _Skill(argparse.Action):
    """Print the bundled agent guide and exit, like --version: needs no root."""

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS,
                         help='Print the agent skill document describing how to use pisar, then exit')

    def __call__(self, parser, namespace, values, option_string=None):
        from importlib.resources import files
        sys.stdout.write(files('pisar').joinpath('skill.md').read_text(encoding='utf-8'))
        parser.exit()


def parser():
    p = argparse.ArgumentParser(prog='pisar',
                                description='Offline knowledge repository CLI; lexical search, explicit Git writes.')
    p.add_argument('--version', action='version', version=f'pisar {__version__}')
    p.add_argument('--skill', action=_Skill)
    p.add_argument('--agent', nargs='?', const=agent.DEFAULT, metavar='{%s}' % ','.join(agent.AGENTS),
                   help='Start this AI agent confined to the knowledge root (without a value: '
                        'default_agent from the config file); arguments after -- go to the agent itself')
    p.add_argument('--root', help='Knowledge root; default $PISAR_ROOT, else root in the config file, '
                                   'else $XDG_DATA_HOME/wiki')
    p.add_argument('--state-dir', help='External runtime directory outside all Git repos and the root; '
                                       'default $PISAR_STATE_DIR, else state_dir in the config file, '
                                       'else $XDG_DATA_HOME/pisar')
    p.add_argument('--ruwana', help='Public ruwana binary; default $PISAR_RUWANA_BIN, else ruwana in the config file, '
                                    'else ruwana on PATH')
    commands = p.add_subparsers(dest='command')
    for name in ('spaces', 'search', 'read', 'check', 'inventory'):
        sub = commands.add_parser(name)
        for flag, verb in (('--include', 'Only these domains'), ('--exclude', 'All domains except these')):
            sub.add_argument(flag, action='append', metavar='DOMAINS',
                             help=f'{verb}; comma separated, repeatable. --exclude applies after --include')
        if name in ('search', 'inventory'):
            sub.add_argument('--space', help='Owner or related space address (domain/id)')
        if name == 'search':
            sub.add_argument('query')
        if name == 'read':
            sub.add_argument('reference', help='wiki:domain/space:document or root-relative path')
    capture = commands.add_parser('capture', help='Commit an unchanged source to its known space')
    capture.add_argument('--space', required=True, help='Destination space address (domain/id)')
    capture.add_argument('--id', required=True)
    capture.add_argument('--source', required=True)
    capture.add_argument('--sha256', help='Expected original hash')
    save = commands.add_parser('save', help='Apply an external agent meeting JSON plan')
    save.add_argument('--plan', required=True)
    triage = commands.add_parser('triage', help='External global inbox routing')
    actions = triage.add_subparsers(dest='action', required=True)
    for name in ('prepare', 'report', 'accept', 'reroute', 'defer'):
        sub = actions.add_parser(name)
        sub.add_argument('--batch', required=True)
        if name == 'prepare':
            source = sub.add_mutually_exclusive_group()
            source.add_argument('--plan', help='External routing JSON')
            source.add_argument('--inbox', help='External inbox directory, default runtime/inbox')
        if name in ('accept', 'reroute', 'defer'):
            sub.add_argument('--item', required=True)
        if name == 'reroute':
            sub.add_argument('--space', required=True, help='New destination space address (domain/id)')
    config_command.add_parser(commands)
    guard.add_parser(commands)
    spacecmd.add_parser(commands)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if '--' in argv and any(a == '--agent' or a.startswith('--agent=') for a in argv[:argv.index('--')]):
        split = argv.index('--')
        argv, extra = argv[:split], argv[split + 1:]
    p = parser()
    args = p.parse_args(argv)
    if args.agent not in (None, agent.DEFAULT, *agent.AGENTS):
        p.error(f'argument --agent: invalid choice: {args.agent!r}')
    if args.agent and args.command:
        p.error('--agent cannot be combined with a command')
    if not args.agent and not args.command:
        p.error('a command is required (or --agent)')
    try:
        if args.agent:
            agent.launch(agent.selected(args.agent), settings.root(args.root),
                         settings.state_dir(args.state_dir), settings.ruwana(args.ruwana), extra)
        if args.command == 'config':
            ctx = SimpleNamespace(flags=dict(root=args.root, state_dir=args.state_dir, ruwana=args.ruwana))
            print(json.dumps(config_command.run(args, ctx), ensure_ascii=False, indent=2))
            return 0
        args.root = settings.root(args.root)
        args.state_dir = settings.state_dir(args.state_dir)
        args.ruwana = settings.ruwana(args.ruwana)
        if args.command == 'guard':
            result = guard.run(args, args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        wiki = Wiki(args.root)
        domains = None
        if getattr(args, 'include', None) or getattr(args, 'exclude', None):
            domains = wiki.select(args.include, args.exclude)
        if args.command == 'check':
            result = check(wiki, domains)
        elif args.command in spacecmd.COMMANDS:
            result = spacecmd.run(args, SimpleNamespace(wiki=wiki, domains=domains, state_dir=args.state_dir))
        else:
            wiki.require_valid(domains)
            if args.command == 'spaces':
                result = {'spaces': [s.record(wiki.root) for s in wiki.spaces
                                     if domains is None or s.domain in domains]}
            elif args.command == 'search':
                result = search(wiki, args.query, domains, args.space)
            elif args.command == 'inventory':
                result = inventory(wiki, domains, args.space)
            elif args.command == 'read':
                if args.reference.startswith('wiki:'):
                    doc = resolve(wiki, args.reference, domains)
                    result = {**doc.record(wiki.root), 'content': doc.path.read_text(encoding='utf-8')}
                else:
                    path = wiki.path(args.reference, domains)
                    result = dict(path=path.relative_to(wiki.root).as_posix(),
                                  content=path.read_text(encoding='utf-8'), sha256=sha256(path.read_bytes()))
            elif args.command == 'capture':
                from .operations import capture
                result = capture(wiki, args.state_dir, args.space, args.id, args.source, args.sha256)
            elif args.command == 'save':
                from .operations import save
                result = save(wiki, args.state_dir, args.plan, args.ruwana)
            elif args.command == 'triage':
                from . import triage
                if args.action == 'prepare':
                    result = triage.prepare(wiki, args.state_dir, args.batch, args.plan, args.inbox)
                elif args.action == 'report':
                    result = triage.report(wiki, args.state_dir, args.batch)
                elif args.action == 'accept':
                    result = triage.accept(wiki, args.state_dir, args.batch, args.item, args.ruwana)
                elif args.action == 'reroute':
                    result = triage.reroute(wiki, args.state_dir, args.batch, args.item, args.space)
                else:
                    result = triage.defer(wiki, args.state_dir, args.batch, args.item)
            else:
                raise WikiError('write operation not implemented yet')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('ok') is False else 0
    except (WikiError, OSError, ValueError, RuntimeError) as error:
        print(f'pisar: {error}', file=sys.stderr)
        return 1
