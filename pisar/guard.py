"""Best-effort lexical guard: report possibly sensitive text, never change it.

Used by lesson operations and before outbound research queries. Ordinary
capture/save/writes never call it. Domain terms come straight from the
`.domain.toml` markers, the `.wiki.toml` ids and README H1 titles below them.
"""
from dataclasses import dataclass
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import tomllib
from .safety import WikiError


LIMITS = ('Best effort: a deterministic lexical scan, not a guarantee. It reports only what its '
          'patterns and the listed domain terms match, so it misses paraphrases, misspellings, '
          'inflected or unlisted names, images and encoded content, and it may flag harmless text. '
          'Amounts and metrics are not reported. It never edits or redacts text; decide on each '
          'finding yourself.')

MIN_TERM = 3
MIN_QUOTE = 30
MIN_QUOTE_WORDS = 4
EXCERPT = 80
SKIP = {'.git', '.ruwana', '__pycache__'}
DOMAIN_ID = re.compile(r'[a-z0-9][a-z0-9-]*')

# Overlapping matches of these categories are reported once, by the first in this order.
_EXCLUSIVE = ('private-key', 'jwt', 'api-token', 'credential', 'url', 'email', 'ip', 'phone',
              'meeting', 'date', 'hostname')
_TIER = {'private-key': 'severe', 'jwt': 'severe', 'api-token': 'severe', 'credential': 'severe',
         'email': 'severe', 'phone': 'severe', 'code-block': 'severe',
         'quote': 'ask', 'meeting': 'ask', 'url': 'ask', 'hostname': 'ask', 'ip': 'ask',
         'term': 'warn', 'date': 'warn'}
_HINT = {
    'private-key': 'private key block: remove it, keys never belong in notes',
    'jwt': 'JSON web token: remove it',
    'api-token': 'cloud or API token pattern: remove it',
    'credential': 'credential assignment: remove the value',
    'email': 'email address (possible client or personal data)',
    'phone': 'phone number (possible client or personal data)',
    'code-block': 'fenced code block: possible proprietary code or architecture',
    'quote': 'verbatim quote: paraphrase unless it may be kept',
    'meeting': 'possible meeting content (timestamp or speaker line)',
    'url': 'URL: may point to internal systems',
    'hostname': 'hostname: may name internal systems',
    'ip': 'IP address: may identify internal systems',
    'date': 'date or time with day or finer precision',
}

_BOUND = r'(?<![A-Za-z0-9_-])(?:{})(?![A-Za-z0-9_-])'
_OCTET = r'(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)'
_HEX = r'[0-9A-Fa-f]{1,4}'
_MONTH = (r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?'
          r'|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?')
_MONTH_RU = r'(?i:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)'
_ORDINAL = r'(?:st|nd|rd|th)?'
_TIME = r'\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?'

_PATTERNS = (
    ('private-key', re.compile(
        r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----'
        r'(?:.*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|.*\Z)', re.S)),
    ('jwt', re.compile(_BOUND.format(r'eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*'))),
    ('api-token', re.compile(_BOUND.format('|'.join((
        r'(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}',
        r'gh[pousr]_[A-Za-z0-9]{36,255}', r'github_pat_[A-Za-z0-9_]{22,255}',
        r'glpat-[A-Za-z0-9_-]{20,}', r'xox[abposr]-[A-Za-z0-9-]{10,}', r'AIza[0-9A-Za-z_-]{35}',
        r'[sr]k_(?:live|test)_[0-9A-Za-z]{16,}', r'sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}',
        r'npm_[A-Za-z0-9]{36}'))))),
    ('credential', re.compile(
        r'(?<![A-Za-z0-9])(?:pass(?:word|wd|phrase)|pwd|(?:client[_-]?)?secret'
        r'|(?:access[_-]?|auth[_-]?|refresh[_-]?)?token|api[_-]?key|access[_-]?key|private[_-]?key)'
        r'''(?![A-Za-z0-9])["']?[ \t]*[:=](?!=)[ \t]*(?:"[^"\n]+"|'[^'\n]+'|[^\s"'`,;]+)''', re.I)),
    ('url', re.compile(r'''(?i:\b(?:https?|ftp|ssh|git|wss?)://|\bwww\.)[^\s<>"'`)\]]*[^\s<>"'`)\].,;:!?]''')),
    ('email', re.compile(r'(?<![\w.+%-])[\w.+%-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}(?![\w-])')),
    ('ip', re.compile(
        rf'(?<![\w.]){_OCTET}(?:\.{_OCTET}){{3}}(?:/\d{{1,2}})?(?![\w]|\.\d)'
        rf'|(?<![\w:])(?:(?:{_HEX}:){{7}}{_HEX}|(?:{_HEX}:){{1,7}}:(?:{_HEX}(?::{_HEX}){{0,6}})?'
        rf'|::{_HEX}(?::{_HEX}){{0,6}})(?:/\d{{1,3}})?(?![\w:])')),
    ('phone', re.compile(
        r'(?<![\w+.,/-])(?:\+\d{1,3}(?:[ .-]?\(?\d{1,4}\)?){2,6}'
        r'|\(\d{2,5}\)[ .-]?\d{2,4}(?:[ .-]?\d{2,4}){1,3}|\d{3}([.-])\d{3}\1\d{4})(?![\w%]|[.,/-]\d)')),
    ('meeting', re.compile(rf'^[ \t]*[\[(]?{_TIME}[\])]?(?=[ \t]|$)', re.M)),
    ('meeting', re.compile(
        rf'^[ \t]*(?:[\[(]?{_TIME}[\])]?[ \t]+)?(?:\*\*)?'
        r"(?P<label>[^\W\d_][\w.'’-]*(?:[ \t][^\W\d_][\w.'’-]*){0,2})(?:\*\*)?:(?=(?:\*\*)?[ \t]+\S)", re.M)),
    ('date', re.compile(
        r'(?<![\w.:/-])(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?'
        r'|\d{4}/\d{1,2}/\d{1,2}|\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}([/-])\d{1,2}\1(?:\d{4}|\d{2}))'
        r'(?![\w/-]|[.:]\d)')),
    ('date', re.compile(
        rf'\b(?:{_MONTH}[ \t]+\d{{1,2}}{_ORDINAL}(?:,?[ \t]+\d{{4}})?'
        rf'|\d{{1,2}}{_ORDINAL}[ \t]+(?:of[ \t]+)?{_MONTH}(?:,?[ \t]+\d{{4}})?'
        rf'|\d{{1,2}}[ \t]+{_MONTH_RU}(?:[ \t]+\d{{4}})?)(?![\w])')),
    ('date', re.compile(
        r'(?<![\w:.+])(?:\d{1,2}:\d{2}(?::\d{2})?(?:[ \t]?(?i:[ap]\.?m\.?))?|\d{1,2}[ \t]?(?i:[ap]\.?m\.?))'
        r'(?![\w:]|\.\d)')),
    ('hostname', re.compile(
        r'(?<![\w@/.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[a-z]{2,24}(?![\w(-]|\.[A-Za-z0-9])')),
)

# Generic labels that look like `Speaker:` lines in ordinary notes.
_LABELS = {'note', 'notes', 'nb', 'example', 'examples', 'lesson', 'lessons', 'summary', 'context',
           'problem', 'solution', 'rule', 'why', 'how', 'what', 'when', 'where', 'tip', 'warning',
           'caution', 'important', 'todo', 'result', 'results', 'source', 'sources', 'update',
           'status', 'see', 'also', 'goal', 'goals', 'reason', 'fix', 'cause', 'impact',
           'decision', 'decisions', 'action', 'actions', 'outcome', 'background', 'details',
           'takeaway', 'takeaways', 'step', 'steps', 'usage', 'input', 'output'}
_EXTENSIONS = set('''md markdown txt rst toml json jsonl ndjson yaml yml ini cfg conf env lock log csv
    tsv xml html htm css scss sass less py pyc pyi ipynb js mjs cjs ts tsx jsx vue svelte sh bash zsh
    fish ps1 bat cmd rs go java kt kts scala c h cc cpp cxx hpp cs rb php pl lua swift m mm r sql
    proto tf hcl gradle pdf png jpg jpeg gif svg webp ico bmp tiff doc docx odt xls xlsx ods ppt
    pptx odp zip gz tgz tar bz2 xz zst whl jar exe dll so dylib bin iso dmg deb rpm vtt srt mp3 mp4
    wav ogg mov mkv avi pem key crt cer csr der p12 pfx pub patch diff tmp bak swp orig'''.split())
_FENCE = re.compile(r' {0,3}(`{3,}|~{3,})(.*)')
_QUOTED = re.compile(rf'"[^"\n]{{{MIN_QUOTE},}}"|“[^”\n]{{{MIN_QUOTE},}}”|«[^»\n]{{{MIN_QUOTE},}}»'
                     rf'|„[^“”\n]{{{MIN_QUOTE},}}[“”]')


def _valid(category, match):
    text = match.group(0)
    if category == 'meeting' and match.groupdict().get('label'):
        words = match.group('label').split()
        return all(w[0].isupper() for w in words) and not (
            len(words) == 1 and words[0].casefold().strip('.') in _LABELS)
    if category == 'phone':
        digits = sum(c.isdigit() for c in text)
        if text.startswith('+'):
            return 10 <= digits <= 15
        return 10 <= digits <= 15 and (not text.startswith('(') or '-' in text)
    if category == 'ip' and ':' in text:
        try:
            ipaddress.IPv6Interface(text)
        except ValueError:
            return False
    if category == 'hostname':
        return text.rsplit('.', 1)[1] not in _EXTENSIONS
    if category == 'date':
        numbers = re.match(r'(\d{1,2}):(\d{2})', text)
        if numbers:
            return int(numbers.group(1)) <= 24 and int(numbers.group(2)) < 60
        clock = re.fullmatch(r'(\d{1,2})[ \t]?[ap]\.?m\.?', text, re.I)
        if clock:
            return 1 <= int(clock.group(1)) <= 12
        parts = re.fullmatch(r'(\d{1,2})([./-])(\d{1,2})\2\d+', text)
        if parts:
            return 1 <= int(parts.group(1)) <= 31 and 1 <= int(parts.group(3)) <= 31
    return True


def _span(category, match):
    if category == 'meeting' and match.groupdict().get('label'):
        start = match.start('label')
        return start, match.end()
    start, end = match.span()
    if category == 'meeting':
        start += len(match.group(0)) - len(match.group(0).lstrip())
    return start, end


def _lines(text):
    offset = 0
    for line in text.split('\n'):
        yield offset, line
        offset += len(line) + 1


def _blocks(text):
    """Fenced code blocks and contiguous blockquotes outside them, as (category, start, end)."""
    fence = quote = None
    for offset, line in _lines(text):
        end = offset + len(line)
        mark = _FENCE.match(line)
        if fence:
            start, char, size = fence
            if mark and mark.group(1)[0] == char and len(mark.group(1)) >= size and not mark.group(2).strip():
                yield 'code-block', start, end
                fence = None
            continue
        if mark and not (mark.group(1)[0] == '`' and '`' in mark.group(2)):
            if quote:
                yield 'quote', *quote
                quote = None
            fence = (offset, mark.group(1)[0], len(mark.group(1)))
        elif re.match(r' {0,3}>', line):
            quote = (quote[0] if quote else offset, end)
        elif quote:
            yield 'quote', *quote
            quote = None
    if fence:
        yield 'code-block', fence[0], len(text)
    if quote:
        yield 'quote', *quote


def _term_pattern(terms):
    labels = {}
    for term, label in terms:
        words = term.split()
        key = ' '.join(words).casefold()
        if len(key) >= MIN_TERM and key not in labels:
            labels[key] = (words, label)
    if not labels:
        return None, labels
    alternatives = sorted(labels.values(), key=lambda item: -len(' '.join(item[0])))
    body = '|'.join(r'\s+'.join(map(re.escape, words)) for words, _ in alternatives)
    return re.compile(rf'(?<!\w)(?:{body})(?!\w)', re.I), labels


def scan(text, terms=()):
    """Return findings for text; terms are (term, label) pairs. The text is never changed."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    candidates = [(start, end, category) for category, start, end in _blocks(text)]
    candidates += [(m.start(), m.end(), 'quote') for m in _QUOTED.finditer(text)
                   if len(m.group(0)[1:-1].split()) >= MIN_QUOTE_WORDS]
    taken = []
    for category in _EXCLUSIVE:
        for name, pattern in _PATTERNS:
            if name != category:
                continue
            for match in pattern.finditer(text):
                start, end = _span(category, match)
                if end > start and _valid(category, match) and not any(
                        start < e and s < end for s, e, _ in taken):
                    taken.append((start, end, category))
    candidates += taken
    pattern, labels = _term_pattern(terms)
    hints = {}
    if pattern:
        for match in pattern.finditer(text):
            key = ' '.join(match.group(0).split()).casefold()
            candidates.append((match.start(), match.end(), 'term'))
            label = labels.get(key, (None, 'listed term'))[1]
            hints[match.start()] = f'source-domain term ({label}): generalise it unless it may be kept'
    order = list(_TIER)
    findings, seen = [], {}
    for start, end, category in sorted(candidates, key=lambda c: (c[0], order.index(c[2]), c[1])):
        matched = text[start:end]
        normalized = ' '.join(matched.split()).casefold()
        occurrence = seen[category, normalized] = seen.get((category, normalized), 0) + 1
        excerpt = matched.split('\n', 1)[0].strip()
        findings.append(dict(
            id=hashlib.sha256(f'{category}|{normalized}|{occurrence}'.encode('utf-8')).hexdigest()[:10],
            tier=_TIER[category], category=category, line=text.count('\n', 0, start) + 1,
            excerpt=excerpt if len(excerpt) <= EXCERPT else excerpt[:EXCERPT - 1] + '…',
            hint=hints[start] if category == 'term' else _HINT[category]))
    return findings


@dataclass(frozen=True)
class Domain:
    id: str
    path: Path
    terms: tuple


def _toml(path, root):
    if path.is_symlink() or not path.is_file():
        raise WikiError(f'{path.relative_to(root)}: must be a regular file')
    try:
        return tomllib.loads(path.read_text(encoding='utf-8'))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise WikiError(f'{path.relative_to(root)}: {error}') from None


def _strings(value, where):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise WikiError(f'{where}: expected a list of strings')
    return value


def _title(readme):
    """First ATX H1 of a README, skipping a +++ or --- front matter block."""
    if readme.is_symlink() or not readme.is_file():
        return None
    lines = readme.read_text(encoding='utf-8', errors='replace').splitlines()
    if lines and lines[0].strip() in ('+++', '---'):
        close = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == lines[0].strip()), None)
        lines = lines[close + 1:] if close else lines
    for line in lines:
        heading = re.match(r' {0,3}#[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$', line)
        if heading:
            return heading.group(1)
    return None


def _spaces(base, root, ident):
    for directory, dirs, names in os.walk(base, followlinks=False):
        here = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not (here / d).is_symlink())
        if '.wiki.toml' not in names or (here / '.wiki.toml').is_symlink():
            continue
        meta = _toml(here / '.wiki.toml', root)
        if isinstance(meta.get('id'), str):
            yield meta['id'], f'{ident}: space id'
        title = _title(here / 'README.md')
        if title:
            yield title, f'{ident}: space title'


def domains(root):
    """Domains are top-level directories holding a regular `.domain.toml` file."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise WikiError(f'wiki root is not a directory: {root}')
    found = []
    for entry in sorted(root.iterdir()):
        marker = entry / '.domain.toml'
        if entry.is_symlink():
            if os.path.lexists(marker):
                raise WikiError(f'{entry.name}: symlinked domain refused')
            continue
        if not entry.is_dir() or not os.path.lexists(marker):
            continue
        meta = _toml(marker, root)
        where = f'{entry.name}/.domain.toml'
        if type(meta.get('schema_version')) is not int or meta['schema_version'] != 1:
            raise WikiError(f'{where}: schema_version must be 1')
        ident = meta.get('id')
        if not isinstance(ident, str) or not DOMAIN_ID.fullmatch(ident) or ident != entry.name:
            raise WikiError(f'{where}: id must be a lowercase kebab id equal to the directory name')
        title = meta.get('title', '')
        sensitive = meta.get('sensitive', {})
        if not isinstance(title, str) or not isinstance(sensitive, dict):
            raise WikiError(f'{where}: title must be a string and [sensitive] a table')
        terms = [(ident, f'{ident}: domain id'), (title, f'{ident}: domain title')]
        terms += [(a, f'{ident}: alias') for a in _strings(sensitive.get('aliases', []), f'{where} aliases')]
        terms += [(t, f'{ident}: sensitive term') for t in _strings(sensitive.get('terms', []), f'{where} terms')]
        terms += _spaces(entry, root, ident)
        found.append(Domain(ident, entry, tuple(terms)))
    return found


def check(root, text, source=None, target=None, outbound=False):
    """Findings for text: terms of the source domain, or of every domain when outbound."""
    known = {d.id: d for d in domains(root)}
    for name in (source, target):
        if name is not None and name not in known:
            raise WikiError(f'unknown domain: {name}')
    if source is None and not outbound:
        raise WikiError('--from DOMAIN is required unless --outbound')
    selected = known.values() if outbound else [known[source]]
    terms = [term for d in selected for term in d.terms]
    return {'findings': scan(text, terms), 'limits': LIMITS}


def add_parser(commands):
    guard = commands.add_parser('guard', help='Report possibly sensitive text (best effort; never edits)')
    actions = guard.add_subparsers(dest='action', required=True)
    sub = actions.add_parser(
        'check', help='Print findings for a file or text as JSON',
        description='Lexically scan text for secrets, client data, code, quotes, meeting content, '
                    'URLs/hosts/IPs, dates and the source domain terms. Findings are information: '
                    'the exit status is 0 whenever the scan ran. Pass text that starts with a dash '
                    'as --text=VALUE.',
        epilog=LIMITS)
    given = sub.add_mutually_exclusive_group(required=True)
    given.add_argument('--file', help='UTF-8 file to scan; it is only read')
    given.add_argument('--text', help='Text to scan')
    sub.add_argument('--from', dest='source', metavar='DOMAIN', help='Domain the text comes from; its terms are reported')
    sub.add_argument('--to', dest='target', metavar='DOMAIN', help='Destination domain (validated)')
    sub.add_argument('--outbound', action='store_true',
                     help='Text leaves the machine (research query): report the terms of all domains')


def run(args, ctx):
    text = args.text if args.file is None else Path(args.file).read_bytes().decode('utf-8')
    return check(ctx.root, text, args.source, args.target, args.outbound)
