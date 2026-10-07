"""`pisar domain` and `pisar space`: create, find, move, archive and restore.

Every write holds the writer lock, refuses a dirty tree, journals its intent
outside Git, commits explicit paths (the owning repository first, then each
parent gitlink) and is safe to retry with the same inputs."""
import hashlib
import json
from pathlib import Path
import re
import tomllib
import unicodedata
from . import gitops
from .operations import Runtime, atomic_bytes, fingerprint
from .safety import WikiError, address, concrete_id, safe_path
from .spaces import LAYOUT, Wiki, load_domain

COMMANDS = ('domain', 'space')
KINDS = ('project', 'area', 'resource')
SCAFFOLD = ('project', 'area', 'resource', 'archive', 'inbox')
# A checkout holding only these still counts as blank when a domain is scaffolded.
BLANK = re.compile(r'(?i)(readme|license|licence|copying)(\..*)?|\.git|\.gitignore|\.gitattributes|\.gitkeep')


def add_parser(commands):
    domain = commands.add_parser('domain', help='List and add domains (top-level directories with .domain.toml)')
    actions = domain.add_subparsers(dest='action', required=True)
    actions.add_parser('list', help='Print every domain with its title, path and layout')
    add = actions.add_parser('add', help='Create a domain; --repo clones a Git repository as a submodule '
                                         '(the only network use besides research, done by git)')
    add.add_argument('--id', required=True, help='Domain id; also the directory name')
    add.add_argument('--title', required=True)
    add.add_argument('--repo', help='Git URL or local path to add as a submodule at the domain directory')
    add.add_argument('--layout', choices=('para', 'none'), default='para',
                     help='para (default) scaffolds the layout folders of a new or blank domain')
    space = commands.add_parser('space', help='Create, find, move, archive and restore spaces')
    actions = space.add_subparsers(dest='action', required=True)
    create = actions.add_parser('create', help='Create a space with .wiki.toml and a README')
    create.add_argument('--domain', required=True)
    create.add_argument('--kind', required=True, choices=KINDS)
    create.add_argument('--title', required=True)
    create.add_argument('--id', help='Space id; derived from the title when omitted')
    find = actions.add_parser('find', help='Find spaces by id, README title or alias')
    find.add_argument('text')
    for flag, verb in (('--include', 'Only these domains'), ('--exclude', 'All domains except these')):
        find.add_argument(flag, action='append', metavar='DOMAINS',
                          help=f'{verb}; comma separated, repeatable. --exclude applies after --include')
    move = actions.add_parser('move', help='Change the kind of a space and move it within its domain')
    move.add_argument('address', help='domain/id')
    move.add_argument('--to', required=True, choices=KINDS)
    for name, verb in (('archive', 'Mark a space archived and move it to the domain archive folder'),
                       ('restore', 'Mark an archived space active and move it back to its kind folder')):
        actions.add_parser(name, help=verb).add_argument('address', help='domain/id')


def run(args, ctx):
    """CTX needs wiki, domains (selected ids or None) and state_dir."""
    wiki, state = ctx.wiki, ctx.state_dir
    if args.command == 'domain':
        if args.action == 'list':
            return dict(domains=[d.record(wiki.root) for d in wiki.domains.values()])
        return domain_add(wiki, state, args.id, args.title, args.repo, args.layout)
    if args.action == 'create':
        return space_create(wiki, state, args.domain, args.kind, args.title, args.id)
    if args.action == 'find':
        return space_find(wiki, args.text, ctx.domains)
    if args.action == 'move':
        return relocate(wiki, state, 'move', args.address, args.to)
    return relocate(wiki, state, args.action, args.address)


# --- shared write machinery -------------------------------------------------

def _title(value):
    title = value.strip() if isinstance(value, str) else ''
    if not title or '\n' in title or '\r' in title:
        raise WikiError('title must be one nonempty line')
    return title


def _inside(path, prefixes):
    return any(path == p or path.startswith(p + '/') for p in prefixes)


def _ensure_clean(repo, prefixes=()):
    stray = sorted({p for _, p in gitops.dirty(repo) if not _inside(p, prefixes)})
    if stray:
        raise WikiError(f'dirty/staged repository {repo}: {", ".join(stray)}')


def _digest(*parts):
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:12]


def _blocking(runtime, relative, own):
    """Id of another incomplete journaled operation touching RELATIVE (root-relative), if any."""
    for path in sorted((runtime.path / 'operations').glob('*.json')):
        try:
            journal = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(journal, dict) or journal.get('status') != 'incomplete' \
                or journal.get('operation_id') == own:
            continue
        items = [*journal.get('artifacts', []), *(journal.get('tasks') or {}).values()]
        if any(isinstance(i, dict) and isinstance(i.get('path'), str) and
               (_inside(i['path'], (relative,)) or _inside(relative, (i['path'],))) for i in items):
            return str(journal.get('operation_id'))
    return None


def _locked(wiki, state_dir, op, intent, body):
    """Run BODY(runtime, resumed_journal) under the writer lock; resumed is None for a fresh run."""
    runtime = Runtime(state_dir, wiki.root)
    with runtime.lock():
        journal = runtime.load(op)
        if journal and journal['status'] == 'incomplete':
            if journal['fingerprint'] != fingerprint(intent):
                raise WikiError(f'incomplete operation {op} has different inputs; retry it unchanged first')
            return body(runtime, journal)
        return body(runtime, None)


def _flow(wiki, runtime, op, intent, repo, touched, apply, resumed, extra=None):
    """Check cleanliness, journal, APPLY(journal) the edits (returns [(repo, sha)]), then commit parent gitlinks."""
    root = wiki.root
    chain = gitops.ancestors(repo, root)
    own = [(root / t).relative_to(repo).as_posix() for t in touched if (root / t).is_relative_to(repo)]
    _ensure_clean(repo, own if resumed else ())
    for parent, link, _ in chain:
        _ensure_clean(parent, (link,) if resumed else ())
    journal = resumed or dict(schema_version=1, root=str(root), operation_id=op, fingerprint=fingerprint(intent),
                              artifacts=[dict(path=t) for t in touched], commits=[], tasks={}, heads={},
                              **(extra or {}))
    journal['status'] = 'incomplete'
    journal.pop('error', None)
    runtime.store(journal)
    try:
        done = list(apply(journal))
        for parent, link, _ in chain:
            done.append((parent, gitops.commit(parent, [link], op)))
    except (WikiError, OSError) as error:
        journal['error'] = str(error)
        runtime.store(journal)
        raise
    journal['commits'] = [dict(repo=Path(r).relative_to(root).as_posix(), commit=sha) for r, sha in done if sha]
    journal['status'] = 'complete'
    runtime.store(journal)
    return journal['commits']


# --- domains ----------------------------------------------------------------

def _location(repo):
    """(location, is_local) for --repo; options and git transport helpers are refused."""
    value = repo.strip()
    if not value or value.startswith('-') or '::' in value or any(c in value for c in '\0\n'):
        raise WikiError(f'unsupported --repo: {repo!r}')
    local = Path(value).expanduser()
    if '://' not in value and local.is_dir():
        return str(local.resolve()), True
    return value, value.startswith('file://')


def _registered(wiki, ident):
    return gitops.run(wiki.root, 'ls-files', '--stage', '--', ident).startswith('160000 ')


def _check_origin(wiki, ident, location):
    url = gitops.run(wiki.root, 'config', '-f', '.gitmodules', '--get', f'submodule.{ident}.url',
                     check=False).strip()
    if not url or _location(url)[0] != location[0]:
        raise WikiError(f'domain {ident} is not the submodule of {location[0]}')


def _marker(ident, title):
    return f'schema_version = 1\nid = "{ident}"\ntitle = {json.dumps(title, ensure_ascii=False)}\n'


def _blank(path):
    return not path.exists() or all(BLANK.fullmatch(p.name) for p in path.iterdir())


def domain_add(wiki, state_dir, ident, title, repo, layout):
    ident = concrete_id(ident)
    title = _title(title)
    location = _location(repo) if repo is not None else None
    base = safe_path(wiki.root, ident)
    root = wiki.root
    op = f'domain-add-{ident}'
    intent = dict(id=ident, title=title, repo=location and location[0], layout=layout)

    def finish(changed, commits=()):
        record = Wiki(root).domains[ident].record(root)
        return dict(domain=record, changed=changed, commits=list(commits))

    def body(runtime, resumed):
        if not resumed and ident in wiki.marked:
            if ident not in wiki.domains:
                raise WikiError('; '.join(wiki.errors_in({ident})))
            existing = wiki.domains[ident]
            if existing.title != title:
                raise WikiError(f'domain {ident} already exists with title {existing.title!r}')
            if location:
                if not _registered(wiki, ident):
                    raise WikiError(f'domain {ident} exists and is not a submodule')
                _check_origin(wiki, ident, location)
            return finish(False)
        if base.exists() and not base.is_dir():
            raise WikiError(f'{ident} exists and is not a directory')
        if not resumed and location and not _registered(wiki, ident) and base.exists() and any(base.iterdir()):
            raise WikiError(f'{ident} exists and is not empty; cannot add a submodule there')
        if location and _registered(wiki, ident):
            _check_origin(wiki, ident, location)
        flow_repo = root if location else gitops.owner(base, root)
        touched = [ident, '.gitmodules'] if location else [ident]

        def apply(journal):
            if location and not _registered(wiki, ident):
                options = ['-c', 'protocol.file.allow=always'] if location[1] else []
                gitops.run(root, *options, 'submodule', 'add', '-q', '--', location[0], ident)
            target = gitops.owner(base, root)
            marker = base / '.domain.toml'
            # Decided once, after any clone and before the marker exists; a retry reuses it.
            if 'scaffold' not in journal:
                journal['scaffold'] = layout == 'para' and not marker.exists() and _blank(base)
                runtime.store(journal)
            paths = []
            if marker.exists():
                known = load_domain(base)
                if known.title != title:
                    raise WikiError(f'domain {ident} marker has title {known.title!r}')
            else:
                atomic_bytes(marker, _marker(ident, title).encode())
                paths.append('.domain.toml')
            if journal['scaffold']:
                for kind in SCAFFOLD:
                    folder = base / LAYOUT[kind]
                    if not folder.exists() or not any(folder.iterdir()):
                        atomic_bytes(folder / '.gitkeep', b'')
                    if (folder / '.gitkeep').is_file():
                        paths.append(f'{LAYOUT[kind]}/.gitkeep')
            inner = [(base / p).relative_to(target).as_posix() for p in paths]
            done = [(target, gitops.commit(target, inner, op))]
            if location:
                done.append((root, gitops.commit(root, ['.gitmodules', ident], op)))
            return done

        return finish(True, _flow(wiki, runtime, op, intent, flow_repo, touched, apply, resumed))

    return _locked(wiki, state_dir, op, intent, body)


# --- spaces -----------------------------------------------------------------

def derive_id(title):
    text = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', text).strip('-')


def _domain(wiki, ident):
    if ident not in wiki.marked:
        raise WikiError(f'unknown domain: {ident}')
    wiki.require_valid(frozenset({ident}))
    return wiki.domains[ident]


def _readme_title(path):
    readme = path / 'README.md'
    try:
        if readme.is_symlink() or not readme.is_file():
            return ''
        for line in readme.read_text(encoding='utf-8').splitlines():
            if match := re.match(r'#\s+(.+?)\s*$', line):
                return match[1]
    except (OSError, ValueError):
        pass
    return ''


def _aliases(path):
    try:
        value = tomllib.loads((path / '.wiki.toml').read_text(encoding='utf-8')).get('aliases', [])
    except (OSError, ValueError):
        return []
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _alternatives(wiki, domain, ident):
    taken = {s.id for s in wiki.spaces if s.domain == domain.id}
    free = (f'{ident}-{n}' for n in range(2, 100) if f'{ident}-{n}' not in taken)
    return [next(free) for _ in range(3)]


def _record(root, value):
    return Wiki(root).space(value, frozenset({value.split('/')[0]})).record(root)


def space_create(wiki, state_dir, domain_id, kind, title, ident):
    domain = _domain(wiki, domain_id)
    title = _title(title)
    if ident is None:
        ident = derive_id(title)
        if not ident:
            raise WikiError(f'the title {title!r} yields no usable id; pass --id (lowercase ASCII kebab)')
    concrete_id(ident)
    if len(ident.split('-')) < domain.min_segments:
        raise WikiError(f'id {ident!r} has fewer than {domain.min_segments} dash-separated segments '
                        f'required by domain {domain.id}; pass a more specific --id')
    root = wiki.root
    relative = f'{domain.id}/{domain.layout[kind]}/{ident}'
    path = safe_path(root, relative)
    addr = f'{domain.id}/{ident}'
    op = f'space-create-{domain.id}-{ident}-{_digest(domain.id, ident)}'
    intent = dict(domain=domain.id, id=ident, kind=kind, title=title)

    def body(runtime, resumed):
        if not resumed:
            for found in (s for s in wiki.spaces if s.address == addr):
                if found.path == path and found.kind == kind and found.status == 'active' \
                        and _readme_title(path) == title:
                    return dict(space=found.record(root), changed=False, commits=[])
                raise WikiError(f'space id {ident!r} is already used in {domain.id} by '
                                f'{found.id}: {found.kind}, {found.status} ({found.path.relative_to(root)}); '
                                f'ids are permanent, archived spaces included; try: '
                                + ', '.join(_alternatives(wiki, domain, ident)))
            if path.exists() or path.is_symlink():
                raise WikiError(f'path already exists: {relative}')
        repo = gitops.owner(path, root)
        files = {'.wiki.toml': f'schema_version = 1\nid = "{ident}"\nkind = "{kind}"\nstatus = "active"\n',
                 'README.md': f'# {title}\n'}

        def apply(_):
            for name, text in files.items():
                target = path / name
                if not target.exists() or target.read_text(encoding='utf-8') != text:
                    atomic_bytes(target, text.encode())
            return [(repo, gitops.commit(repo, [(path / n).relative_to(repo).as_posix() for n in files], op))]

        done = _flow(wiki, runtime, op, intent, repo, [relative], apply, resumed)
        return dict(space=_record(root, addr), changed=True, commits=done)

    return _locked(wiki, state_dir, op, intent, body)


def space_find(wiki, text, domains=None):
    needle = text.casefold().strip()
    if not needle:
        raise WikiError('search text must be nonempty')
    wiki.require_valid(domains)
    found = []
    for s in sorted(wiki.spaces, key=lambda s: s.address):
        if domains is not None and s.domain not in domains:
            continue
        title, aliases = _readme_title(s.path), _aliases(s.path)
        matched = [name for name, value in (('id', s.id), ('title', title), *(('alias', a) for a in aliases))
                   if needle in value.casefold()]
        if matched:
            found.append(dict(**s.record(wiki.root), title=title, aliases=aliases, matched=sorted(set(matched))))
    return dict(spaces=found)


def _rewrite(text, kind, status):
    """TEXT with the top-level kind/status values replaced; validated by parsing the result."""
    for key, value in (('kind', kind), ('status', status)):
        text = re.sub(rf'(?m)^([ \t]*{key}[ \t]*=[ \t]*)("[^"\n]*"|\'[^\'\n]*\')',
                      lambda m, v=value: f'{m[1]}"{v}"', text, count=1)
    try:
        meta = tomllib.loads(text)
    except ValueError:
        meta = {}
    if (meta.get('kind'), meta.get('status')) != (kind, status):
        raise WikiError('cannot update kind/status in .wiki.toml; edit it by hand')
    return text


def _edit_marker(path, kind, status):
    old = path.read_text(encoding='utf-8')
    text = _rewrite(old, kind, status)
    if text != old:
        atomic_bytes(path, text.encode())


def relocate(wiki, state_dir, action, value, to=None):
    """move (kind), archive or restore: git mv the directory and rewrite .wiki.toml in one commit."""
    domain_id = address(value).split('/')[0]
    domain = _domain(wiki, domain_id)
    root = wiki.root
    intent = dict(action=action, address=value, to=to)
    op = f'space-{action}-{domain_id}-{value.split("/")[1]}-{_digest(value, to)}'

    def body(runtime, resumed):
        space = wiki.space(value, frozenset({domain_id}))
        if resumed:
            old, new = root / resumed['old'], root / resumed['new']
            kind, status = resumed['space_kind'], resumed['space_status']
        else:
            if (space.path / '.git').exists():
                raise WikiError(f'{value} is itself a Git submodule; move it by hand')
            nested = [line.split('\t')[-1] for line in gitops.run(
                gitops.owner(space.path, root), 'ls-files', '--stage', '--',
                space.path.relative_to(gitops.owner(space.path, root)).as_posix()).splitlines()
                if line.startswith('160000 ')]
            if nested:
                raise WikiError(f'{value} contains Git submodules ({", ".join(nested)}); a move would '
                                'have to rewrite .gitmodules, so move it by hand')
            kind, status = space.kind, space.status
            if action == 'move':
                if status == 'archived':
                    raise WikiError(f'{value} is archived; restore it before changing its kind')
                if kind == to:
                    return dict(space=space.record(root), changed=False, commits=[],
                                message=f'{value} is already a {kind}; nothing to do')
                kind, folder = to, domain.layout[to]
            elif action == 'archive':
                if status == 'archived':
                    return dict(space=space.record(root), changed=False, commits=[],
                                message=f'{value} is already archived; nothing to do')
                status, folder = 'archived', domain.layout['archive']
            else:
                if status == 'active':
                    return dict(space=space.record(root), changed=False, commits=[],
                                message=f'{value} is already active; nothing to do')
                status, folder = 'active', domain.layout[kind]
            old = space.path
            _rewrite((old / '.wiki.toml').read_text(encoding='utf-8'), kind, status)
            new = safe_path(root, f'{domain_id}/{folder}') / old.name
            if new != old and (new.exists() or new.is_symlink()):
                raise WikiError(f'target exists: {new.relative_to(root)}')
            if blocker := _blocking(runtime, old.relative_to(root).as_posix(), op):
                raise WikiError(f'incomplete operation {blocker} touches {value}; finish or roll it back first')
        repo = gitops.owner(old if old.exists() else new, root)
        if gitops.owner(new, root) != repo:
            raise WikiError('the move would cross Git repositories')
        rel = lambda p: p.relative_to(repo).as_posix()  # noqa: E731

        def apply(_):
            if new != old and old.exists() and not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                gitops.run(repo, 'mv', '--', rel(old), rel(new))
            _edit_marker(new / '.wiki.toml', kind, status)
            if new == old:
                return [(repo, gitops.commit(repo, [rel(new / '.wiki.toml')], op))]
            return [(repo, gitops.commit_move(repo, rel(old), rel(new), op))]

        touched = sorted({old.relative_to(root).as_posix(), new.relative_to(root).as_posix()})
        extra = dict(old=old.relative_to(root).as_posix(), new=new.relative_to(root).as_posix(),
                     space_kind=kind, space_status=status)
        done = _flow(wiki, runtime, op, intent, repo, touched, apply, resumed, extra=extra)
        return dict(space=_record(root, value), changed=True, commits=done)

    return _locked(wiki, state_dir, op, intent, body)
