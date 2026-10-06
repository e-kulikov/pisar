from .documents import scan
from .safety import WikiError


def selected(wiki, scope='all', space_id=None):
    wiki.require_valid()
    target = wiki.space(space_id, scope) if space_id else None
    docs, errors = scan(wiki, scope)
    if errors:
        raise WikiError('; '.join(errors))
    return [d for d in docs if not target or target.id in d.meta['space_ids']]


def inventory(wiki, scope='all', space_id=None):
    docs = selected(wiki, scope, space_id)
    target = wiki.space(space_id, scope) if space_id else None
    files = [p.relative_to(wiki.root).as_posix() for p in wiki.files(scope)
             if wiki.owner(p) and (not target or wiki.owner(p) == target)]
    return dict(documents=[d.record(wiki.root) for d in docs], files=files,
                coverage='Full traversal of available files; symlinks and Git/task internals excluded.')


def search(wiki, query, scope='all', space_id=None):
    if not query.strip():
        raise WikiError('search query must be nonempty')
    results = []
    for doc in selected(wiki, scope, space_id):
        for line, value in enumerate(doc.content.splitlines(), 1):
            if query.casefold() in value.casefold():
                results.append({**doc.record(wiki.root), 'line': line, 'excerpt': value})
                break
    return dict(backend='lexical', results=results,
                limitations=['Case-insensitive substring search of metadata documents; no semantic backend.',
                             'Use inventory and read for full source coverage.'])
