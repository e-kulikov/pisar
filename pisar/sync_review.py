"""Read-only, domain-partitioned semantic review for sync candidates."""
from dataclasses import dataclass
from importlib.resources import files
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

from . import codex_agent, documents, gitops, operations, research, settings, sync_git
from .safety import WikiError
from .spaces import Wiki

PROMPT_VERSION = 'sync-review-v1'
PROMPT = files('pisar').joinpath('sync-review-prompt.md').read_text(encoding='utf-8')
MAX_PATHS = 500
MAX_TEXT = 1_000_000
MAX_PACKAGE = 4_000_000
MAX_STRING = 4000
STATEMENT_DISPOSITIONS = ('preserved', 'equivalent', 'superseded', 'lost', 'uncertain')
SIDES = ('base', 'ours', 'theirs', 'candidate')
VERDICTS = ('clear', 'needs_decision', 'reject')
CATEGORIES = ('omission', 'distortion', 'contradiction', 'provenance', 'task', 'context')
_STATEMENT = {'type': 'object', 'additionalProperties': False,
              'required': ['id', 'side', 'path', 'quote', 'disposition', 'output_path', 'output_quote', 'reason'],
              'properties': {'id': {'type': 'string'}, 'side': {'type': 'string', 'enum': list(SIDES)},
                             'path': {'type': 'string'}, 'quote': {'type': 'string'},
                             'disposition': {'type': 'string', 'enum': list(STATEMENT_DISPOSITIONS)},
                             'output_path': {'type': ['string', 'null']},
                             'output_quote': {'type': ['string', 'null']}, 'reason': {'type': 'string'}}}
_OPTION = {'type': 'object', 'additionalProperties': False,
           'required': ['id', 'candidate_description', 'lost', 'distorted', 'unknown'],
           'properties': {'id': {'type': 'string'}, 'candidate_description': {'type': 'string'},
                          'lost': {'type': 'array', 'items': {'type': 'string'}},
                          'distorted': {'type': 'array', 'items': {'type': 'string'}},
                          'unknown': {'type': 'array', 'items': {'type': 'string'}}}}
_FINDING = {'type': 'object', 'additionalProperties': False,
            'required': ['id', 'category', 'paths', 'evidence_quotes', 'explanation', 'options', 'question'],
            'properties': {'id': {'type': 'string'}, 'category': {'type': 'string', 'enum': list(CATEGORIES)},
                           'paths': {'type': 'array', 'items': {'type': 'string'}},
                           'evidence_quotes': {'type': 'array', 'items': {'type': 'string'}},
                           'explanation': {'type': 'string'}, 'options': {'type': 'array', 'items': _OPTION},
                           'question': {'type': 'string'}}}
SCHEMA = {'type': 'object', 'additionalProperties': False,
          'required': ['schema_version', 'candidate_revision', 'domain', 'model', 'prompt_version',
                       'confidence', 'context_complete', 'verdict', 'statements', 'findings', 'summary'],
          'properties': {'schema_version': {'type': 'integer', 'enum': [1]},
                         'candidate_revision': {'type': 'string'}, 'domain': {'type': 'string'},
                         'model': {'type': 'string'}, 'prompt_version': {'type': 'string'},
                         'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
                         'context_complete': {'type': 'boolean'}, 'verdict': {'type': 'string', 'enum': list(VERDICTS)},
                         'statements': {'type': 'array', 'items': _STATEMENT},
                         'findings': {'type': 'array', 'items': _FINDING}, 'summary': {'type': 'string'}}}


@dataclass(frozen=True)
class SemanticPolicy:
    review: str = 'all'
    approval: str = 'manual'
    agent: str = 'claude'
    min_confidence: float = 0.95
    model: str = 'default'
    effort: str = 'default'
    prompt_version: str = PROMPT_VERSION
    prompt_digest: str = ''


def policy():
    return SemanticPolicy(**settings.semantic_review_policy(), prompt_version=PROMPT_VERSION,
                          prompt_digest=operations.fingerprint(PROMPT))


def acceptance_revision(candidate_revision, report_digest):
    return operations.fingerprint({'candidate_revision': candidate_revision, 'report_digest': report_digest})


def report_digest(reports):
    """Hash the normalized JSON meaning of reports, independent of formatting/key order."""
    return operations.fingerprint(reports)


def validate_stored_report(runtime, operation):
    """Recompute a semantic acceptance token from the immutable candidate and report bytes."""
    review = operation.get('review') or {}
    semantic = operation.get('semantic_review') or {}
    if review.get('kind') == 'reduced-coverage':
        return validate_reduced_coverage(runtime, operation)
    if review.get('kind') != 'semantic':
        if review.get('kind') == 'no-change':
            digest = report_digest([])
            token = acceptance_revision(review.get('candidate_revision'), digest)
            if token != review.get('acceptance_revision') or token != semantic.get('acceptance_revision'):
                raise WikiError('no-change acceptance revision is invalid')
            return token
        return review.get('acceptance_revision', operation.get('revision'))
    candidate_revision = semantic.get('revision')
    path = runtime.path / 'sync-reports' / operation['operation_id'] / f'{candidate_revision}.json'
    if (semantic.get('report_path') != str(path) or path.is_symlink() or not path.is_file()):
        raise WikiError('stored sync review report path is invalid')
    try:
        reports = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        raise WikiError('stored sync review report is invalid') from None
    actual_digest = report_digest(reports)
    if actual_digest != semantic.get('report_digest'):
        raise WikiError('sync review report digest changed after review')
    token = acceptance_revision(candidate_revision, actual_digest)
    if token != review.get('acceptance_revision') or token != semantic.get('acceptance_revision'):
        raise WikiError('sync review report digest does not match its acceptance revision')
    return token


def validate_reduced_coverage(runtime, operation):
    """Validate explicit weaker-policy authorization without inventing a model report."""
    from . import sync
    current = policy()
    review = operation.get('review') or {}
    semantic = operation.get('semantic_review') or {}
    candidate = sync._candidate(runtime, operation['operation_id'])
    if (current.review != 'conflicts' or current.approval != 'auto'
            or review.get('kind') != 'reduced-coverage' or semantic.get('coverage') != 'uncovered'
            or review.get('policy') != current.__dict__ or semantic.get('policy') != current.__dict__
            or review.get('git_revision') != operation.get('revision')
            or semantic.get('git_revision') != operation.get('revision')
            or operation.get('resolver_result') or operation.get('resolver_proposal')
            or len(sync_git.parents(candidate, operation['candidate'])) >= 2):
        raise WikiError('reduced-coverage authorization is stale or ineligible')
    revision = _candidate_revision(operation, requests(Wiki(runtime.root), operation, candidate), current)
    token = operations.fingerprint({'authorization': 'conflicts-only-clean', 'candidate_revision': revision})
    if (review.get('candidate_revision') != revision or semantic.get('revision') != revision
            or review.get('acceptance_revision') != token or semantic.get('acceptance_revision') != token):
        raise WikiError('reduced-coverage authorization no longer matches the candidate and policy')
    return token


def checked_stored_reports(wiki, runtime, operation, candidate):
    """Load the exact checked reports for the sealed candidate and current policy."""
    semantic = operation.get('semantic_review') or {}
    current_policy = policy()
    if (semantic.get('coverage') != 'all' or semantic.get('policy') != current_policy.__dict__
            or semantic.get('git_revision') != operation.get('revision')):
        raise WikiError('stored sync review is stale for the current candidate or policy')
    validate_stored_report(runtime, operation)
    review_requests = requests(wiki, operation, candidate)
    if not review_requests:
        raise WikiError('stored sync review has no changed candidate paths')
    candidate_revision = _candidate_revision(operation, review_requests, current_policy)
    if candidate_revision != semantic.get('revision'):
        raise WikiError('stored sync review is stale for the current candidate context')
    for request in review_requests:
        request['policy'] = current_policy.__dict__
        request['candidate_revision'] = candidate_revision
    report_path = runtime.path / 'sync-reports' / operation['operation_id'] / f'{candidate_revision}.json'
    try:
        reports = json.loads(report_path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        raise WikiError('stored sync review report is invalid') from None
    if not isinstance(reports, list) or len(reports) != len(review_requests):
        raise WikiError('stored sync review does not cover the current candidate')
    checked = [validate_review(request, report) for request, report in zip(review_requests, reports)]
    if report_digest(checked) != semantic.get('report_digest'):
        raise WikiError('sync review report digest changed after review')
    return review_requests, checked


def pinned_domains(repo, operation, paths):
    """Classify root paths from immutable O/T/base marker evidence."""
    commits = [operation.get('ours'), operation.get('theirs')]
    ours, theirs = operation.get('ours'), operation.get('theirs')
    if ours and theirs:
        commits.extend(sync_git.text(repo, 'merge-base', '--all', ours, theirs,
                                     check=False).splitlines())
    result = {}
    for path in paths:
        parts = PurePosixPath(path).parts
        domain = parts[0] if parts else 'workspace'
        if domain != 'workspace':
            marker = f'{domain}/.domain.toml'
            if not any(subprocess.run(sync_git.command(repo, 'cat-file', '-e', f'{commit}:{marker}'),
                                      capture_output=True, env=gitops.environment()).returncode == 0
                       for commit in commits if commit):
                domain = 'workspace'
        result[path] = domain
    return result


def decode_report(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'duplicate key: {key}')
            result[key] = value
        return result
    def constant(value):
        raise ValueError(f'non-finite number: {value}')
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeDecodeError) as error:
        raise WikiError(f'sync review report JSON invalid: {error}') from None
    if not isinstance(value, dict):
        raise WikiError('sync review report must be a JSON object')
    return value


def _text(value, name, *, empty=False):
    if not isinstance(value, str) or len(value) > MAX_STRING or (not empty and not value.strip()):
        raise WikiError(f'sync review {name} must be a bounded {"string" if empty else "nonempty string"}')
    return value


def _keys(value, expected, name):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise WikiError(f'sync review {name} has an invalid schema')


def validate_review(request, response):
    required = ('schema_version', 'candidate_revision', 'domain', 'model', 'prompt_version', 'confidence',
                'context_complete', 'verdict', 'statements', 'findings', 'summary')
    _keys(response, required, 'report')
    if type(response['schema_version']) is not int or response['schema_version'] != 1:
        raise WikiError('sync review schema_version is unsupported')
    for key in ('candidate_revision', 'domain', 'model', 'prompt_version', 'summary'):
        _text(response[key], key, empty=(key == 'summary'))
    if response['candidate_revision'] != request['candidate_revision']:
        raise WikiError('sync review candidate_revision is stale')
    if response['domain'] != request['domain']:
        raise WikiError('sync review domain does not match the requested domain')
    if response['prompt_version'] != PROMPT_VERSION:
        raise WikiError('sync review prompt_version is stale')
    confidence = response['confidence']
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise WikiError('sync review confidence must be finite and from 0 to 1')
    if response['context_complete'] is not True:
        raise WikiError('sync review context_complete must be true')
    if response['verdict'] not in VERDICTS:
        raise WikiError('sync review verdict is invalid')
    versions = request['versions']
    changed = set(request['changed_paths'])
    accounted = set()
    ids = set()
    statements = response['statements']
    findings = response['findings']
    if not isinstance(statements, list) or not isinstance(findings, list) or len(statements) > 500 or len(findings) > 100:
        raise WikiError('sync review statements/findings must be bounded arrays')
    for item in statements:
        fields = ('id', 'side', 'path', 'quote', 'disposition', 'output_path', 'output_quote', 'reason')
        _keys(item, fields, 'statement')
        for key in ('id', 'path', 'quote', 'reason'):
            _text(item[key], f'statement.{key}')
        if item['id'] in ids:
            raise WikiError('sync review contains duplicate statement IDs')
        ids.add(item['id'])
        path = item['path']
        if path not in changed or item['side'] not in SIDES or item['disposition'] not in STATEMENT_DISPOSITIONS:
            raise WikiError('sync review statement references an invalid side, disposition or path')
        if item['quote'] not in versions.get(item['side'], {}).get(path, ''):
            raise WikiError('sync review statement quote is not present in its pinned version')
        accounted.add(path)
        if item['output_path'] is not None:
            _text(item['output_path'], 'statement.output_path')
            if item['output_path'] not in changed:
                raise WikiError('sync review output mapping references an undeclared path')
            if not isinstance(item['output_quote'], str) or item['output_quote'] not in versions['candidate'].get(
                    item['output_path'], ''):
                raise WikiError('sync review output quote is not present in the candidate')
        elif item['output_quote'] is not None:
            raise WikiError('sync review output_quote requires output_path')
    for item in findings:
        fields = ('id', 'category', 'paths', 'evidence_quotes', 'explanation', 'options', 'question')
        _keys(item, fields, 'finding')
        for key in ('id', 'explanation', 'question'):
            _text(item[key], f'finding.{key}', empty=(key == 'question'))
        if item['id'] in ids:
            raise WikiError('sync review contains duplicate IDs')
        ids.add(item['id'])
        if item['category'] not in CATEGORIES or not isinstance(item['paths'], list) or not item['paths']:
            raise WikiError('sync review finding category/paths are invalid')
        if not all(isinstance(path, str) and path in changed for path in item['paths']):
            raise WikiError('sync review finding references an undeclared path')
        accounted.update(item['paths'])
        if not isinstance(item['evidence_quotes'], list):
            raise WikiError('sync review evidence_quotes must be a list')
        corpus = '\n'.join(content for per_side in versions.values() for content in per_side.values())
        corpus += '\n' + '\n'.join(request.get('context', {}).values())
        if not all(isinstance(quote, str) and quote and quote in corpus for quote in item['evidence_quotes']):
            raise WikiError('sync review finding evidence quote is not present in pinned versions')
        if not isinstance(item['options'], list) or len(item['options']) > 20:
            raise WikiError('sync review options must be a bounded list')
        for option in item['options']:
            _keys(option, ('id', 'candidate_description', 'lost', 'distorted', 'unknown'), 'option')
            _text(option['id'], 'option.id')
            _text(option['candidate_description'], 'option.candidate_description', empty=True)
            for key in ('lost', 'distorted', 'unknown'):
                if not isinstance(option[key], list) or len(option[key]) > 100:
                    raise WikiError(f'sync review option {key} must be a bounded list')
                for detail in option[key]:
                    _text(detail, f'option.{key}')
    unresolved = [item for item in statements if item['disposition'] in ('lost', 'uncertain', 'superseded')]
    for statement in unresolved:
        matching = [item for item in findings if statement['path'] in item['paths']]
        if not matching:
            raise WikiError('sync review lost/uncertain/superseded statement needs an evidence finding')
    if (unresolved or findings) and response['verdict'] == 'clear':
        raise WikiError('sync review cannot be clear with unresolved findings or statement dispositions')
    if changed - accounted:
        raise WikiError('sync review did not account for every changed path')
    return response


def claude_command(executable, model, effort):
    import json as json_module
    return [executable, '-p', '--output-format', 'json', '--json-schema', json_module.dumps(SCHEMA),
            *(['--model', model] if model else []), *(['--effort', effort] if effort else []),
            '--safe-mode', '--system-prompt', PROMPT, '--tools=', '--strict-mcp-config', '--no-session-persistence']


def claude_answer(output):
    """Decode Claude's JSON envelope into the common report object."""
    envelope = decode_report(output)
    if isinstance(envelope.get('structured_output'), dict):
        return envelope['structured_output']
    if isinstance(envelope.get('result'), str):
        return decode_report(envelope['result'].encode('utf-8'))
    raise WikiError('Claude sync reviewer returned no structured report')


def codex_answer(output):
    """Decode the Codex helper's schema-constrained JSON file."""
    return decode_report(output)


def codex_command(executable, home, cwd):
    instructions = Path(home) / 'sync-review-prompt.md'
    schema = Path(home) / 'sync-review-schema.json'
    return codex_agent.exec_argv(executable, cwd, Path(cwd) / 'answer.json', schema, instructions,
                                 settings.agent_option('codex', 'model', 'reviewer')[0],
                                 settings.agent_option('codex', 'effort', 'reviewer')[0], 'disabled')


def _read_blob(repo, commit, path):
    exists = subprocess.run(sync_git.command(repo, 'cat-file', '-e', f'{commit}:{path}'),
                            capture_output=True, env=gitops.environment())
    if exists.returncode:
        return None
    data = sync_git.run(repo, 'show', f'{commit}:{path}')
    if len(data) > MAX_TEXT:
        raise WikiError(f'sync review context file is too large: {path}')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        raise WikiError(f'sync review cannot package non-UTF-8 changed file: {path}') from None


def _graph_blob(operation, candidate, root_commit, path):
    """Read a blob from the candidate repository that owns PATH."""
    owners = [item for item in operation.get('graph', []) if item['path'] != '.'
              and path.startswith(item['path'] + '/')]
    if not owners:
        return _read_blob(candidate, root_commit, path)
    item = max(owners, key=lambda value: len(value['path']))
    repo = Path(candidate) / item['path']
    relative = path[len(item['path']) + 1:]
    return _read_blob(repo, item['candidate'], relative)


def requests(wiki, operation, candidate):
    repo = candidate
    ours, theirs, result = operation['ours'], operation['theirs'], operation['candidate']
    paths = set()
    sources = {}
    for before in (ours, theirs):
        raw = sync_git.run(repo, 'diff', '--name-only', '-z', before, result)
        paths.update(p.decode('utf-8', errors='surrogateescape') for p in raw.split(b'\0') if p)
    # Gitlinks are opaque commit objects; review the changed files from each child candidate.
    for item in operation.get('graph', []):
        if item['path'] == '.':
            continue
        child_repo = Path(candidate) / item['path']
        child_result = item['candidate']
        child_ours, child_theirs = item['ours'], item.get('theirs', item['candidate'])
        bases_child = sync_git.text(child_repo, 'merge-base', '--all', child_ours, child_theirs,
                                    check=False).splitlines()
        child_base = bases_child[0] if bases_child else child_theirs
        child_paths = set()
        for before in (child_ours, child_theirs):
            raw = sync_git.run(child_repo, 'diff', '--name-only', '-z', before, child_result)
            child_paths.update(p.decode('utf-8', errors='surrogateescape') for p in raw.split(b'\0') if p)
        for relative in child_paths:
            combined = f'{item["path"]}/{relative}'
            paths.add(combined)
            sources[combined] = (child_repo, child_base, child_ours, child_theirs, child_result, relative)
    # Exclude gitlink entries from root blob reads.
    gitlinks = set()
    for commit in (ours, theirs, result):
        for record in sync_git.run(repo, 'ls-tree', '-r', '-z', commit).split(b'\0'):
            if record.startswith(b'160000 '):
                gitlinks.add(record.split(b'\t', 1)[1].decode('utf-8', 'surrogateescape'))
    paths.difference_update(gitlinks)
    for path in paths:
        sources.setdefault(path, (repo, None, ours, theirs, result, path))
    if len(paths) > MAX_PATHS:
        raise WikiError('sync review has too many changed paths')
    if not paths:
        return []
    bases = sync_git.text(repo, 'merge-base', '--all', ours, theirs, check=False).splitlines()
    base = bases[0] if bases else theirs
    candidate_wiki = Wiki(candidate)
    path_domains = {PurePosixPath(path).parts[0] for path in paths if PurePosixPath(path).parts}
    known_domains = set()
    for domain in path_domains:
        marker = f'{domain}/.domain.toml'
        if _graph_blob(operation, candidate, result, marker) is not None:
            known_domains.add(domain)
            continue
        if domain in candidate_wiki.domains:
            raise WikiError(f'sync review cannot establish pinned domain ownership: {domain}')
        # Retain the boundary for domains removed from the result, using the
        # pinned history in whichever graph repository owns the marker.
        owners = [item for item in operation.get('graph', []) if item['path'] != '.'
                  and marker.startswith(item['path'] + '/')]
        if owners:
            item = max(owners, key=lambda value: len(value['path']))
            marker_repo = Path(candidate) / item['path']
            marker_path = marker[len(item['path']) + 1:]
            commits = (item['ours'], item.get('theirs', item['candidate']))
        else:
            marker_repo, marker_path, commits = repo, marker, (base, ours, theirs)
        if any(_read_blob(marker_repo, commit, marker_path) is not None for commit in commits):
            known_domains.add(domain)
    groups = {}
    for path in sorted(paths):
        parts = PurePosixPath(path).parts
        domain = parts[0] if parts and parts[0] in known_domains else 'workspace'
        owner = candidate_wiki.owner(Path(candidate) / path)
        source_repo, source_base, source_ours, source_theirs, source_result, source_path = sources[path]
        versions = {side: _read_blob(source_repo, commit, source_path) for side, commit in
                    (('base', source_base or base), ('ours', source_ours), ('theirs', source_theirs),
                     ('candidate', source_result))}
        groups.setdefault(domain, {'files': {}, 'spaces': set()})['files'][path] = versions
        if owner is not None:
            groups[domain]['spaces'].add(owner)
    answer = []
    for domain, group in sorted(groups.items()):
        files = group['files']
        sides = {side: {path: versions[side] for path, versions in files.items() if versions[side] is not None}
                 for side in SIDES}
        context = {}
        omitted_context = []
        linked_references = set()
        for content in sides['candidate'].values():
            linked_references.update(match.group(0).split('#')[0]
                                     for match in documents.REF.finditer(content))
        dependency_paths = set()
        if domain != 'workspace':
            docs, _ = documents.scan(candidate_wiki, {domain})
            by_reference = {doc.reference: doc.path for doc in docs}
            for reference in linked_references:
                match = documents.REF.fullmatch(reference)
                if match and match.group(1).split('/', 1)[0] == domain:
                    target = by_reference.get(reference)
                    if target is None:
                        omitted_context.append(f'referenced:{reference}')
                    else:
                        dependency_paths.add(target.relative_to(candidate).as_posix())
        for space in group['spaces']:
            for path in candidate_wiki.files():
                if path.is_relative_to(space.path):
                    relative = path.relative_to(candidate).as_posix()
                    if relative in files:
                        continue
                    if path.suffix.lower() not in ('.md', '.txt', '.rst', '.csv', '.tsv', '.toml', '.json', '.yaml', '.yml'):
                        if 'sources' in path.relative_to(space.path).parts:
                            omitted_context.append(relative)
                        continue
                    data = _graph_blob(operation, candidate, result, relative)
                    if data is not None:
                        context[relative] = data
                        if sum(len(text.encode('utf-8')) for text in context.values()) > MAX_TEXT:
                            raise WikiError(f'sync review context is too large in {space.address}')
        for relative in sorted(dependency_paths):
            if relative in files or relative in context:
                continue
            data = _graph_blob(operation, candidate, result, relative)
            if data is not None:
                context[relative] = data
                if sum(len(text.encode('utf-8')) for text in context.values()) > MAX_TEXT:
                    raise WikiError(f'sync review context is too large in {domain}')
        complete = not omitted_context
        answer.append({'candidate_revision': operation['revision'], 'domain': domain,
                       'changed_paths': sorted(files), 'versions': sides, 'context': context,
                       'context_complete': complete, 'omitted_context': sorted(omitted_context)})
        if sum(len(json.dumps(item, ensure_ascii=False).encode('utf-8')) for item in answer) > MAX_PACKAGE:
            raise WikiError('sync review context package is too large')
    return answer


def review_candidate(wiki, operation, candidate, backend):
    """Run BACKEND once per domain and validate each immutable-context report."""
    generated = []
    for request in requests(wiki, operation, candidate):
        response = backend(request)
        generated.append(validate_review(request, response))
    return generated


def review_prospective_tree(wiki, operation, candidate, tree, *, backend=None, runtime_base=None):
    """Review an uncommitted tree without creating a commit object or moving a ref."""
    projected = dict(operation)
    projected['candidate'] = tree
    projected['tree'] = tree
    projected['revision'] = operations.fingerprint({'base_revision': operation.get('revision'),
                                                      'prospective_tree': tree})
    current_policy = policy()
    review_requests = requests(wiki, projected, candidate)
    for request in review_requests:
        request['policy'] = current_policy.__dict__
        if not request['context_complete']:
            omitted = ', '.join(request['omitted_context'][:10])
            raise WikiError(f'semantic review context is incomplete; inspect omitted files: {omitted}')
    candidate_revision = _candidate_revision(projected, review_requests, current_policy)
    for request in review_requests:
        request['candidate_revision'] = candidate_revision
    reports = []
    if review_requests:
        active_backend = backend or _configured_backend(wiki, runtime_base)
        for request in review_requests:
            checked = validate_review(request, active_backend(request))
            if backend is None:
                checked['model'] = current_policy.model
            reports.append(checked)
    if len(reports) != len(review_requests):
        raise WikiError('prospective semantic review report count is incomplete')
    return {'operation_id': operation['operation_id'], 'status': 'reviewed' if reports else 'no-change',
            'candidate_revision': candidate_revision, 'reports': reports,
            'policy': current_policy.__dict__, '_policy': current_policy,
            '_requests': review_requests}


def record_prospective_review(runtime, operation, reviewed):
    """Bind already validated pre-commit reports to the commit and record them."""
    current_policy = reviewed['_policy']
    review_requests = reviewed['_requests']
    candidate_revision = _candidate_revision(operation, review_requests, current_policy)
    reports = []
    for request, prior in zip(review_requests, reviewed['reports']):
        bound = dict(prior, candidate_revision=candidate_revision)
        request['candidate_revision'] = candidate_revision
        reports.append(bound)
    digest = report_digest(reports)
    token = acceptance_revision(candidate_revision, digest)
    report_dir = runtime.path / 'sync-reports' / operation['operation_id']
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    report_path = report_dir / f'{candidate_revision}.json'
    operations.atomic_bytes(report_path, (json.dumps(reports, ensure_ascii=False, indent=2) + '\n').encode())
    operation['semantic_review'] = {'coverage': 'all', 'revision': candidate_revision,
                                    'git_revision': operation['revision'],
                                    'acceptance_revision': token, 'report_digest': digest,
                                    'policy': current_policy.__dict__, 'report_path': str(report_path),
                                    'verdict': 'clear' if all(r['verdict'] == 'clear' for r in reports)
                                    else 'needs_decision'}
    operation['review'] = dict(kind='semantic', revision=candidate_revision,
                               candidate_revision=candidate_revision, acceptance_revision=token,
                               git_revision=operation['revision'], policy=current_policy.__dict__,
                               report_path=str(report_path))
    operation['status'] = 'reviewed'
    return {'operation_id': operation['operation_id'], 'status': 'reviewed',
            'revision': token, 'candidate_revision': candidate_revision,
            'reports': reports, 'policy': current_policy.__dict__}


def _candidate_revision(operation, requests, current_policy):
    context = [{key: request[key] for key in
                ('domain', 'changed_paths', 'versions', 'context', 'context_complete', 'omitted_context')}
               for request in requests]
    return operations.fingerprint({'git_candidate_revision': operation['revision'],
                                   'policy': current_policy.__dict__, 'prompt_version': PROMPT_VERSION,
                                   'context': context})


def review_operation(wiki, state_dir, ident, *, backend=None):
    """Run configured read-only reviewer and persist reports outside Git."""
    from datetime import datetime, timezone
    from . import sync
    runtime = operations.Runtime(state_dir, wiki.root)
    with runtime.writer(ident):
        operation = sync._load(runtime, ident)
        if operation.get('status') not in ('prepared', 'reviewed') or not operation.get('candidate'):
            raise WikiError('semantic review requires a sealed prepared candidate')
        candidate = sync._candidate(runtime, ident)
        sync._assert_candidate(operation, candidate)
        current_policy = policy()
        frozen_revision = operation['revision']
        review_requests = requests(wiki, operation, candidate)
        for request in review_requests:
            request['policy'] = current_policy.__dict__
            if not request['context_complete']:
                omitted = ', '.join(request['omitted_context'][:10])
                raise WikiError(f'semantic review context is incomplete; inspect omitted files: {omitted}')
        candidate_revision = _candidate_revision(operation, review_requests, current_policy)
        for request in review_requests:
            request['candidate_revision'] = candidate_revision
        prior = operation.get('semantic_review')
        if (prior and prior.get('revision') == candidate_revision and prior.get('policy') == current_policy.__dict__
                and prior.get('coverage') == 'all'):
            report_path = runtime.path / 'sync-reports' / ident / f'{candidate_revision}.json'
            if prior.get('report_path') != str(report_path) or report_path.is_symlink() or not report_path.is_file():
                raise WikiError('stored sync review report path is invalid')
            cached = json.loads(report_path.read_text(encoding='utf-8'))
            if len(cached) != len(review_requests):
                raise WikiError('stored sync review does not cover the current candidate')
            cached = [validate_review(request, report) for request, report in zip(review_requests, cached)]
            digest = report_digest(cached)
            if digest != prior.get('report_digest'):
                raise WikiError('sync review report digest changed after review')
            token = acceptance_revision(candidate_revision, digest)
            if token != prior.get('acceptance_revision'):
                raise WikiError('sync review report digest does not match its acceptance revision')
            return {'operation_id': ident, 'status': 'reviewed', 'revision': token,
                    'candidate_revision': candidate_revision,
                    'reports': cached,
                    'policy': current_policy.__dict__, 'reused': True}
        if (current_policy.review == 'conflicts'
                and len(sync_git.parents(candidate, operation['candidate'])) < 2
                and (review_requests or current_policy.approval != 'auto')):
            if current_policy.approval == 'auto' and review_requests and not (
                    operation.get('resolver_result') or operation.get('resolver_proposal')):
                token = operations.fingerprint({'authorization': 'conflicts-only-clean',
                                                'candidate_revision': candidate_revision})
                operation['review'] = dict(kind='reduced-coverage', candidate_revision=candidate_revision,
                                           git_revision=frozen_revision, acceptance_revision=token,
                                           policy=current_policy.__dict__)
                operation['semantic_review'] = dict(coverage='uncovered', revision=candidate_revision,
                                                    git_revision=frozen_revision, acceptance_revision=token,
                                                    policy=current_policy.__dict__,
                                                    label='not semantically reviewed')
                operation['accepted'] = None
                operation['status'] = 'reviewed'
                sync._save(runtime, operation)
                return dict(operation_id=ident, status='uncovered', revision=token,
                            label='not semantically reviewed', policy=current_policy.__dict__)
            operation['review'] = None
            operation['semantic_review'] = {'coverage': 'uncovered', 'revision': candidate_revision,
                                            'policy': current_policy.__dict__}
            operation['accepted'] = None
            operation['status'] = 'prepared'
            sync._save(runtime, operation)
            return {'operation_id': ident, 'status': 'uncovered', 'label': 'not semantically reviewed'}
        if prior:
            operation['review'] = None
            operation['semantic_review'] = None
            operation['accepted'] = None
            operation['status'] = 'prepared'
            sync._save(runtime, operation)
        reports = []
        if review_requests:
            active_backend = backend or _configured_backend(wiki, runtime.base)
            for request in review_requests:
                checked = validate_review(request, active_backend(request))
                if backend is None:
                    checked['model'] = current_policy.model
                reports.append(checked)
    if not reports:
        with runtime.writer(ident):
            operation = sync._load(runtime, ident)
            if operation.get('revision') != frozen_revision or operation.get('status') not in ('prepared', 'reviewed'):
                raise WikiError('sync review became stale while the reviewer was running')
            digest = report_digest([])
            token = acceptance_revision(candidate_revision, digest)
            operation['review'] = {'kind': 'no-change', 'revision': candidate_revision,
                                   'candidate_revision': candidate_revision,
                                   'git_revision': frozen_revision,
                                   'acceptance_revision': token, 'policy': current_policy.__dict__}
            operation['semantic_review'] = {'coverage': 'no-change', 'revision': candidate_revision,
                                            'git_revision': frozen_revision,
                                            'acceptance_revision': token, 'report_digest': digest,
                                            'policy': current_policy.__dict__, 'verdict': 'clear'}
            operation['status'] = 'reviewed'
            sync._save(runtime, operation)
        return {'operation_id': ident, 'status': 'no-change', 'revision': token,
                'candidate_revision': candidate_revision}
    with runtime.writer(ident):
        operation = sync._load(runtime, ident)
        candidate = sync._candidate(runtime, ident)
        sync._assert_candidate(operation, candidate)
        if (operation.get('revision') != frozen_revision or policy() != current_policy
                or operation.get('status') not in ('prepared', 'reviewed')):
            raise WikiError('sync review became stale while the reviewer was running')
        report_dir = runtime.path / 'sync-reports' / ident
        report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        report_path = report_dir / f'{candidate_revision}.json'
        operations.atomic_bytes(report_path, (json.dumps(reports, ensure_ascii=False, indent=2) + '\n').encode())
        digest = report_digest(reports)
        token = acceptance_revision(candidate_revision, digest)
        operation['semantic_review'] = {'coverage': 'all', 'revision': candidate_revision,
                                       'git_revision': frozen_revision,
                                       'acceptance_revision': token, 'report_digest': digest,
                                       'policy': current_policy.__dict__, 'report_path': str(report_path),
                                       'verdict': 'clear' if all(r['verdict'] == 'clear' for r in reports)
                                       else 'needs_decision', 'recorded_at': datetime.now(timezone.utc).isoformat()}
        operation['review'] = dict(kind='semantic', revision=candidate_revision,
                                   candidate_revision=candidate_revision, acceptance_revision=token,
                                   git_revision=frozen_revision,
                                   policy=current_policy.__dict__, report_path=str(report_path))
        operation['status'] = 'reviewed'
        sync._save(runtime, operation)
        return {'operation_id': ident, 'status': 'reviewed', 'revision': token,
                'candidate_revision': candidate_revision,
                'reports': reports, 'policy': current_policy.__dict__}


def _configured_backend(wiki, runtime_base):
    reviewer_policy = policy()
    agent = reviewer_policy.agent
    if agent == 'claude':
        return _claude_backend(wiki, runtime_base)
    if agent == 'codex':
        return _codex_backend(wiki, runtime_base)
    raise WikiError(f'unsupported sync semantic reviewer: {agent}')


def _claude_backend(wiki, runtime_base):
    from . import research
    executable = shutil.which('claude')
    if not executable:
        raise WikiError('claude is not on PATH')
    config_home = operations.outside_git(Path(runtime_base) / 'agents' / 'claude' / 'sync-review')
    config_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    def run(request):
        prompt = json.dumps(request, ensure_ascii=False)
        if len(prompt.encode()) > MAX_TEXT:
            raise WikiError('sync review request is too large')
        with tempfile.TemporaryDirectory(prefix='pisar-sync-review-', dir=research.scratch_base(wiki.root, runtime_base)) as cwd:
            done = research.run_bounded(claude_command(executable,
                    settings.agent_option('claude', 'model', 'reviewer')[0],
                    settings.agent_option('claude', 'effort', 'reviewer')[0]), prompt, cwd,
                    research.child_environment(config_home))
            if done.state != 'ok' or done.returncode != 0:
                raise WikiError('Claude sync reviewer failed; no report was recorded')
            return claude_answer(done.stdout)
    return run


def _codex_backend(wiki, runtime_base):
    executable = shutil.which('codex')
    if not executable:
        raise WikiError('codex is not on PATH')
    home = codex_agent.helper_home(runtime_base, 'sync-reviewer')
    instructions = home / 'sync-review-prompt.md'
    instructions.write_text(PROMPT, encoding='utf-8')
    instructions.chmod(0o600)
    schema = home / 'sync-review-schema.json'
    schema.write_text(json.dumps(SCHEMA), encoding='utf-8')
    schema.chmod(0o600)
    main = codex_agent.helper_home(runtime_base)
    def run(request):
        from . import research
        prompt = json.dumps(request, ensure_ascii=False)
        if len(prompt.encode('utf-8')) > MAX_PACKAGE:
            raise WikiError('sync review request is too large')
        with tempfile.TemporaryDirectory(prefix='pisar-sync-review-', dir=research.scratch_base(wiki.root, runtime_base)) as cwd:
            argv = codex_command(executable, home, cwd)
            done = research.run_bounded(argv, prompt, cwd,
                                        codex_agent.exec_environment(main, main / codex_agent.HOME))
            if done.state != 'ok' or done.returncode != 0:
                raise WikiError('Codex sync reviewer failed; no report was recorded')
            answer = Path(cwd) / 'answer.json'
            if not answer.is_file() or answer.is_symlink():
                raise WikiError('Codex sync reviewer returned no structured report')
            if answer.stat().st_size > MAX_TEXT:
                raise WikiError('Codex sync reviewer report is too large')
            return codex_answer(answer.read_bytes())
    return run


def record_feedback(wiki, state_dir, ident, rating, category=None):
    """Store outcome metadata only; never persist excerpts, prompts or document text."""
    from datetime import datetime, timezone
    if rating not in ('correct', 'corrected', 'rejected'):
        raise WikiError('sync feedback rating must be correct, corrected or rejected')
    if category is not None and (not isinstance(category, str) or not re.fullmatch(r'[a-z0-9_-]{1,40}', category)):
        raise WikiError('sync feedback category must be a short lowercase identifier')
    runtime = operations.Runtime(state_dir, wiki.root)
    with runtime.lock():
        from . import sync
        operation = sync._load(runtime, ident)
        review_data = operation.get('semantic_review')
        if not review_data or review_data.get('coverage') != 'all':
            raise WikiError('feedback requires a completed semantic review')
        report_path = runtime.path / 'sync-reports' / ident / f'{review_data["revision"]}.json'
        if review_data.get('report_path') != str(report_path) or report_path.is_symlink():
            raise WikiError('stored sync review report path is invalid')
        reports = json.loads(report_path.read_text(encoding='utf-8'))
        record = {'operation_id': ident, 'candidate_revision': review_data['revision'],
                  'policy': review_data['policy'],
                  'review_kind': operation.get('kind', 'integration'),
                  'reviewers': [{'model': item['model'], 'prompt_version': item['prompt_version'],
                                 'confidence': item['confidence']} for item in reports],
                  'resolver': (operation.get('resolver_result') or {}).get('resolver_identity'),
                  'resolver_proposal_revision': (operation.get('resolver_result') or {}).get('proposal_revision'),
                  'rating': rating, 'category': category,
                  'recorded_at': datetime.now(timezone.utc).isoformat()}
        directory = runtime.path / 'feedback'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f'{ident}-{review_data["revision"]}.json'
        operations.atomic_bytes(path, (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode())
        return record


def feedback_summary(wiki, state_dir):
    """Aggregate only local labels and model metadata; never expose report content."""
    runtime = operations.Runtime(state_dir, wiki.root)
    feedback_dir = runtime.path / 'feedback'
    ratings = {}
    if feedback_dir.is_dir():
        for path in feedback_dir.glob('*.json'):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                item = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if item.get('rating') in ('correct', 'corrected', 'rejected'):
                ratings[(item.get('operation_id'), 'reviewer', item.get('candidate_revision'))] = item['rating']
                proposal_revision = item.get('resolver_proposal_revision')
                if isinstance(proposal_revision, str):
                    ratings[(item.get('operation_id'), 'resolver', proposal_revision)] = item['rating']
    groups = {}
    sync_dir = runtime.path / 'sync'
    if sync_dir.is_dir():
        for path in sync_dir.glob('*.json'):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                operation = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            operation_id = operation.get('operation_id', '')
            operation_kind = str(operation.get('kind', 'integration'))
            report_dir = runtime.path / 'sync-reports' / operation_id
            evaluated = []
            if report_dir.is_dir():
                for report_path in sorted(report_dir.glob('*.json')):
                    if report_path.is_symlink() or not report_path.is_file():
                        continue
                    try:
                        reports = json.loads(report_path.read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        continue
                    if isinstance(reports, list):
                        evaluated.extend((report, 'reviewer', operation_kind,
                                          ratings.get((operation_id, 'reviewer', report_path.stem)))
                                         for report in reports if isinstance(report, dict))
            resolver_dir = runtime.path / 'sync-resolver-reports' / operation_id
            if resolver_dir.is_dir():
                recorded_revisions = set(operation.get('resolver_proposal_revisions', []))
                current_proposal = operation.get('resolver_proposal') or {}
                completed_proposal = operation.get('resolver_result') or {}
                recorded_revisions.update(revision for revision in
                                          (current_proposal.get('revision'),
                                           completed_proposal.get('proposal_revision')) if revision)
                for proposal_path in sorted(resolver_dir.glob('*.json')):
                    if (proposal_path.stem not in recorded_revisions or proposal_path.is_symlink()
                            or not proposal_path.is_file()):
                        continue
                    try:
                        proposal = json.loads(proposal_path.read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        continue
                    identity = proposal.get('resolver_identity', {})
                    if isinstance(identity, dict):
                        evaluated.extend((report, 'resolver', operation_kind,
                                          ratings.get((operation_id, 'resolver', proposal_path.stem)), identity)
                                         for report in proposal.get('reports', []) if isinstance(report, dict))
            for item in evaluated:
                report, role, operation_kind, rating = item[:4]
                confidence = report.get('confidence')
                if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    continue
                band = ('0.00-0.49' if confidence < .5 else '0.50-0.74' if confidence < .75
                        else '0.75-0.89' if confidence < .9 else '0.90-0.94' if confidence < .95 else '0.95-1.00')
                identity = item[4] if role == 'resolver' else report
                key = (str(identity.get('model', 'unknown')), str(identity.get('prompt_version', 'unknown')),
                       role, operation_kind, band)
                row = groups.setdefault(key, {'model': key[0], 'prompt_version': key[1],
                                              'review_kind': key[2], 'operation_kind': key[3],
                                              'confidence_band': band,
                                              'reviews': 0, 'correct': 0, 'corrected': 0,
                                              'rejected': 0, 'missing_label': 0})
                row['reviews'] += 1
                if rating is None:
                    row['missing_label'] += 1
                else:
                    row[rating] += 1
    result = []
    for key in sorted(groups):
        row = groups[key]
        labelled = row['correct'] + row['corrected'] + row['rejected']
        row['labelled'] = labelled
        row['corrected_or_rejected_fraction'] = ((row['corrected'] + row['rejected']) / labelled
                                                  if labelled else None)
        result.append(row)
    return {'groups': result, 'confidence_bands': ['0.00-0.49', '0.50-0.74', '0.75-0.89',
                                                    '0.90-0.94', '0.95-1.00'],
            'threshold_tuning': 'disabled'}
