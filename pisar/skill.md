---
name: pisar
description: Use the pisar CLI to discover, validate, search, capture and save knowledge in a Git-backed Markdown repository, and to route a global inbox with explicit human approval. Read before running any pisar command or writing a plan file for it.
---

# Working with pisar

pisar is an offline, standard-library command-line tool for a **file-authoritative
knowledge repository kept in Git**. Files are the source of truth. pisar finds,
validates and searches documents, and performs a few explicit, journaled Git
writes. It never calls a model, never reads meaning into text and never touches
the network or any remote. **You** (the agent) read sources, understand them and
write the summaries; pisar checks structure and applies what you prepared.

All successful output is JSON on stdout. Errors go to stderr prefixed `pisar:`
and exit 1; argument errors exit 2. `pisar check` prints JSON and exits 1 when
validation fails.

## Concepts

- **Root**: the knowledge repository, its own Git top level.
- **Domain**: a top-level directory of the root holding a `.domain.toml`
  (`schema_version = 1`, `id` equal to the directory name, `title`, optional
  `[layout]`, `[ids]`, `[sensitive]`). For example `personal` and one domain per
  employer or client. Top-level directories without the marker are not domains.
  The domain is the confidentiality boundary: data must not cross between domains.
- **Space**: a directory inside a domain holding a `.wiki.toml` (`schema_version =
  1`, stable `id`, `kind` = `project`|`area`|`resource`, `status` =
  `active`|`archived`). Ids are unique within their domain, archived spaces
  included; other domains may reuse them. A space is named everywhere by its
  **address** `domain/id`, for example `work/alpha`. The nearest enclosing space
  owns a document. Spaces may live in initialized Git submodules; pisar commits
  there first, then records the new submodule commit in the parent.
- **Document**: Markdown with TOML front matter between `+++` lines. Required
  keys include `schema_version`, `id`, `type`, `title`, `space_ids` (addresses:
  owner first, then related spaces of the same domain) and `sources` (possibly
  empty). Types:
  `source`, `meeting`, `overview`, `decision`, `note`, `recipe`, `movie`,
  `series`, `book`. Timestamps are quoted RFC 3339 with an explicit offset or
  `Z`; omit values you do not know instead of inventing them.
- **Reference**: `wiki:<domain>/<space-id>:<document-id>`, optionally
  `#fragment`. Stable across moving a space inside its domain. Document IDs are
  unique per owner. `pisar check` reports the old `wiki:<space-id>:<document-id>`
  form as a legacy reference.
- **Source**: an original file kept byte for byte under the space's `sources/`.
  Never edit one. URLs and `urn:` values are kept as provenance and not fetched.
- **Runtime state directory**: journals, locks, the global inbox, triage batches
  and processed originals. It lives outside every Git repository and outside the
  root, and must be backed up with the root.

## Configuration

Global options go **before** the command. A flag beats its environment variable,
which beats the user's config file (`$XDG_CONFIG_HOME/pisar/config.toml`), which
beats the default; empty variables count as unset. `pisar config show` prints
each effective value and its source (`flag`, `env`, `config` or `default`).

| Option | Environment | Default |
| --- | --- | --- |
| `--root PATH` | `PISAR_ROOT` | `$XDG_DATA_HOME/wiki` |
| `--state-dir PATH` | `PISAR_STATE_DIR` | `$XDG_DATA_HOME/pisar` |
| `--ruwana PATH` | `PISAR_RUWANA_BIN` | `ruwana` on `PATH` |

`$XDG_DATA_HOME` falls back to the `.local/share` directory of the home
directory. `pisar --version` and `pisar --skill` need no root. pisar ignores
`WIKI_ROOT`; it sets that variable only for the `ruwana` child process it runs.

## Reading (safe, no writes)

```sh
pisar spaces [--include D1,D2] [--exclude D3]
pisar check [--include ...] [--exclude ...]
pisar inventory [--include ...] [--exclude ...] [--space DOMAIN/ID]
pisar search "text" [--include ...] [--exclude ...] [--space DOMAIN/ID]
pisar read wiki:<domain>/<space>:<document> [--include ...] [--exclude ...]
pisar read <root-relative-path>
```

- Run `pisar spaces` first; use the real addresses it reports, never guessed ones.
- `--include` keeps only the listed domains, `--exclude` drops them (after
  `--include`). Both are comma separated and repeatable; neither means all
  domains; an unknown domain is an error. Restrict reads to the domain you work
  in so other domains' content does not reach you.
- `pisar search` is **lexical**: a case-insensitive substring match, one matching
  line per document, no ranking, no semantics. It cannot prove that something is
  absent. Try several phrasings, synonyms and spellings, then read candidates.
- `pisar inventory` is a **complete** traversal of the selected domains or a space,
  with no limit.
  Use it, not search, whenever you must be exhaustive. Pending items in a space's
  `inbox/` show up here too.
- `pisar read` returns UTF-8 text only; it cannot show PDF or audio originals.
- Run `pisar check` after you edit any document by hand. Writes by pisar itself
  require a valid root.

## Writing

Every write requires the root to be a clean Git top level. Unrelated dirty or
staged paths block the operation; commit or set aside that work first. Never
work around it with `git add .`, `git reset --hard`, `git stash` or by deleting
journals. pisar makes **local commits only**: no push, no remote, hooks and
signing disabled.

### 1. Capture a source into a known space

```sh
pisar capture --space <domain>/<space-id> --id <capture-id> [--sha256 HEX] --source /abs/file
```

Commits the unchanged bytes plus a pending descriptor in the space's `inbox/`.
It does not read or understand the content and does not delete the original.
Decide the target space and its domain yourself; a source already inside the
root must be in the target's domain. An untracked file that the
user already dropped into that space's `inbox/` may be passed as `--source`.
Repeating the same command is safe and creates no second commit.

### 2. Save a meeting from a plan you wrote

You read the source, then write a JSON plan **outside every Git repository** and
apply it:

```sh
pisar save --plan /abs/plan.json
pisar --ruwana /abs/bin/ruwana save --plan /abs/plan.json   # explicit task binary
```

Plan fields:

- `schema_version` (1), `operation_id` (stable; reuse it to retry),
  `space_id` (owner address `domain/id`). Every `space_id` and
  `related_space_ids` entry in a plan is an address in the owner's domain.
- `capture_id` (optional; the pending descriptor this plan finishes). When
  present, `source.path` is the preserved original and the descriptor becomes
  `processed`.
- `source`: `{path, sha256}` of the original, absolute path and real hash.
- `meeting`: `id`, `title`, `body` (finished Markdown), optional
  `related_space_ids` and `occurred_at`. Write **one coherent summary**:
  context, discussion, decisions, proposals (keep these apart from decisions),
  tasks, open questions, contradictions, and what the evidence does not cover.
  Do not write a `## Ruwana references` section; pisar generates it.
- `tasks`: each with a unique kebab-case `id`, `space_id` (the owner or a
  related space, of any kind), `title`, and `agreed: true`. Create a task only for something that was really agreed.
  Optional `description` and `due` (RFC 3339 with offset). Leave out unknown
  owners and dates; ruwana has no assignee field, so keep known owners in the
  meeting body and description. A task needs the `ruwana` CLI.
- `pages`: optional extra pages to create or update, each
  `{space_id, path, content, base_sha256?}`. Omit `base_sha256` to create; give
  the exact current hash to update. Only include knowledge worth keeping.

pisar creates exactly one meeting document, preserves the transcript, creates
the tasks through ruwana and links them. It validates metadata and references,
not whether your narrative is accurate. Do not present model conclusions as
facts or agreements; state uncertainty in the body.

### 3. Route a global inbox with human approval

Files dropped into `<state-dir>/inbox` only queue; nothing watches them.

```sh
pisar triage prepare --batch ID [--plan routing.json | --inbox DIR]
pisar triage report  --batch ID
pisar triage accept  --batch ID --item ITEM
pisar triage reroute --batch ID --item ITEM --space DOMAIN/ID
pisar triage defer   --batch ID --item ITEM
```

- `prepare` snapshots the inbox (or your routing JSON) into the state directory.
  Nothing is written to any repository yet. Items with no known destination
  stay unrouted.
- Your routing plan is `{"schema_version": 1, "items": [{id, source, sha256,
  space_id | null, plan?}]}` with `space_id` an address; an item may embed a
  meeting plan as above.
- `report` shows what would happen. Show it to the user.
- **Only the user decides.** Run `accept` only for items they explicitly
  approved. `reroute` changes the destination but does not approve; it discards
  any prepared semantic plan, so prepare a fresh one. `defer` leaves the item
  outside any repository.
- Once an accept has started, `defer`/`reroute` are refused. Fix the cause and
  retry the same `accept`. Do not delete journals.

## Guard: report possibly sensitive text

```sh
pisar guard check (--file F | --text=T) --from DOMAIN [--to DOMAIN] [--outbound]
```

Prints `{findings: [{id, tier, category, line, excerpt, hint}], limits}` and
exits 0 whenever the scan ran; findings are information. Tiers: `severe`
(secrets, emails, phone numbers, fenced code), `ask` (quotes, meeting markers,
URLs, hostnames, IPs), `warn` (terms of the `--from` domain, day-precise dates).
`--outbound` reports the terms of every domain. It is a best-effort lexical
scan: it misses paraphrases and unlisted names and never edits text. Show the
findings to the user and change the text only with their agreement.

## Recovery and rollback

- Every write has a journal in the state directory. If an operation stops
  halfway (missing `ruwana`, a failing check, an interrupted run), fix the cause
  and **rerun the identical command or plan with the same operation ID**. A
  completed operation verifies and writes nothing new. Changing the inputs under
  the same ID is a conflict, by design.
- Operations are **not atomic** across repositories and ruwana; partial progress
  is recorded, not hidden. Read the journal and `git status` in the owning
  repository before deciding anything.
- Never revert an operation's commits wholesale: commits that contain
  `.ruwana/` task files can delete tasks or restore old statuses. Roll back
  knowledge files only, leave task files and current status alone, do it in each
  affected submodule before its parent, and review the staged diff before
  committing. Task status is authoritative in ruwana, not in a meeting summary.

## Rules of thumb

1. Discover before acting: `spaces`, then `inventory` or `search`, then `read`.
2. Keep domains apart; when the domain is unclear, ask.
3. Never modify originals; derive new documents beside them.
4. Cite sources with references or fragments, and say what you could not verify.
5. Plans, drafts and journals stay outside Git.
6. A task or decision exists only if it was actually agreed.
7. Report failures as they are, including partial writes. Do not retry by
   changing IDs or deleting state.
8. `pisar <command> --help` lists exact flags.
