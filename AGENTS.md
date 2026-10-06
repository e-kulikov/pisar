# Development rules

Pisar is an offline Python >=3.11 CLI for Git-backed knowledge repositories.
Canonical documents, `.wiki.toml` and `wiki:` links are data formats, not branding.
Keep runtime state outside all Git repositories. Preserve source bytes and user
changes. Personal/work boundaries and explicit global-inbox acceptance are mandatory.

Use test-first changes and run the full unittest suite before claiming success.
Tests must use temporary data roots, never live knowledge or task data.
Never store credentials, private knowledge, agent logs or workstation-specific
session metadata in this public repository. No external model/index service.
Use Conventional Commits. Release-please owns version bumps and changelog.
Only publish, push or change GitHub settings when explicitly authorized by the user.
Implementation workers do not publish; the supervisor owns publication and release.

## Layout

- `pisar/` — standard-library package; `cli.py` is the entry point. `spaces.py`,
  `documents.py`, `search.py` read; `operations.py`, `triage.py`, `gitops.py`
  write; `safety.py` guards paths and symlinks; `settings.py` resolves options;
  `ruwana.py` is the only adapter to the external task tracker.
- `tests/` — unittest suite; `tests/support.py` builds temporary fixtures.
- `pisar/agent.py` and `agent-prompt.md` implement `--agent`; any change to its
  allowlist needs a test in `tests/test_agent.py` showing the new rule cannot run
  arbitrary code (no bare `git *`, `pisar *`, `mv`, interpreters or shells).
- `scripts/` — zipapp build, release packaging, version and commit checks.
- `.github/workflows/` — `verify.yml` (tests, zipapp, shell/commit checks) and
  `release.yml` (release-please, archive, checksums, provenance).

## Verify before claiming success

```sh
python -m unittest discover -s tests -t . -v        # from source
python scripts/build-zipapp.py /path/outside/repo/pisar
PISAR_TEST_EXECUTABLE=/path/outside/repo/pisar python -m unittest discover -s tests -t .
```

Real-`ruwana` tests skip unless `PISAR_TEST_RUWANA` points at a binary; set
`PISAR_TEST_REQUIRE_RUWANA=1` to make that mandatory. Run both the source and the
zipapp suites, and run shellcheck/actionlint on changed scripts and workflows.
Expect one documented skip in the zipapp run.

## Testing rules

- Every defect found in review gets a regression test before the fix.
- Tests must pass in a clean environment: no reliance on the developer's global
  Git identity, config, `XDG_*` variables or `PISAR_*` variables. Set a synthetic
  identity inside every fixture repository and submodule, and reproduce CI
  failures with a temporary `HOME` and `XDG_CONFIG_HOME`.
- Dangerous scenarios (rollback, mixed submodule changes, interrupted saves,
  retrying a completed `save`) run only on temporary fixtures, never live data.
- Cover edge cases: personal/work boundary, uncommitted user edits, submodules
  (children before parents on rollback), whitespace and empty fields.

## Design constraints worth remembering

- File content is authoritative; the search index and runtime state are
  disposable and live outside Git.
- Normalise ruwana fields in `ruwana.py` only (trim, empty description as
  `null`) and compare normalised values; keep originals in history. Rust and
  Python differ on what counts as whitespace.
- Resolve a relative `--ruwana`/`PISAR_RUWANA_BIN` path to absolute before any
  subprocess changes its working directory.
- Pass the child environment to ruwana explicitly; never inherit `WIKI_ROOT` or
  other ambient settings.
- Persist operation fingerprints so an interrupted operation can resume without
  duplicates.

## Commits and releases

- Conventional Commits; release-please opens the release PR, bumps the version
  and writes the changelog. Never edit those by hand.
- Author and committer identity for public history must be the GitHub noreply
  address; never commit a corporate or private email.
- Before any public push, check the diff and history for local paths, private
  knowledge, session identifiers and credentials. Never push `--all` or
  `--mirror`; keep backup refs local.
- Release workflow checks out the tagged commit by SHA and does not persist push
  credentials. A push may report a transport error even though GitHub accepted
  it: confirm with `git ls-remote` or the GitHub API before retrying.
- Never remove the `autorelease: pending` label from a release-please PR and
  merge it only with the label present; without it no tag or release is created
  and the green release run is misleading. Confirm that the tag and release
  exist after every merge.
