"""`pisar sync`: wiring of the sync operations to the command line (pull-only; pisar never pushes)."""
from . import sync


def add_parser(commands):
    group = commands.add_parser('sync', help='Pull wiki changes from a Git remote (explicit; pisar never pushes)')
    actions = group.add_subparsers(dest='action', required=True)
    for name, text in (('fetch', 'Fetch the remote branch into a private ref and report what would change'),
                       ('status', 'Report the fetched change and any open sync (offline)')):
        _target(actions.add_parser(name, help=text))
    pull = actions.add_parser('pull', help='Fast-forward the local branch to the fetched commit (--merge: merge it)')
    _target(pull)
    pull.add_argument('--merge', action='store_true', help='Merge a diverged history into a merge commit')
    pull.add_argument('--dry-run', action='store_true', help='Compute and validate the change; write nothing')
    resolve = actions.add_parser('resolve', help='Finish a conflicted merge from its work tree')
    resolve.add_argument('--id', required=True, dest='sync_id', help='Sync id from the conflict report')
    take = actions.add_parser('take', help='Resolve one conflicted path by taking one side whole')
    take.add_argument('--id', required=True, dest='sync_id')
    take.add_argument('--path', required=True)
    take.add_argument('--side', required=True, choices=('ours', 'theirs'))
    abort = actions.add_parser('abort', help='Drop a sync that has not moved the branch; the repository is untouched')
    abort.add_argument('--id', required=True, dest='sync_id')


def _target(sub):
    sub.add_argument('--repo', metavar='REL', help='Repository to sync: the root (default) or a registered '
                                                   'submodule, root-relative')
    sub.add_argument('--remote', help='Configured remote name (default: [sync] remote, else origin)')
    sub.add_argument('--branch', help='Remote branch to fetch (default: the local branch name)')


def run(args, ctx):
    """CTX needs root and state_dir; these commands do not need the loaded wiki."""
    root, state = ctx.root, ctx.state_dir
    if args.action == 'fetch':
        return sync.do_fetch(root, state, args.repo, args.remote, args.branch)
    if args.action == 'status':
        return sync.do_status(root, state, args.repo, args.remote, args.branch)
    if args.action == 'pull':
        return sync.do_pull(root, state, args.repo, args.remote, args.branch, merge=args.merge,
                            dry_run=args.dry_run)
    if args.action == 'resolve':
        return sync.do_resolve(root, state, args.sync_id)
    if args.action == 'take':
        return sync.do_take(root, state, args.sync_id, args.path, args.side)
    return sync.do_abort(root, state, args.sync_id)
