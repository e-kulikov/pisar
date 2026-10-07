"""`pisar lesson`: wiring of the lesson workspace operations to the command line."""
from . import lesson


def add_parser(commands):
    group = commands.add_parser('lesson', help='Carry a reviewed lesson into a space (guard + review + explicit accept)')
    actions = group.add_subparsers(dest='action', required=True)
    start = actions.add_parser('start', help='Create a lesson batch with an empty draft')
    start.add_argument('--from', dest='source', required=True, metavar='DOMAIN_OR_ADDRESS',
                       help='Where the lesson comes from; its domain terms are checked')
    start.add_argument('--to', required=True, metavar='ADDRESS', help='Destination space address (domain/id)')
    start.add_argument('--title', required=True)
    for name, text in (('check', 'Snapshot the draft as the next revision and report guard findings'),
                       ('review', 'Attach a reviewer verdict (JSON) to the current revision'),
                       ('show', 'Print a human-readable report of the batch'),
                       ('accept', 'Write the reviewed revision as a note in the destination space'),
                       ('discard', 'Remove the batch workspace, keeping a journal entry')):
        sub = actions.add_parser(name, help=text)
        sub.add_argument('--batch', required=True)
        if name == 'review':
            sub.add_argument('--file', required=True, help='Reviewer JSON for the current revision')
        if name == 'accept':
            sub.add_argument('--keep', action='append', default=[], metavar='F1,F2',
                             help='Finding ids to keep as they are (the user\'s explicit decision); repeatable')
            sub.add_argument('--mention-origin', action='store_true',
                             help='Add a plain-text line naming the origin domain title')
            sub.add_argument('--skip-review', action='store_true',
                             help='Accept without a review of the current revision (recorded)')


def run(args, ctx):
    """CTX.wiki is the loaded wiki; returns a dict, or text for `show`."""
    wiki, state = ctx.wiki, args.state_dir
    if args.action == 'start':
        return lesson.start(wiki, state, args.source, args.to, args.title)
    if args.action == 'check':
        return lesson.check(wiki, state, args.batch)
    if args.action == 'review':
        return lesson.review(wiki, state, args.batch, args.file)
    if args.action == 'show':
        return lesson.show(wiki, state, args.batch)
    if args.action == 'accept':
        return lesson.accept(wiki, state, args.batch, args.keep, args.mention_origin, args.skip_review)
    return lesson.discard(wiki, state, args.batch)
