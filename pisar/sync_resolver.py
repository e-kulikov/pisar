"""Host-enforced tools and approval gates for candidate-only semantic resolution."""
from pathlib import Path, PurePosixPath
from contextlib import contextmanager
from importlib.resources import files
import hashlib
import json
import math
import shutil
import stat
import tempfile

from . import codex_agent, operations, research, settings, sync_git
from .safety import WikiError
from .spaces import Wiki


TOOL_NAMES = ('candidate_context', 'candidate_write', 'candidate_stage', 'candidate_commit')
PROMPT_VERSION = 'sync-merge-v1'
PROMPT = files('pisar').joinpath('sync-merge-prompt.md').read_text(encoding='utf-8')
MAX_CONTEXT = 1_000_000
MAX_REPORT = 200_000
HARD_DISPOSITIONS = {'lost', 'uncertain', 'superseded'}
HARD_FINDINGS = {'omission', 'distortion', 'contradiction', 'provenance', 'task', 'context'}


@contextmanager
def _candidate_transaction(candidate, paths):
    """Restore candidate files and the exact Git index if a proposal fails midway."""
    root = Path(candidate)
    index = Path(sync_git.text(root, 'rev-parse', '--git-path', 'index').strip())
    if not index.is_absolute():
        index = root / index
    original_index = index.read_bytes()
    original_files = {}
    for name in paths:
        target = root.joinpath(*PurePosixPath(name).parts)
        if target.is_symlink():
            raise WikiError(f'resolver refuses symlink candidate path: {name}')
        original_files[name] = ((target.read_bytes(), stat.S_IMODE(target.stat().st_mode))
                                if target.is_file() else None)
    transaction = {'expected_index': original_index,
                   'expected_files': {name: _digest(value[0]) if value else None
                                      for name, value in original_files.items()}}
    try:
        yield transaction
    except BaseException:
        if index.read_bytes() == transaction['expected_index']:
            for name, value in original_files.items():
                target = root.joinpath(*PurePosixPath(name).parts)
                current = _digest(target.read_bytes()) if target.is_file() and not target.is_symlink() else None
                if current != transaction['expected_files'][name]:
                    continue
                if value is None:
                    if target.is_file() or target.is_symlink():
                        target.unlink()
                else:
                    data, mode = value
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    target.chmod(mode)
            index.write_bytes(original_index)
        raise


def resolver_identity():
    agent = settings.semantic_policy().get('agent', 'claude')
    model = settings.agent_option(agent, 'model', 'resolver')[0]
    effort = settings.agent_option(agent, 'effort', 'resolver')[0]
    return {'agent': agent, 'model': model or 'default', 'effort': effort or 'default',
            'prompt_version': PROMPT_VERSION, 'prompt_digest': operations.fingerprint(PROMPT)}


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class CandidateTools:
    """Four candidate-bound tools. The backend only receives these bound callables."""

    tool_names = TOOL_NAMES

    def __init__(self, candidate, *, domain, allowed_paths, expected_head, initial_staged=None,
                 initial_index=None):
        raw_repo = Path(candidate)
        if raw_repo.is_symlink():
            raise WikiError('resolver candidate must not be a symlink')
        self.repo = raw_repo.resolve()
        if sync_git.text(self.repo, 'rev-parse', '--show-toplevel').strip() != str(self.repo):
            raise WikiError('resolver candidate must be a Git repository root')
        self.domain = domain
        self.expected_head = expected_head
        self.expected_merge_head = sync_git.text(
            self.repo, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}', check=False).strip() or None
        self.allowed_paths = frozenset(allowed_paths)
        if not self.allowed_paths or not isinstance(domain, str) or not domain:
            raise WikiError('resolver requires a domain and a nonempty changed-path scope')
        for name in self.allowed_paths:
            path = PurePosixPath(name)
            if (path.is_absolute() or '..' in path.parts or not path.parts
                    or (domain not in ('*', 'workspace') and path.parts[0] != domain)):
                raise WikiError(f'resolver path is outside its domain: {name}')
        self._written = {}
        self._staged = set()
        self._authorized_report = None
        self._transaction_checkpoint = None
        self._observed = {}
        self._initial_staged = (set(initial_staged) if initial_staged is not None else
                                set(p.decode('utf-8', errors='surrogateescape') for p in
                                    sync_git.run(self.repo, 'diff', '--cached', '--name-only', '-z').split(b'\0') if p))
        self._initial_index = ({name: [tuple(entry) for entry in entries]
                                for name, entries in initial_index.items()} if initial_index is not None
                               else self._index_entries(self._initial_staged))
        for name in self.allowed_paths:
            path = self._path(name)
            self._observed[name] = _digest(path.read_bytes()) if path.is_file() else None

    def _fresh(self):
        if sync_git.head(self.repo) != self.expected_head:
            raise WikiError('resolver candidate is stale')
        if sync_git.text(self.repo, 'rev-parse', '--is-inside-work-tree').strip() != 'true':
            raise WikiError('resolver candidate is not a Git worktree')
        merge_head = sync_git.text(self.repo, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}', check=False).strip() or None
        if merge_head != self.expected_merge_head:
            raise WikiError('resolver merge parents changed while the resolver was running')
        for name, old_digest in self._observed.items():
            path = self._path(name)
            current = _digest(path.read_bytes()) if path.is_file() else None
            if current != old_digest:
                raise WikiError(f'resolver candidate is stale: {name} changed outside its write tool')

    def _path(self, name):
        if not isinstance(name, str) or name not in self.allowed_paths:
            raise WikiError(f'resolver path is outside its bound changed-path scope: {name}')
        relative = PurePosixPath(name)
        if ('.ruwana' in relative.parts or 'sources' in relative.parts or '.git' in relative.parts
                or '.gitmodules' in relative.parts
                or relative.name in ('.wiki.toml', '.domain.toml')):
            raise WikiError(f'resolver cannot synthesize task, source, wiki or Git topology metadata: {name}')
        path = self.repo.joinpath(*relative.parts)
        mode = sync_git.text(self.repo, 'ls-files', '-s', '--', name).split()
        if mode and mode[0] == '160000':
            raise WikiError(f'resolver cannot synthesize a submodule gitlink: {name}')
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != self.repo) \
                or not path.resolve().is_relative_to(self.repo):
            raise WikiError(f'resolver refuses symlink or escaping path: {name}')
        return path

    def _index_entries(self, paths):
        entries = {}
        if not paths:
            return entries
        raw = sync_git.run(self.repo, 'ls-files', '--stage', '-z', '--', *sorted(paths))
        for record in raw.split(b'\0'):
            if not record:
                continue
            try:
                metadata, encoded_path = record.split(b'\t', 1)
                mode, oid, stage = metadata.decode('ascii').split()
                name = encoded_path.decode('utf-8', errors='surrogateescape')
            except (ValueError, UnicodeDecodeError):
                raise WikiError('Git returned malformed resolver index entries') from None
            entries.setdefault(name, []).append((mode, oid, stage))
        return entries

    def _verify_index(self):
        expected_paths = self._initial_staged | set(self._written)
        entries = self._index_entries(expected_paths)
        for name in expected_paths:
            actual = entries.get(name, [])
            if name not in self._written:
                if actual != self._initial_index.get(name, []):
                    raise WikiError(f'resolver changed an inherited staged index entry: {name}')
                continue
            if len(actual) != 1 or actual[0][2] != '0' or actual[0][0] not in ('100644', '100755'):
                raise WikiError(f'resolver output is absent or unresolved in the Git index: {name}')
            blob = sync_git.run(self.repo, 'cat-file', 'blob', actual[0][1])
            if _digest(blob) != self._written[name]:
                raise WikiError(f'resolver Git index blob differs from accepted output: {name}')

    def candidate_context(self):
        self._fresh()
        files = {}
        size = 0
        for name in sorted(self.allowed_paths):
            path = self._path(name)
            if not path.is_file():
                files[name] = None
                continue
            data = path.read_bytes()
            size += len(data)
            if size > MAX_CONTEXT:
                raise WikiError('resolver context exceeds the size limit')
            try:
                files[name] = data.decode('utf-8')
            except UnicodeDecodeError:
                raise WikiError(f'resolver cannot edit non-text file: {name}') from None
        return {'domain': self.domain, 'candidate_commit': self.expected_head, 'files': files}

    def candidate_write(self, path, expected_sha256, text):
        self._fresh()
        target = self._path(path)
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise WikiError('candidate_write requires expected_sha256 of current bytes')
        if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_CONTEXT:
            raise WikiError('candidate_write text must be bounded UTF-8 text')
        before = target.read_bytes() if target.is_file() else b''
        if _digest(before) != expected_sha256:
            raise WikiError(f'expected_sha256 does not match current candidate bytes for {path}')
        encoded = text.encode('utf-8')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        self._written[path] = _digest(encoded)
        self._observed[path] = self._written[path]
        self._staged.discard(path)
        self._authorized_report = None
        if self._transaction_checkpoint:
            self._transaction_checkpoint(self, (path,))
        return {'path': path, 'sha256': self._written[path]}

    def candidate_stage(self, paths):
        self._fresh()
        if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)):
            raise WikiError('candidate_stage requires a nonempty unique path list')
        if set(paths) != set(self._written):
            raise WikiError('candidate_stage may stage only every path written by this resolver')
        for name in paths:
            target = self._path(name)
            actual = target.read_bytes() if target.is_file() else b''
            if _digest(actual) != self._written[name]:
                raise WikiError(f'candidate bytes changed before staging: {name}')
        sync_git.text(self.repo, 'add', '--', *paths)
        staged = set(p.decode('utf-8', errors='surrogateescape') for p in
                     sync_git.run(self.repo, 'diff', '--cached', '--name-only', '-z').split(b'\0') if p)
        if staged != self._initial_staged | set(paths):
            raise WikiError('candidate stage contains paths outside the resolver write set')
        self._staged = set(paths)
        if self._transaction_checkpoint:
            self._transaction_checkpoint(self, paths)
        self._verify_index()
        self._authorized_report = None
        return {'staged': sorted(self._staged)}

    def authorize_commit(self, report):
        """Host-only approval hook; this is deliberately absent from tool_names."""
        self._fresh()
        if not isinstance(report, dict) or len(_canonical(report).encode()) > MAX_REPORT:
            raise WikiError('resolver report must be bounded JSON')
        if not self._written or self._staged != set(self._written):
            raise WikiError('no accepted resolution is ready to commit')
        staged_now = set(p.decode('utf-8', errors='surrogateescape') for p in
                         sync_git.run(self.repo, 'diff', '--cached', '--name-only', '-z').split(b'\0') if p)
        if staged_now != self._initial_staged | self._staged:
            raise WikiError('candidate staged paths differ from the resolver write set')
        self._verify_index()
        changed = set(p.decode('utf-8', errors='surrogateescape') for p in
                      sync_git.run(self.repo, 'diff', '--name-only', '-z', 'HEAD').split(b'\0') if p)
        if changed - self.allowed_paths:
            raise WikiError('candidate has changes outside the resolver scope')
        for name, digest in self._written.items():
            target = self._path(name)
            if not target.is_file() or _digest(target.read_bytes()) != digest:
                raise WikiError(f'candidate staged bytes changed after resolver write: {name}')
        self._authorized_report = _digest(_canonical(report).encode())

    def candidate_commit(self, report):
        self._fresh()
        if not self._written or self._staged != set(self._written):
            raise WikiError('candidate write set must be fully staged before commit')
        if not self._authorized_report or _digest(_canonical(report).encode()) != self._authorized_report:
            raise WikiError('no accepted resolution is authorized for candidate commit')
        self.authorize_commit(report)
        if sync_git.text(self.repo, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}', check=False).strip():
            commit = sync_git.commit_merge(self.repo, message=report.get('summary', 'Resolve candidate conflict'))
        else:
            sync_git.text(self.repo, 'diff', '--cached', '--check')
            sync_git.text(self.repo, 'commit', '--no-gpg-sign', '-m', report.get('summary', 'Resolve candidate'))
            commit = sync_git.head(self.repo)
        self.expected_head = commit
        self._written.clear()
        self._staged.clear()
        self._authorized_report = None
        return {'commit': commit, 'tree': sync_git.text(self.repo, 'rev-parse', f'{commit}^{{tree}}').strip()}


def acceptance_decision(review_bundle, policy, human_decision=None):
    """Return accepted only after technical gates and, for auto, every semantic hard gate."""
    if policy.get('approval') not in ('manual', 'auto'):
        raise WikiError('sync approval policy must be manual or auto')
    threshold = policy.get('min_confidence')
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise WikiError('sync minimum confidence must be finite and from 0 to 1')
    if not review_bundle.get('technical_checks_passed') or not review_bundle.get('candidate_current'):
        return 'pending'
    if not review_bundle.get('resolver_commit_valid') or not review_bundle.get('fresh_review'):
        return 'pending'
    if policy['approval'] == 'manual':
        return 'accepted' if isinstance(human_decision, str) and human_decision.strip() else 'pending'
    reports = review_bundle.get('reports')
    if not isinstance(reports, list) or not reports:
        return 'pending'
    for report in reports:
        if (not report.get('context_complete') or report.get('verdict') != 'clear'
                or isinstance(report.get('confidence'), bool)
                or not isinstance(report.get('confidence'), (int, float))
                or not math.isfinite(report['confidence']) or report['confidence'] < threshold
                or report.get('findings') or any(item.get('disposition') in HARD_DISPOSITIONS
                                                 for item in report.get('statements', []))):
            return 'pending'
    return 'accepted'


def resolve_proposal(candidate, domain, allowed_paths, backend, *, expected_head, supplemental_context=None,
                     initial_staged=None, initial_index=None, transaction_checkpoint=None):
    """Run one bounded resolver against an immutable candidate revision.

    BACKEND receives only the scoped context and the four candidate tools. A report
    is returned for host policy and owner review; this function never commits.
    """
    tools = CandidateTools(candidate, domain=domain, allowed_paths=allowed_paths,
                           expected_head=expected_head, initial_staged=initial_staged,
                           initial_index=initial_index)
    tools._transaction_checkpoint = transaction_checkpoint
    context = tools.candidate_context()
    if supplemental_context:
        if supplemental_context.get('context_complete') is False:
            raise WikiError('resolver context is incomplete; no candidate write can be committed')
        context['pinned_context'] = supplemental_context
    response = backend(context, tools)
    if isinstance(response, dict) and set(response) == {'tool_calls', 'report'}:
        report = execute_plan(response, tools, context)
    else:
        # Test adapters can exercise the same host tools directly.
        report = response(context, tools) if callable(response) else response
    if (not isinstance(report, dict) or not isinstance(report.get('summary'), str)
            or not report['summary'].strip() or len(report['summary']) > 4000):
        raise WikiError('resolver returned no bounded resolution report')
    confidence = report.get('confidence')
    statements = report.get('statements')
    findings = report.get('findings')
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise WikiError('resolver confidence must be finite and between zero and one')
    if report.get('context_complete') is not True:
        raise WikiError('resolver context is incomplete; no candidate write can be committed')
    if (not isinstance(statements, list) or len(statements) > 500
            or not isinstance(findings, list) or len(findings) > 100):
        raise WikiError('resolver report requires statement accounting and findings arrays')
    statement_ids = set()
    for item in statements:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or len(item['id']) > 200 \
                or item['id'] in statement_ids or item.get('disposition') not in (
                    'preserved', 'equivalent', 'superseded', 'lost', 'uncertain'):
            raise WikiError('resolver statement accounting is malformed or duplicated')
        statement_ids.add(item['id'])
    hard_statement = any(item['disposition'] in HARD_DISPOSITIONS for item in statements)
    context_corpus = json.dumps(context, ensure_ascii=False)
    for item in findings:
        if (not isinstance(item, dict) or item.get('category') not in HARD_FINDINGS
                or not isinstance(item.get('id'), str) or not item['id']
                or not isinstance(item.get('paths'), list) or not item['paths']
                or not set(item['paths']) <= tools.allowed_paths
                or not isinstance(item.get('evidence_quotes'), list)
                or not all(isinstance(quote, str) and quote and quote in context_corpus
                           for quote in item.get('evidence_quotes', []))
                or not isinstance(item.get('explanation'), str) or not isinstance(item.get('question'), str)):
            raise WikiError('resolver finding schema/category is invalid')
    if hard_statement and not findings:
        raise WikiError('resolver loss or uncertainty requires a hard finding')
    if not tools._written or tools._staged != set(tools._written):
        raise WikiError('resolver must write and stage its complete declared resolution')
    mappings = report.get('mappings')
    if not isinstance(mappings, list) or len(mappings) != len(tools._written):
        raise WikiError('resolver report must map every written output path')
    mapped = {}
    for item in mappings:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256', 'statement_ids'}:
            raise WikiError('resolver report mapping has an invalid schema')
        path = item['path']
        if path in mapped or path not in tools._written or item['sha256'] != tools._written[path]:
            raise WikiError('resolver report mapping does not match actual candidate output')
        if not isinstance(item['statement_ids'], list) or not item['statement_ids']:
            raise WikiError('resolver output mapping requires statement accounting')
        if (not all(isinstance(ident, str) for ident in item['statement_ids'])
                or not set(item['statement_ids']) <= statement_ids):
            raise WikiError('resolver output mapping references unknown statements')
        mapped[path] = item
    if set(mapped) != set(tools._written):
        raise WikiError('resolver report omits a written candidate path')
    report['candidate_revision'] = expected_head
    return {'report': report, 'tools': tools, 'candidate': str(candidate),
            'written_paths': sorted(tools._written)}


def _conflict_context(wiki, candidate, operation, domain, paths):
    """Package the pinned merge sides and available same-space text for a root conflict."""
    from . import sync_review
    bases = sync_git.text(candidate, 'merge-base', '--all', operation['ours'], operation['theirs'],
                          check=False).splitlines()
    base = bases[0] if bases else None
    versions = {}
    for path in sorted(paths):
        versions[path] = {side: sync_review._read_blob(candidate, commit, path) if commit else None
                          for side, commit in (('base', base), ('ours', operation['ours']),
                                               ('theirs', operation['theirs']))}
        working = Path(candidate) / path
        try:
            versions[path]['candidate'] = working.read_bytes().decode('utf-8') if working.is_file() else None
        except UnicodeDecodeError:
            raise WikiError(f'resolver refuses non-text conflict input: {path}') from None
    candidate_wiki = Wiki(candidate)
    spaces = set()
    for path in paths:
        owner = candidate_wiki.owner(Path(candidate) / path)
        if owner is not None:
            spaces.add(owner)
    context = {}
    total = 0
    for space in spaces:
        for path in candidate_wiki.files():
            if not path.is_relative_to(space.path):
                continue
            relative = path.relative_to(candidate).as_posix()
            if relative in paths or path.suffix.lower() not in (
                    '.md', '.txt', '.rst', '.csv', '.tsv', '.toml', '.json', '.yaml', '.yml'):
                continue
            data = path.read_bytes()
            try:
                content = data.decode('utf-8')
            except UnicodeDecodeError:
                raise WikiError(f'resolver same-domain context is not UTF-8 text: {relative}') from None
            total += len(data)
            if total > MAX_CONTEXT:
                raise WikiError('resolver same-domain context exceeds the size limit')
            context[relative] = content
    return {'domain': domain, 'base': base, 'versions': versions, 'context': context,
            'context_complete': True}


def resolve_operation(wiki, state_dir, ident, *, backend=None, revision=None, decision=None,
                      review_backend=None):
    """Create a candidate resolution proposal, or approve and commit a stored proposal."""
    from datetime import datetime, timezone
    from . import sync, sync_review
    runtime = operations.Runtime(state_dir, wiki.root)
    if revision is not None:
        with runtime.lock():
            operation = sync._load(runtime, ident)
            proposal = operation.get('resolver_proposal')
            if operation.get('status') != 'resolution-pending' or not proposal:
                raise WikiError('operation has no pending resolver proposal')
            if revision != proposal.get('revision') or proposal.get('revision') != operations.fingerprint(
                    {key: proposal[key] for key in ('candidate_head', 'merge_head', 'reports', 'written', 'policy',
                                                    'allowed_paths', 'resolver_identity', 'initial_staged',
                                                    'initial_index')}):
                raise WikiError('resolver proposal revision is stale or invalid')
            if proposal.get('policy') != sync_review.policy().__dict__:
                raise WikiError('resolver policy changed after proposal; run resolution again')
            if proposal.get('resolver_identity') != resolver_identity():
                raise WikiError('resolver identity changed after proposal; run resolution again')
            candidate = sync._candidate(runtime, ident)
            if sync_git.head(candidate) != proposal['candidate_head']:
                raise WikiError('resolver candidate changed after proposal')
            current_merge_head = sync_git.text(
                candidate, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}', check=False).strip() or None
            if current_merge_head != proposal.get('merge_head'):
                raise WikiError('resolver merge parents changed after proposal')
            tool = CandidateTools(candidate, domain='*', allowed_paths=set(proposal['allowed_paths']),
                                  expected_head=proposal['candidate_head'],
                                  initial_staged=proposal['initial_staged'],
                                  initial_index=proposal['initial_index'])
            tool._written = dict(proposal['written'])
            tool._staged = set(proposal['written'])
            report = {'summary': 'Resolve semantic candidate', 'confidence': min(
                (item.get('confidence', 0) for item in proposal['reports']), default=0),
                'context_complete': all(item.get('context_complete') is True for item in proposal['reports']),
                'statements': [statement for item in proposal['reports'] for statement in item.get('statements', [])],
                'findings': [finding for item in proposal['reports'] for finding in item.get('findings', [])],
                'mappings': [mapping for item in proposal['reports'] for mapping in item['mappings']]}
            for path, digest in tool._written.items():
                target = tool._path(path)
                actual = _digest(target.read_bytes()) if target.is_file() else None
                if actual != digest:
                    raise WikiError(f'resolver candidate changed after proposal: {path}')
            staged = set(p.decode('utf-8', errors='surrogateescape') for p in
                         sync_git.run(candidate, 'diff', '--cached', '--name-only', '-z').split(b'\0') if p)
            if staged != set(proposal['initial_staged']) | set(proposal['written']):
                raise WikiError('resolver staged paths changed after proposal')
            changed = set(p.decode('utf-8', errors='surrogateescape') for p in
                          sync_git.run(candidate, 'diff', '--name-only', '-z', 'HEAD').split(b'\0') if p)
            untracked = set(p.decode('utf-8', errors='surrogateescape') for p in
                            sync_git.run(candidate, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0') if p)
            if (changed | untracked) - set(proposal['allowed_paths']):
                raise WikiError('candidate has changes outside the resolver proposal scope')
            policy = sync_review.policy().__dict__
            if policy['approval'] == 'manual' and (not isinstance(decision, str) or not decision.strip()):
                raise WikiError('resolver commit requires a concrete owner decision')
            if policy['approval'] == 'auto':
                if (report['confidence'] < policy['min_confidence'] or report['findings']
                        or any(item.get('disposition') in HARD_DISPOSITIONS for item in report['statements'])):
                    raise WikiError('resolver report does not satisfy automatic-resolution gates')
            from .documents import check as check_documents
            structural = check_documents(Wiki(candidate))
            if not structural['ok']:
                raise WikiError('candidate structural check failed: ' + '; '.join(structural['errors']))
            staged_tree = sync_git.text(candidate, 'write-tree').strip()
            sync._task_changes(candidate, operation['ours'], operation['theirs'], staged_tree)
            sync._validate_originals(candidate, operation['ours'], operation['theirs'], staged_tree)
            sync._validate_processing(candidate, staged_tree)
            operation['candidate'] = proposal['candidate_head']
            operation['status'] = 'resolving'
            tool.authorize_commit(report)
            committed = tool.candidate_commit(report)
            operation['resolver_result'] = {'commit': committed['commit'], 'tree': committed['tree'],
                                            'proposal_revision': revision,
                                            'resolver_identity': proposal['resolver_identity'],
                                            'decision': decision.strip() if isinstance(decision, str) else None,
                                            'recorded_at': datetime.now(timezone.utc).isoformat()}
            operation['status'] = 'prepared'
            operation['review'] = None
            operation['accepted'] = None
            operation['semantic_review'] = None
            sync._save(runtime, operation)
        # Re-run all Git, task, provenance and complete-wiki checks on the committed result.
        prepared = sync.prepare(wiki, state_dir, ident, merge=True)
        if policy['approval'] == 'auto':
            reviewed = sync.semantic_review(wiki, state_dir, ident, backend=review_backend)
            from . import sync_resolver as resolver_module
            if (reviewed.get('status') != 'reviewed' or resolver_module.acceptance_decision(
                    {'reports': reviewed.get('reports', []), 'technical_checks_passed': True,
                     'candidate_current': True, 'resolver_commit_valid': True, 'fresh_review': True},
                    reviewed.get('policy', {})) != 'accepted'):
                return {**prepared, 'operation_id': ident, 'status': 'needs-decision',
                        'semantic_review': reviewed}
            sync.accept(wiki, state_dir, ident, reviewed['revision'])
            applied = sync.apply(wiki, state_dir, ident)
            return {**prepared, **applied, 'operation_id': ident, 'status': 'applied-automatically',
                    'semantic_review': reviewed}
        return prepared

    with runtime.lock():
        operation = sync._load(runtime, ident)
        if operation.get('status') == 'resolution-pending':
            raise WikiError('operation already has a pending resolver proposal; inspect and approve its exact revision')
        if operation.get('status') not in ('fetched', 'conflict', 'prepared', 'reviewed'):
            raise WikiError('resolver requires a fetched conflict or sealed prepared candidate')
        candidate = sync._candidate(runtime, ident)
        if not candidate.is_dir():
            raise WikiError('resolver candidate is not prepared')
        head = sync_git.head(candidate)
        unmerged = sync_git.run(candidate, 'ls-files', '--unmerged', '-z')
        if unmerged:
            if head != (operation.get('candidate') or operation.get('ours')):
                raise WikiError('conflict candidate commit changed before resolution')
            merge_head = sync_git.text(candidate, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}',
                                       check=False).strip()
            if merge_head != operation.get('theirs'):
                raise WikiError('conflict candidate merge parent changed before resolution')
            paths = set()
            for record in unmerged.decode('utf-8', errors='surrogateescape').split('\0'):
                if record:
                    paths.add(record.split('\t', 1)[-1])
            changed = set(p.decode('utf-8', errors='surrogateescape') for p in
                          sync_git.run(candidate, 'diff', '--name-only', '-z', 'HEAD').split(b'\0') if p)
            allowed = paths | changed
            if any('.ruwana' in Path(path).parts or 'sources' in Path(path).parts for path in allowed):
                raise WikiError('resolver refuses source or task conflicts')
            path_domains = sync_review.pinned_domains(candidate, operation, allowed)
            operation['candidate'] = head
        else:
            semantic = operation.get('semantic_review') or {}
            if not semantic or semantic.get('coverage') != 'all' or semantic.get('verdict') == 'clear':
                raise WikiError('resolver requires a current semantic finding or Git conflict')
            sync._assert_candidate(operation, candidate)
            requests, reports = sync_review.checked_stored_reports(wiki, runtime, operation, candidate)
            allowed = {path for item in reports for finding in item.get('findings', [])
                       for path in finding.get('paths', [])}
            if not allowed:
                raise WikiError('semantic report has no resolvable path')
            path_domains = {path: request['domain'] for request in requests
                            for path in request['changed_paths']}
            if allowed - set(path_domains):
                raise WikiError('semantic findings reference paths outside the checked candidate report')
            operation['candidate'] = head
        policy = sync_review.policy().__dict__
        initial_staged = sorted(p.decode('utf-8', errors='surrogateescape') for p in
                                sync_git.run(candidate, 'diff', '--cached', '--name-only', '-z').split(b'\0') if p)
        index_probe = CandidateTools(candidate, domain='*', allowed_paths=allowed, expected_head=head,
                                     initial_staged=set(initial_staged))
        initial_index = {name: [list(entry) for entry in entries]
                         for name, entries in index_probe._initial_index.items()}
        groups = {}
        for path in allowed:
            domain = path_domains[path]
            groups.setdefault(domain, set()).add(path)
        resolver = backend or _configured_backend(wiki, runtime.base)
        def make_proposal(transaction):
            reports, written = [], {}
            session_staged = set(initial_staged)
            session_index = initial_index

            def checkpoint(tools, paths):
                transaction['expected_files'].update({name: tools._written.get(name)
                                                      for name in paths})
                index_path = Path(sync_git.text(candidate, 'rev-parse', '--git-path', 'index').strip())
                if not index_path.is_absolute():
                    index_path = candidate / index_path
                transaction['expected_index'] = index_path.read_bytes()

            for domain, paths in sorted(groups.items()):
                supplementary = (next((item for item in requests if item['domain'] == domain), None)
                                 if not unmerged else _conflict_context(wiki, candidate, operation, domain, paths))
                result = resolve_proposal(candidate, domain, paths, resolver, expected_head=head,
                                          supplemental_context=supplementary,
                                          initial_staged=session_staged, initial_index=session_index,
                                          transaction_checkpoint=checkpoint)
                result_tools = result['tools']
                result_tools._fresh()
                result_tools._verify_index()
                report = result['report']
                reports.append(report)
                for mapping in report['mappings']:
                    written[mapping['path']] = mapping['sha256']
                transaction['expected_files'].update(result_tools._written)
                index_path = Path(sync_git.text(candidate, 'rev-parse', '--git-path', 'index').strip())
                if not index_path.is_absolute():
                    index_path = candidate / index_path
                transaction['expected_index'] = index_path.read_bytes()
                session_staged.update(result_tools._written)
                session_index = {name: [list(entry) for entry in entries]
                                 for name, entries in result_tools._index_entries(session_staged).items()}
            if not written:
                raise WikiError('resolver did not produce any candidate writes')
            merge_head = sync_git.text(candidate, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}',
                                       check=False).strip() or None
            revision_value = {'candidate_head': head, 'merge_head': merge_head, 'reports': reports,
                              'written': written, 'policy': policy, 'allowed_paths': sorted(allowed),
                              'resolver_identity': resolver_identity(), 'initial_staged': initial_staged,
                              'initial_index': initial_index}
            proposal_value = {**revision_value, 'initial_staged': initial_staged,
                              'revision': operations.fingerprint(revision_value),
                              'recorded_at': datetime.now(timezone.utc).isoformat()}
            proposal_dir = runtime.path / 'sync-resolver-reports' / ident
            proposal_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            proposal_path = proposal_dir / f'{proposal_value["revision"]}.json'
            if proposal_path.is_symlink():
                raise WikiError('stored resolver proposal path is unsafe')
            operations.atomic_bytes(proposal_path,
                                    (json.dumps(proposal_value, ensure_ascii=False, indent=2) + '\n').encode())
            operation['candidate'] = head
            operation['resolver_proposal'] = proposal_value
            revisions = operation.setdefault('resolver_proposal_revisions', [])
            if proposal_value['revision'] not in revisions:
                revisions.append(proposal_value['revision'])
            operation['status'] = 'resolution-pending'
            operation['review'] = None
            operation['accepted'] = None
            operation['semantic_review'] = None
            sync._save(runtime, operation)
            response_value = {'operation_id': ident, 'status': 'resolution-pending',
                              'revision': proposal_value['revision'], 'domains': sorted(groups),
                              'written_paths': sorted(written), 'reports': reports,
                              'approval': policy['approval']}
            return proposal_value, response_value

        with _candidate_transaction(candidate, allowed) as transaction:
            proposal, response = make_proposal(transaction)
    if policy['approval'] == 'auto':
        return resolve_operation(wiki, state_dir, ident, backend=backend, revision=proposal['revision'],
                                 review_backend=review_backend)
    return response


def execute_plan(response, tools, context=None):
    """Execute a bounded JSON tool plan through the same host checks as live tools."""
    if not isinstance(response, dict) or set(response) != {'tool_calls', 'report'}:
        raise WikiError('resolver response has an invalid tool plan')
    calls, report = response['tool_calls'], response['report']
    if not isinstance(calls, list) or not 1 <= len(calls) <= 40:
        raise WikiError('resolver tool plan must contain a bounded nonempty call list')
    writes, stage_paths = {}, None
    saw_commit = False
    for index, call in enumerate(calls):
        if not isinstance(call, dict) or set(call) != {'name', 'arguments'}:
            raise WikiError('resolver tool call has an invalid schema')
        name, args = call['name'], call['arguments']
        if not isinstance(args, dict):
            raise WikiError('resolver tool arguments must be an object')
        if name == 'candidate_context':
            if args or writes or stage_paths is not None:
                raise WikiError('candidate_context takes no arguments')
        elif name == 'candidate_write':
            if stage_paths is not None or saw_commit or set(args) != {'path', 'expected_sha256', 'text'}:
                raise WikiError('candidate_write must precede stage and commit with exact arguments')
            path = args['path']
            target = tools._path(path)
            current = target.read_bytes() if target.is_file() else b''
            if not isinstance(args['expected_sha256'], str) or _digest(current) != args['expected_sha256']:
                raise WikiError(f'expected_sha256 does not match current candidate bytes for {path}')
            if (not isinstance(args['text'], str) or len(args['text'].encode('utf-8')) > MAX_CONTEXT
                    or path in writes):
                raise WikiError('candidate_write text is oversized or path is duplicated')
            writes[path] = _digest(args['text'].encode('utf-8'))
        elif name == 'candidate_stage':
            if saw_commit or stage_paths is not None or set(args) != {'paths'}:
                raise WikiError('candidate_stage has invalid ordering or arguments')
            stage_paths = args['paths']
            if (not isinstance(stage_paths, list) or not all(isinstance(path, str) for path in stage_paths)
                    or len(stage_paths) != len(set(stage_paths)) or set(stage_paths) != set(writes)):
                raise WikiError('candidate_stage must stage every and only written path')
        elif name == 'candidate_commit':
            if index != len(calls) - 1 or saw_commit or set(args) != {'report'} or args['report'] != report:
                raise WikiError('candidate_commit must be the final call bound to the returned report')
            saw_commit = True
        else:
            raise WikiError(f'resolver requested unsupported tool: {name}')
    if not saw_commit:
        raise WikiError('resolver plan omitted candidate_commit')
    report = response['report']
    if (not isinstance(report, dict) or not isinstance(report.get('summary'), str)
            or not report['summary'].strip() or len(report['summary']) > 4000
            or isinstance(report.get('confidence'), bool)
            or not isinstance(report.get('confidence'), (int, float))
            or not math.isfinite(report['confidence']) or not 0 <= report['confidence'] <= 1
            or report.get('context_complete') is not True):
        raise WikiError('resolver report is invalid or declares incomplete context')
    statements = report.get('statements')
    mappings = report.get('mappings')
    findings = report.get('findings')
    if (not isinstance(statements, list) or len(statements) > 500 or not isinstance(mappings, list)
            or not isinstance(findings, list) or len(findings) > 100):
        raise WikiError('resolver report requires statements and mappings')
    if not all(isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']
               and len(item['id']) <= 200
               and item.get('disposition') in ('preserved', 'equivalent', 'superseded', 'lost', 'uncertain')
               for item in statements):
        raise WikiError('resolver statement accounting is invalid')
    statement_ids = {item['id'] for item in statements}
    if len(statement_ids) != len(statements):
        raise WikiError('resolver statement accounting is invalid')
    mapped = {}
    for item in mappings:
        if (not isinstance(item, dict) or set(item) != {'path', 'sha256', 'statement_ids'}
                or not isinstance(item['path'], str) or item['path'] in mapped
                or writes.get(item['path']) != item['sha256']
                or not isinstance(item['statement_ids'], list) or not item['statement_ids']
                or not all(isinstance(ident, str) for ident in item['statement_ids'])
                or not set(item['statement_ids']) <= statement_ids):
            raise WikiError('resolver output mapping does not match its proposed writes')
        mapped[item['path']] = item
    if set(mapped) != set(writes):
        raise WikiError('resolver output mapping omits a proposed write')
    for item in statements:
        if item['disposition'] in HARD_DISPOSITIONS and not findings:
            raise WikiError('resolver loss or uncertainty requires a hard finding')
    context_corpus = json.dumps(context or {}, ensure_ascii=False)
    for finding in findings:
        if (not isinstance(finding, dict) or finding.get('category') not in HARD_FINDINGS
                or not isinstance(finding.get('paths'), list) or not finding['paths']
                or not all(isinstance(path, str) and path in tools.allowed_paths for path in finding['paths'])
                or not isinstance(finding.get('evidence_quotes'), list)
                or not all(isinstance(quote, str) and quote and quote in context_corpus
                           for quote in finding['evidence_quotes'])):
            raise WikiError('resolver finding is invalid or outside the supplied context')
    tools._fresh()
    for call in calls:
        name, args = call['name'], call['arguments']
        if name == 'candidate_context':
            tools.candidate_context()
        elif name == 'candidate_write':
            tools.candidate_write(args['path'], args['expected_sha256'], args['text'])
        elif name == 'candidate_stage':
            tools.candidate_stage(args['paths'])
    return report


def schema():
    """Strict response schema shared by the two resolver subprocess profiles."""
    return {'type': 'object', 'additionalProperties': False, 'required': ['tool_calls', 'report'],
            'properties': {
                'tool_calls': {'type': 'array', 'minItems': 1, 'maxItems': 40, 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['name', 'arguments'],
                    'properties': {'name': {'type': 'string', 'enum': list(TOOL_NAMES)},
                                   'arguments': {'type': 'object'}}}},
                'report': {'type': 'object', 'additionalProperties': False,
                           'required': ['summary', 'confidence', 'context_complete', 'statements', 'findings', 'mappings'],
                           'properties': {'summary': {'type': 'string'},
                                          'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
                                          'context_complete': {'type': 'boolean'},
                                          'statements': {'type': 'array'}, 'findings': {'type': 'array'},
                                          'mappings': {'type': 'array'}}}}}


def codex_command(executable, home, cwd, answer, response_schema, instructions, model, effort):
    """Resolver helper uses Codex's read-only, tool-less profile; host executes validated calls."""
    return codex_agent.exec_argv(executable, cwd, answer, response_schema, instructions,
                                 model, effort, 'disabled')


def _claude_response(output):
    try:
        envelope = json.loads(output)
        result = envelope.get('structured_output')
        if isinstance(result, dict):
            return result
        if isinstance(envelope.get('result'), str):
            result = json.loads(envelope['result'])
            if isinstance(result, dict):
                return result
    except (ValueError, AttributeError):
        pass
    raise WikiError('Claude sync resolver returned no structured resolution')


def _configured_backend(wiki, runtime_base):
    agent = settings.semantic_policy()['agent']
    model = settings.agent_option(agent, 'model', 'resolver')[0]
    effort = settings.agent_option(agent, 'effort', 'resolver')[0]
    if agent == 'claude':
        executable = shutil.which('claude')
        if not executable:
            raise WikiError('claude is not on PATH')
        config_home = operations.outside_git(Path(runtime_base) / 'agents' / 'claude' / 'sync-resolver')
        config_home.mkdir(parents=True, exist_ok=True, mode=0o700)

        def run(context, tools=None):
            prompt = json.dumps({'context': context, 'tools': list(TOOL_NAMES)}, ensure_ascii=False)
            with tempfile.TemporaryDirectory(prefix='pisar-sync-resolver-',
                                             dir=research.scratch_base(wiki.root, runtime_base)) as cwd:
                argv = [executable, '-p', '--output-format', 'json', '--json-schema', json.dumps(schema()),
                        *(['--model', model] if model else []), *(['--effort', effort] if effort else []),
                        '--safe-mode', '--system-prompt', PROMPT, '--tools=', '--strict-mcp-config',
                        '--no-session-persistence']
                done = research.run_bounded(argv, prompt, cwd, research.child_environment(config_home))
                if done.state != 'ok' or done.returncode:
                    raise WikiError('Claude sync resolver failed; no candidate commit was made')
                return _claude_response(done.stdout)
        return run
    if agent == 'codex':
        executable = shutil.which('codex')
        if not executable:
            raise WikiError('codex is not on PATH')
        home = codex_agent.helper_home(runtime_base, 'sync-resolver')
        instructions = home / 'sync-merge-prompt.md'
        instructions.write_text(PROMPT, encoding='utf-8')
        instructions.chmod(0o600)
        response_schema = home / 'sync-merge-schema.json'
        response_schema.write_text(json.dumps(schema()), encoding='utf-8')
        response_schema.chmod(0o600)
        main = codex_agent.helper_home(runtime_base)

        def run(context, tools=None):
            prompt = json.dumps({'context': context, 'tools': list(TOOL_NAMES)}, ensure_ascii=False)
            with tempfile.TemporaryDirectory(prefix='pisar-sync-resolver-',
                                             dir=research.scratch_base(wiki.root, runtime_base)) as cwd:
                answer = Path(cwd) / 'answer.json'
                argv = codex_command(executable, home, cwd, answer, response_schema, instructions, model, effort)
                done = research.run_bounded(argv, prompt, cwd,
                                            codex_agent.exec_environment(main, main / codex_agent.HOME))
                if done.state != 'ok' or done.returncode or not answer.is_file() or answer.is_symlink():
                    raise WikiError('Codex sync resolver failed; no candidate commit was made')
                if answer.stat().st_size > 200_000:
                    raise WikiError('Codex sync resolver response is too large')
                try:
                    return json.loads(answer.read_text(encoding='utf-8'))
                except ValueError:
                    raise WikiError('Codex sync resolver returned invalid JSON') from None
        return run
    raise WikiError(f'unsupported sync resolver agent: {agent}')


def commit_proposal(proposal, policy, *, human_decision=None, validator=None):
    """Commit only an owner-approved manual proposal or a hard-gated auto proposal."""
    report = proposal['report']
    threshold = policy.get('min_confidence')
    if policy.get('approval') == 'manual':
        if not isinstance(human_decision, str) or not human_decision.strip():
            raise WikiError('resolver commit requires explicit owner approval')
    elif policy.get('approval') == 'auto':
        findings = report.get('findings', [])
        statements = report.get('statements', [])
        confidence = report.get('confidence')
        if (report.get('context_complete') is not True
                or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence) or confidence < threshold or findings
                or any(not isinstance(item, dict) or item.get('disposition') in HARD_DISPOSITIONS
                       for item in statements)):
            raise WikiError('resolver report does not satisfy automatic-resolution gates')
    else:
        raise WikiError('sync approval policy must be manual or auto')
    if validator is not None and validator(proposal) is not True:
        raise WikiError('resolver candidate failed host validation')
    tools = proposal['tools']
    tools.authorize_commit(report)
    return tools.candidate_commit(report)
