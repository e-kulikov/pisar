"""CLI surface for delivery-I sync operations."""
from . import sync
from .sync import Target


def add_parser(commands):
    root = commands.add_parser('sync', help='Prepare, review, apply and explicitly publish Git sync candidates')
    actions = root.add_subparsers(dest='action', required=True)
    fetch = actions.add_parser('fetch', help='Fetch a named remote branch and freeze its exact tip')
    fetch.add_argument('--repo', required=True, help='Repository path relative to the wiki root (delivery I: .)')
    fetch.add_argument('--remote', default='origin')
    fetch.add_argument('--branch', default='main')
    pull = actions.add_parser('pull', help='Fetch and prepare a candidate; manual acceptance is always required')
    pull.add_argument('--repo', required=True)
    pull.add_argument('--remote', default='origin')
    pull.add_argument('--branch', default='main')
    pull.add_argument('--merge', action='store_true')
    pull.add_argument('--dry-run', action='store_true')
    prepare = actions.add_parser('prepare', help='Prepare a disposable Git candidate')
    prepare.add_argument('--operation', required=True)
    prepare.add_argument('--merge', action='store_true', help='Allow a native merge for divergent histories')
    review = actions.add_parser('review', help='Record explicit human review of a prepared candidate')
    review.add_argument('--operation', required=True)
    review.add_argument('--manual', action='store_true', required=True,
                        help='Record a human review alternative (delivery I has no AI reviewer)')
    status = actions.add_parser('status', help='Read cached sync operation facts without fetching')
    status.add_argument('--operation')
    close = actions.add_parser('close', help='Release an unfinished operation reservation and retain its candidate')
    close.add_argument('--operation', required=True)
    accept = actions.add_parser('accept', help='Record owner acceptance without moving Git state')
    accept.add_argument('--operation', required=True)
    accept.add_argument('--revision', required=True)
    apply = actions.add_parser('apply', help='Apply exactly an accepted candidate')
    apply.add_argument('--operation', required=True)
    publish = actions.add_parser('publish', help='Explicitly publish an applied operation')
    selector = publish.add_mutually_exclusive_group(required=True)
    selector.add_argument('--operation')
    selector.add_argument('--repo', help='Standalone publish-only review is added in a later delivery')
    publish.add_argument('--dry-run', action='store_true')
    publish.add_argument('--remote', default='origin')
    publish.add_argument('--branch', default='main')


def run(args, wiki, state_dir):
    if args.action == 'fetch':
        return sync.fetch(wiki, state_dir, args.repo, Target(args.remote, args.branch))
    if args.action == 'pull':
        return sync.pull(wiki, state_dir, args.repo, Target(args.remote, args.branch),
                         merge=args.merge, dry_run=args.dry_run)
    if args.action == 'prepare':
        return sync.prepare(wiki, state_dir, args.operation, merge=args.merge)
    if args.action == 'review':
        return sync.manual_review(wiki, state_dir, args.operation)
    if args.action == 'status':
        return sync.inspect(wiki, state_dir, args.operation)
    if args.action == 'close':
        return sync.close(wiki, state_dir, args.operation)
    if args.action == 'accept':
        return sync.accept(wiki, state_dir, args.operation, args.revision)
    if args.action == 'apply':
        return sync.apply(wiki, state_dir, args.operation)
    if args.action == 'publish':
        if args.repo is not None:
            target = Target(args.remote, args.branch)
            if args.dry_run:
                return sync.pull(wiki, state_dir, args.repo, target, dry_run=True)
            return sync.publish_only(wiki, state_dir, args.repo, target)
        return sync.publish(wiki, state_dir, args.operation, dry_run=args.dry_run)
    raise ValueError('unknown sync action')
