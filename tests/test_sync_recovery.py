"""Recovery never replays over the user's work (review 20, findings 3, 4, 5 and 8).

Recovery acts only on states it can prove it produced from the journaled old and new entries: a path already at its
new index entry is left alone whatever the working copy holds; a path at its old entry is written; anything else is
a refusal that changes nothing. A dry run never writes, in any phase."""
from pisar import sync
from .sync_faults import Faults, RemoteFixture, run_interrupted
from .support import commit_all, document, git


def add_three(repo):
    """A remote change with three kinds of path: an added file, a modified file and a deleted file."""
    (repo / 'work/team/alpha/added.md').write_text(document('added', 'work/alpha', body='Added.'))
    (repo / 'work/team/alpha/call.md').write_text(document(related=('work/beta',), body='Modified remotely.'))
    (repo / 'work/team/alpha/gone.md').unlink()


def seed_gone(fixture):
    """Commit the file that the remote will delete, both locally and on the remote."""
    (fixture.root / 'work/team/alpha/gone.md').write_text(document('gone', 'work/alpha', body='To delete.'))
    commit_all(fixture.root)
    git(fixture.root, 'push', '-q', 'origin', fixture.branch)
    git(fixture.other, 'pull', '-q', 'origin', fixture.branch)


class RecoveryTests(RemoteFixture):
    def test_staged_user_content_survives_a_retry_and_nothing_changes(self):
        # Review 20 finding 3: interrupted at `moved`; the user stages their own content, then restores the bytes.
        self.new_world('staged')
        self.publish(lambda repo: (repo / 'work/team/alpha/call.md').write_text(
            document(related=('work/beta',), body='Remote edit.')))
        self.start()
        with Faults('moved'):
            self.assertTrue(run_interrupted(sync.do_pull, self.root, self.state))
        path = self.root / 'work/team/alpha/call.md'
        old = path.read_bytes()
        path.write_text('unique user index-only content\n')
        git(self.root, 'add', 'work/team/alpha/call.md')
        path.write_bytes(old)
        staged = git(self.root, 'show', ':work/team/alpha/call.md')
        before = self.snapshot()
        p = self.cli('sync', 'pull', ok=False)
        self.assertTrue(p.stderr.startswith('pisar: '), p.stderr)
        self.assertEqual(git(self.root, 'show', ':work/team/alpha/call.md'), staged)
        self.assertEqual(self.snapshot(), before)

    def test_a_user_deletion_after_the_files_were_applied_is_refused_not_undone(self):
        # Review 20 finding 4: interrupted after the document was written and indexed, then deleted by the user.
        self.new_world('deleted')
        self.publish(lambda repo: (repo / 'work/team/alpha/fresh.md').write_text(
            document('fresh', 'work/alpha', body='New.')))
        self.start()
        with Faults('files'):
            self.assertTrue(run_interrupted(sync.do_pull, self.root, self.state))
        (self.root / 'work/team/alpha/fresh.md').unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(Exception, 'fresh.md'):
            sync.do_pull(self.root, self.state)
        self.assertFalse((self.root / 'work/team/alpha/fresh.md').exists(), 'the user deletion was undone')
        self.assertEqual(self.snapshot(), before)

    def test_an_interruption_on_either_side_of_every_file_checkpoint_resumes_correctly(self):
        for name in ('file-written', 'index-set'):
            for nth in range(1, 7):
                with self.subTest(checkpoint=name, nth=nth):
                    self.new_world(f'{name}-{nth}')
                    seed_gone(self)
                    self.publish(add_three)
                    self.start()
                    with Faults(name, nth):
                        if not run_interrupted(sync.do_pull, self.root, self.state):
                            continue  # this run has fewer than NTH such points
                    result = sync.do_pull(self.root, self.state)
                    self.assertIn(result['status'], ('fast-forward', 'up-to-date'))
                    self.assertTrue((self.root / 'work/team/alpha/added.md').is_file())
                    self.assertIn('Modified remotely.', (self.root / 'work/team/alpha/call.md').read_text())
                    self.assertFalse((self.root / 'work/team/alpha/gone.md').exists())
                    self.assertEqual(git(self.root, 'status', '--porcelain'), '')
                    self.assertEqual(git(self.root, 'rev-parse', 'HEAD'),
                                     git(self.root, 'rev-parse', f'refs/pisar/sync/origin/{self.branch}'))

    def test_a_changed_planned_file_is_never_overwritten_on_resume(self):
        self.new_world('edited')
        self.publish(lambda repo: (repo / 'work/team/alpha/call.md').write_text(
            document(related=('work/beta',), body='Remote edit.')))
        self.start()
        with Faults('moved'):
            self.assertTrue(run_interrupted(sync.do_pull, self.root, self.state))
        (self.root / 'work/team/alpha/call.md').write_text('Edited by hand.\n')
        with self.assertRaisesRegex(Exception, 'call.md'):
            sync.do_pull(self.root, self.state)
        self.assertEqual((self.root / 'work/team/alpha/call.md').read_text(), 'Edited by hand.\n')


class DryRunTests(RemoteFixture):
    def interrupted_fast_forward(self, name, fault):
        self.new_world(name)
        self.publish(lambda repo: (repo / 'work/team/alpha/new.md').write_text(document('new', 'work/alpha')))
        self.start()
        with Faults(fault):
            self.assertTrue(run_interrupted(sync.do_pull, self.root, self.state))

    def assert_dry_run_writes_nothing(self):
        before, journals, listing = self.snapshot(), self.journals(), self.sync_listing()
        result = sync.do_pull(self.root, self.state, dry_run=True)
        self.assertEqual(result['status'], 'would-resume', result)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.journals(), journals)
        self.assertEqual(self.sync_listing(), listing)
        return result

    def sync_listing(self):
        root = self.state / 'roots'
        if not root.exists():
            return []
        return sorted((p.relative_to(root).as_posix(), p.read_bytes() if p.is_file() else None)
                      for p in root.rglob('*'))

    def test_a_dry_run_of_every_resumable_phase_reports_and_writes_nothing(self):
        # The journal's phase is written after each checkpoint: at `moved` the branch has moved but the journal
        # still says planned, which is the truth a dry run must report.
        for fault, phase in (('journal', 'planned'), ('pending', 'planned'), ('moved', 'planned'),
                             ('files', 'moved')):
            with self.subTest(fault=fault):
                self.interrupted_fast_forward(f'dry-{fault}', fault)
                result = self.assert_dry_run_writes_nothing()
                self.assertEqual(result['phase'], phase)

    def test_a_dry_run_of_a_conflicted_sync_reports_the_conflicts_and_writes_nothing(self):
        self.new_world('dry-conflict')
        self.publish(lambda repo: (repo / 'work/team/alpha/call.md').write_text(
            document(related=('work/beta',), body='Remote wording.')))
        (self.root / 'work/team/alpha/call.md').write_text(document(related=('work/beta',), body='Local wording.'))
        commit_all(self.root)
        self.start()
        self.assertEqual(sync.do_pull(self.root, self.state, merge=True)['status'], 'conflicts')
        before, journals, listing = self.snapshot(), self.journals(), self.sync_listing()
        result = sync.do_pull(self.root, self.state, merge=True, dry_run=True)
        self.assertEqual(result['status'], 'conflicts')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.journals(), journals)
        self.assertEqual(self.sync_listing(), listing)


class AncestorRecoveryTests(RemoteFixture):
    def test_a_parent_interrupted_between_its_ref_move_and_its_index_sync_is_finished_clean(self):
        # Review 20 finding 8: the child moved; the parent's branch moved; the parent's index was not yet synced.
        self.new_world('parent')
        child = self.add_submodule()
        origin = self.base / 'module-origin'
        bare = self.base / 'child.git'
        git(origin, 'clone', '-q', '--bare', str(origin), str(bare))
        git(child, 'remote', 'set-url', 'origin', str(bare))
        (origin / 'new.txt').write_text('new\n')
        commit_all(origin)
        git(origin, 'push', '-q', str(bare), 'HEAD')
        sync.do_fetch(self.root, self.state, relative='work/module')
        with Faults('ancestor-moved'):
            self.assertTrue(run_interrupted(sync.do_pull, self.root, self.state, relative='work/module'))
        self.assertNotEqual(git(self.root, 'diff', '--cached', '--name-only'), '')  # mid-sync for real
        result = sync.do_pull(self.root, self.state, relative='work/module')
        self.assertEqual(result['status'], 'fast-forward')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(git(self.root, 'diff', '--cached', '--name-only'), '')
