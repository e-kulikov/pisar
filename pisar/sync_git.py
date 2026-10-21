"""Native Git primitives for isolated, single-repository sync candidates."""
from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
import tempfile

from . import gitops
from .safety import WikiError


POLICY = ('-c', 'submodule.recurse=false', '-c', 'merge.autoStash=false')


@dataclass(frozen=True)
class RepoSnapshot:
    repo: Path
    branch: str
    head: str


def command(repo, *args):
    return ['git', '--no-pager', '--no-optional-locks', '-C', str(repo), *gitops.HARDENED, *POLICY, *args]


def run(repo, *args, check=True, input=None):
    """Run Git without a shell and with inherited Git redirection settings removed."""
    result = subprocess.run(command(repo, *args), input=input, capture_output=True,
                            env=gitops.environment())
    if check and result.returncode:
        detail = result.stderr.decode('utf-8', errors='replace').strip()
        raise WikiError(f'git {args[0]} failed in {repo}: {detail}')
    return result.stdout


def text(repo, *args, check=True):
    return run(repo, *args, check=check).decode('utf-8', errors='strict')


def head(repo):
    return text(repo, 'rev-parse', '--verify', 'HEAD^{commit}').strip()


def parents(repo, commit):
    fields = text(repo, 'rev-list', '--parents', '-n', '1', commit).split()
    return tuple(fields[1:])


def _git_path(repo, name):
    value = Path(text(repo, 'rev-parse', '--git-path', name).strip())
    return value if value.is_absolute() else Path(repo) / value


def index_tree(repo):
    """Ask Git for the staged tree without rewriting the candidate's index cache."""
    with tempfile.TemporaryDirectory(prefix='pisar-index-check-') as temporary:
        index = Path(temporary) / 'index'
        index.write_bytes(_git_path(repo, 'index').read_bytes())
        result = subprocess.run(command(repo, 'write-tree'), capture_output=True,
                                env={**gitops.environment(), 'GIT_INDEX_FILE': str(index)})
        if result.returncode:
            raise WikiError('git write-tree failed during candidate check')
        return result.stdout.decode('ascii').strip()


def observe(repo, *, allow_dirty=False):
    repo = Path(repo).resolve()
    top = Path(text(repo, 'rev-parse', '--show-toplevel').strip()).resolve()
    if top != repo:
        raise WikiError(f'sync target must be a Git repository root: {repo}')
    branch = text(repo, 'symbolic-ref', '--quiet', 'HEAD', check=False).strip()
    if not branch.startswith('refs/heads/'):
        raise WikiError('sync requires an attached local branch')
    current = head(repo)
    if not allow_dirty and text(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
                                 '--ignore-submodules=none'):
        raise WikiError('sync requires a clean repository')
    for record in filter(None, text(repo, 'ls-files', '-v', '-z').split('\0')):
        if record[:1].islower() or record.startswith('S '):
            raise WikiError('sync refuses assume-unchanged or skip-worktree index entries')
    if text(repo, 'rev-parse', '--is-shallow-repository').strip() == 'true':
        raise WikiError('sync requires complete repository history')
    sparse = text(repo, 'config', '--bool', '--get', 'core.sparseCheckout', check=False).strip()
    if sparse == 'true':
        raise WikiError('sync refuses sparse checkouts')
    for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
        path = _git_path(repo, marker)
        if path.exists():
            raise WikiError(f'sync refuses unfinished Git operation: {marker}')
    return RepoSnapshot(repo, branch, current)


def fetch_tip(repo, remote, branch):
    if not remote or remote.startswith('-'):
        raise WikiError('invalid sync remote name')
    if not branch or branch.startswith('-'):
        raise WikiError('invalid sync branch name')
    tracking = f'refs/remotes/{remote}/{branch}'
    for ref in (f'refs/heads/{branch}', tracking):
        if subprocess.run(command(repo, 'check-ref-format', ref), env=gitops.environment(),
                          capture_output=True).returncode:
            raise WikiError(f'invalid sync ref: {ref}')
    result = subprocess.run(command(repo, 'fetch', '--no-tags', '--no-recurse-submodules', remote,
                                    f'+refs/heads/{branch}:{tracking}'), capture_output=True,
                            env=gitops.environment())
    if result.returncode:
        raise WikiError(f'git fetch failed with exit status {result.returncode}; remote details omitted')
    return text(repo, 'rev-parse', '--verify', f'{tracking}^{{commit}}').strip()


def _ancestor(repo, ancestor, descendant):
    result = subprocess.run(command(repo, 'merge-base', '--is-ancestor', ancestor, descendant),
                            capture_output=True, env=gitops.environment())
    if result.returncode not in (0, 1):
        raise WikiError(f'git merge-base failed in {repo}: {result.stderr.decode(errors="replace").strip()}')
    return result.returncode == 0


def _identity(source, candidate):
    for key in ('user.name', 'user.email'):
        value = text(source, 'config', '--get', key, check=False).strip()
        if value:
            text(candidate, 'config', key, value)


def _candidate_attributes(repo, commit):
    paths = run(repo, 'ls-tree', '-r', '--name-only', '-z', commit)
    with tempfile.TemporaryDirectory(prefix='pisar-attributes-') as temporary:
        environment = {**gitops.environment(), 'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
        for args, data in ((('read-tree', commit), None),
                           (('check-attr', '--cached', '-z', '--stdin', 'merge', 'filter'), paths)):
            result = subprocess.run(command(repo, *args), input=data, capture_output=True,
                                    env=environment)
            if result.returncode:
                raise WikiError('candidate-compatibility: cannot inspect pinned Git attributes')
        fields = result.stdout.split(b'\0')
        return {(fields[i], fields[i + 1]): fields[i + 2]
                for i in range(0, len(fields) - 1, 3)}


def check_candidate_compatibility(source, candidate, ours, theirs):
    """Refuse local merge/checkout semantics that an ordinary clone cannot reproduce."""
    keys = {'merge.default', 'merge.renormalize', 'core.autocrlf', 'core.eol'}
    commits = {ours, theirs, *text(source, 'merge-base', '--all', ours, theirs, check=False).splitlines()}
    for commit in sorted(commits):
        attributes = _candidate_attributes(source, commit)
        if attributes != _candidate_attributes(candidate, commit):
            raise WikiError('candidate-compatibility: repository-local Git attributes differ')
        for (_, attribute), raw_value in attributes.items():
            value = raw_value.decode('utf-8', errors='surrogateescape')
            if value in ('set', 'unset', 'unspecified'):
                continue
            if attribute == b'merge':
                keys.update(f'merge.{value}.{option}' for option in ('driver', 'recursive'))
            elif attribute == b'filter':
                keys.update(f'filter.{value}.{option}' for option in ('clean', 'smudge', 'process', 'required'))
    default = text(source, 'config', '--get', 'merge.default', check=False).strip()
    if default:
        keys.update(f'merge.{default}.{option}' for option in ('driver', 'recursive'))
    for key in sorted(keys):
        if (run(source, 'config', '--get-all', key, check=False)
                != run(candidate, 'config', '--get-all', key, check=False)):
            raise WikiError(f'candidate-compatibility: required Git configuration differs: {key}')


def prepare_candidate(source, candidate, ours, theirs, *, merge=False, message=None, local_only=False):
    """Clone source ordinarily, import exact O/T objects, then prepare a detached candidate branch."""
    source, candidate = Path(source).resolve(), Path(candidate).absolute()
    if candidate.exists() or candidate.is_symlink():
        raise WikiError(f'candidate path already exists: {candidate}')
    ours_is_ancestor = _ancestor(source, ours, theirs)
    theirs_is_ancestor = _ancestor(source, theirs, ours)
    if not ours_is_ancestor and not theirs_is_ancestor and not merge:
        return {'state': 'diverged', 'candidate': ours, 'ours': ours, 'theirs': theirs}
    if not ours_is_ancestor and not theirs_is_ancestor:
        bases = text(source, 'merge-base', '--all', ours, theirs, check=False).splitlines()
        if not bases:
            raise WikiError('sync refuses unrelated histories')
    if not candidate.parent.is_dir():
        candidate.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(['git', 'clone', '--no-local', '--no-checkout', '--', str(source), str(candidate)],
                       capture_output=True, env=gitops.environment())
    if p.returncode:
        raise WikiError(f'git clone failed for candidate: {p.stderr.decode(errors="replace").strip()}')
    _identity(source, candidate)
    text(candidate, 'fetch', '--no-tags', '--no-recurse-submodules', '--', str(source), ours, theirs)
    text(candidate, 'cat-file', '-e', f'{ours}^{{commit}}')
    text(candidate, 'cat-file', '-e', f'{theirs}^{{commit}}')
    check_candidate_compatibility(source, candidate, ours, theirs)
    text(candidate, 'switch', '--detach', ours)
    text(candidate, 'switch', '-c', 'pisar-candidate')
    if local_only:
        return {'state': 'ready', 'candidate': ours, 'ours': ours, 'theirs': theirs}
    if _ancestor(candidate, theirs, ours):
        return {'state': 'ready', 'candidate': ours, 'ours': ours, 'theirs': theirs}
    if _ancestor(candidate, ours, theirs):
        text(candidate, 'merge', '--ff-only', '--no-autostash', '--no-overwrite-ignore', '--no-edit', theirs)
        return {'state': 'ready', 'candidate': head(candidate), 'ours': ours, 'theirs': theirs}
    result = subprocess.run(command(candidate, 'merge', '--no-ff', '--no-commit', '--no-edit',
                                    '--no-autostash', '--no-overwrite-ignore', '-s', 'ort', theirs),
                            capture_output=True, env=gitops.environment())
    merge_head = text(candidate, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}', check=False).strip()
    if result.returncode and merge_head != theirs:
        raise WikiError(f'git merge failed in candidate: {result.stderr.decode(errors="replace").strip()}')
    if merge_head != theirs:
        raise WikiError('candidate merge did not retain the recorded incoming commit')
    unmerged = run(candidate, 'ls-files', '--unmerged', '-z')
    if unmerged:
        return {'state': 'conflict', 'candidate': ours, 'ours': ours, 'theirs': theirs,
                'unmerged': unmerged.decode('utf-8', errors='surrogateescape')}
    text(candidate, 'diff', '--cached', '--check')
    commit = commit_merge(candidate, message=message)
    if parents(candidate, commit) != (ours, theirs):
        raise WikiError('candidate merge commit has unexpected parents')
    return {'state': 'ready', 'candidate': commit, 'ours': ours, 'theirs': theirs}


def commit_merge(candidate, *, message=None):
    """Finish a user-resolved merge already recorded by Git in this disposable candidate."""
    merge_head = text(candidate, 'rev-parse', '--verify', 'MERGE_HEAD^{commit}', check=False).strip()
    if not merge_head:
        raise WikiError('candidate has no unfinished merge')
    if run(candidate, 'ls-files', '--unmerged', '-z'):
        raise WikiError('candidate still has unresolved paths')
    unstaged = subprocess.run(command(candidate, 'diff-files', '--quiet'),
                              capture_output=True, env=gitops.environment())
    if unstaged.returncode not in (0,):
        raise WikiError('candidate resolution has unstaged changes')
    if run(candidate, 'ls-files', '--others', '--exclude-standard', '-z'):
        raise WikiError('candidate has untracked files; stage or remove them explicitly')
    text(candidate, 'diff', '--cached', '--check')
    if callable(message):
        message = message(candidate)
    args = ['commit', '--no-gpg-sign', '--no-edit']
    if message is None:
        args += ['-m', 'wiki: sync-candidate']
    else:
        args += ['-F', '-']
    run(candidate, *args, input=(message or 'wiki: sync-candidate').encode('utf-8'))
    return head(candidate)
