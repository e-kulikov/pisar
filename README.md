# pisar

pisar is an offline command-line tool for a file-authoritative knowledge
repository kept in Git and split into domains (for example personal and one per
employer or client). It discovers domains and spaces, validates and searches
Markdown documents with TOML front matter, captures sources without changing
their bytes, applies externally prepared meeting plans, and routes a global inbox
only after explicit acceptance. No network, model, daemon, index service or cloud
account is used; all Git activity is local and nothing is pushed. The one
exception is `pisar domain add --repo`, which asks `git` to clone a repository
(see [Creating and organising domains and spaces](#creating-and-organising-domains-and-spaces)).

## The name

*Pisar* (писарь) is a scribe: the person who sets down what others say and do.
In Russian the word is simply the verb *писать* ("to write") plus the agent
suffix *-арь* ([Wiktionary](https://en.wiktionary.org/wiki/%D0%BF%D0%B8%D1%81%D0%B0%D1%80%D1%8C)). The same word, built the same way, exists
across the Slavic languages: Bulgarian, Macedonian, Serbo-Croatian and Ukrainian
*писар* ([Wiktionary](https://en.wiktionary.org/wiki/%D0%BF%D0%B8%D1%81%D0%B0%D1%80)), Slovene *pisar* ([SSKJ²](https://www.fran.si/iskanje?FilteredDictionaryIds=133&View=1&Query=pisar)), Czech *písař*
([Wiktionary](https://en.wiktionary.org/wiki/p%C3%ADsa%C5%99)) and Polish *pisarz*, recorded since 1391 ([Wiktionary](https://en.wiktionary.org/wiki/pisarz)).

The verb is old. It is attested in Old Church Slavonic as *писати* ("to write";
[Wiktionary](https://en.wiktionary.org/wiki/%D0%BF%D0%B8%D1%81%D0%B0%D1%82%D0%B8)) and comes from Proto-Slavic *\*pьsati*, which meant both
"to draw" and "to write". That in turn goes back to the Indo-European root
*\*peyḱ-* ("to mark, color, paint, decorate, embroider"; [Wiktionary](https://en.wiktionary.org/wiki/Reconstruction:Proto-Slavic/p%D1%8Csati)),
the same root that gave Latin *pingō* ("I paint"; [Wiktionary](https://en.wiktionary.org/wiki/pingo)). Writing,
in other words, began as making marks on a surface.

A scribe does not decide what was agreed and does not rewrite the originals.
They write down faithfully, keep the documents in order, and can say where each
statement came from. That is the job description for this tool. pisar never
invents or interprets: it keeps sources unchanged, records provenance, applies
the plan an agent or a person prepared, and leaves a journal of what it did. The
thinking stays with whoever reads the sources; pisar is the scribe.

## Requirements

- Python 3.11 or newer, available as `python3`. pisar uses only the standard
  library and has no other dependencies.
- Git, for every write.
- The `ruwana` task tracker CLI, only when a meeting plan contains agreed tasks.
- Linux. pisar uses POSIX `fcntl` locks; other POSIX systems are untested and
  Windows is unsupported. Every symlinked path ancestor is refused, including the
  usual macOS `/tmp` and `/var` aliases, so use real (non-symlink) paths there.

## Install

Releases ship one architecture-independent archive, `pisar_<version>_any.tar.gz`,
holding the `pisar` executable (a Python zipapp) and this README, plus
`SHA256SUMS`. Assets carry GitHub build provenance attestations.

With [mise](https://mise.jdx.dev), using its GitHub backend:

```sh
mise use -g "github:e-kulikov/pisar[asset_pattern=pisar_*_any.tar.gz,strip_components=1]"
pisar --version
```

or in `mise.toml`:

```toml
[tools]
"github:e-kulikov/pisar" = { version = "latest", asset_pattern = "pisar_*_any.tar.gz", strip_components = 1 }
```

The archive holds a single `pisar_<version>_any/` directory; `strip_components = 1`
puts `pisar` at the top of the installed tool directory, where mise finds it.

mise checks the release's GitHub artifact attestation when one is available.
The executable still needs a Python 3.11+ `python3` on `PATH`; mise can provide
one too (`mise use -g python@3.13`).

Manually, download the archive and `SHA256SUMS` from a release, then:

```sh
sha256sum --check --ignore-missing SHA256SUMS
gh attestation verify pisar_*_any.tar.gz --repo e-kulikov/pisar
tar -xzf pisar_*_any.tar.gz
install -m 0755 pisar_*_any/pisar ~/.local/bin/pisar
```

From a source checkout, run `./bin/pisar` or `python3 -m pisar`.

## Configuration

Global options go **before** the command. Each setting is taken from the first
of: the explicit option, its environment variable, the config file, the built-in
default. Empty variables count as unset.

| Option | Environment | Config key | Default | Purpose |
| --- | --- | --- | --- | --- |
| `--root PATH` | `PISAR_ROOT` | `root` | `$XDG_DATA_HOME/wiki` | Knowledge repository root |
| `--state-dir PATH` | `PISAR_STATE_DIR` | `state_dir` | `$XDG_DATA_HOME/pisar` | External runtime: journals, locks, global inbox, triage |
| `--ruwana PATH` | `PISAR_RUWANA_BIN` | `ruwana` | `ruwana` on `PATH` | Task tracker binary |

`$XDG_DATA_HOME` falls back to `~/.local/share` when it is unset or not absolute.

### Config file

The optional file `$XDG_CONFIG_HOME/pisar/config.toml` (`~/.config/pisar/config.toml`
when `XDG_CONFIG_HOME` is unset or not absolute) holds the same settings plus the
agent options. Every key is optional:

```toml
default_agent = "claude"        # used by `pisar --agent` without a value
root = "/abs/path/wiki"         # like PISAR_ROOT; must be absolute
state_dir = "/abs/path/pisar"   # like PISAR_STATE_DIR; must be absolute
ruwana = "ruwana"               # like PISAR_RUWANA_BIN; a command name or an absolute path

[agents.claude]                 # the main `pisar --agent` session
model = "sonnet"
effort = "medium"

[agents.claude.reviewer]        # lesson reviewer subagent (default opus, high)
model = "opus"
effort = "high"

[agents.claude.researcher]      # research subagent (default sonnet, medium)
model = "sonnet"
effort = "medium"
```

A missing file is fine. Unknown keys, wrong types, empty strings, a relative
`root` or `state_dir` and a relative `ruwana` path are errors naming the file and
the key, and stop every command that reads settings (`--version` and `--skill`
do not). `model` and `effort` are passed to the agent verbatim, with no list of
allowed values; when the main session leaves them unset, nothing is passed.

`pisar config show` prints every effective value with its source, one of `flag`,
`env`, `config` or `default`. It needs no existing root:

```sh
pisar config show
pisar --root /tmp/other config show   # root now reports "source": "flag"
```

```json
{
  "file": {"path": "/home/me/.config/pisar/config.toml", "exists": true},
  "settings": {
    "root": {"value": "/home/me/.local/share/wiki", "source": "default"},
    "default_agent": {"value": "claude", "source": "config"},
    "agents.claude.reviewer.model": {"value": "opus", "source": "default"}
  }
}
```

The example is shortened; the output lists `root`, `state_dir`, `ruwana`,
`default_agent` and `model`/`effort` for the main session, `reviewer` and
`researcher`.
The runtime directory must be outside every Git repository, must not be inside
the root, and must not contain the root; overlapping combinations are refused
before any runtime file is written. Read-only commands do not touch the runtime.
`WIKI_ROOT` is not a pisar setting and is ignored (see the ruwana adapter below).

`pisar --version` prints `pisar <version>` and needs no root. `pisar --skill`
prints a self-contained guide for AI agents (concepts, commands, plan formats,
safety and recovery rules) to stdout, also without a root; point an agent at it
before it uses pisar. All successful
command output is JSON. Errors go to stderr prefixed with `pisar:` and exit 1
(argument errors exit 2). `check` emits JSON and exits 1 when validation fails.

```sh
pisar spaces
pisar --root /absolute/knowledge check
PISAR_ROOT=/absolute/knowledge pisar inventory --include work
```

### Data formats

The knowledge formats are independent of the executable's name and remain
stable: domain markers are named `.domain.toml`, space descriptors `.wiki.toml`,
stable document references are `wiki:DOMAIN/SPACE:DOCUMENT`, and every commit
pisar makes has the subject
`wiki: OPERATION-ID`. Retries recognize their own commits by that subject and the
rollback procedure below selects commits with it.

## Running an agent

```sh
pisar --agent claude                 # start Claude Code in the knowledge root
pisar --agent                        # start default_agent from the config file
pisar --agent claude -- -c           # arguments after -- go to the agent itself
```

`--agent` starts an AI agent confined to this repository, so it behaves as a
knowledge assistant. Today only `claude` (Claude Code, which must be on `PATH`)
is supported. It is used instead of a command:

- It runs in the root, with its own configuration directory
  `<state-dir>/agents/claude` (mode 0700, outside every Git repository). Log in
  once there; none of your global Claude settings, hooks, plugins, skills or MCP
  servers apply. The child gets `PISAR_ROOT`, `PISAR_STATE_DIR` and
  `PISAR_RUWANA_BIN` for the same selection and never inherits `WIKI_ROOT`.
- `--model` and `--effort` come from `[agents.claude]` in the config file
  (verbatim, only when set). Your own `--model` or `--effort` after `--` wins
  and the configured value is then not passed.
- Its system prompt is replaced by a short built-in one: start with
  `pisar spaces`, and do with pisar everything pisar can do, so the repository,
  its journals and the task tracker stay in sync.
- Only the tools `Read`, `Write`, `Edit`, `Glob`, `Grep` and `Bash` exist. MCP is
  strict: no servers are loaded unless the root contains a regular (non-symlink)
  `.mcp.json`, which is then passed with `--mcp-config`. `Bash` is limited to read-only `git status|diff|log`, `git add`,
  `git commit`, `git mv` and the pisar subcommands. There is no bare `git *`
  or `pisar *` (git aliases and `-c`, or `pisar --ruwana BIN`, would run arbitrary
  programs) and no `mv`: files enter the repository with `pisar capture` and are
  renamed with `git mv`. `git push` and edits inside `.git` are denied.
- The allowlist also covers `pisar domain|space|guard|config|research *` and the
  `pisar lesson` steps `start`, `check`, `review`, `show` and `discard`.
  `pisar lesson accept` is never allowed silently: it is in the permission `ask`
  list (passed with `--settings`), so it always needs your confirmation. The
  only place the agent may edit outside the root is the lesson workspace
  `<state-dir>/lessons` (`--allowedTools Edit(//<state-dir>/lessons/**)`).
- A bundled plugin is extracted to `<state-dir>/agents/claude/plugin/<pisar
  version>-<hash>/` and passed with `--plugin-dir`. The hash covers everything
  generated, so each generation is immutable: changed settings or a new pisar
  publish a new directory with one atomic rename and never alter one a running
  session uses. pisar never deletes, moves or repairs plugin directories (a
  damaged one is left alone and a fresh generation with a `-N` suffix is used),
  so old generations and the rare `.build.tmp-*` leftover of a crash stay until
  you remove them; they are tiny. It provides the skills `pisar` (the
  text of `pisar --skill`), `lessons` and `research`; a hook that denies direct
  `Write`/`Edit`/`MultiEdit` of `.wiki.toml` and `.domain.toml` (use `pisar domain`
  and `pisar space`); and the `lesson-reviewer` subagent, generated with
  `[agents.claude.reviewer]` model and effort (default `opus`, `high`) and the
  JSON contract of `pisar lesson review`. Install any further plugins of your
  own inside the isolated configuration directory.

In an interactive session a command outside the allowlist asks for confirmation;
non-interactively it is refused. The allowlist narrows what the agent may do, it
is not a sandbox: it can still read and edit files in the repository.

## Read, discover, and validate

```sh
pisar spaces --include personal
pisar search 'релиз' --include work --space work/alpha
pisar read wiki:work/alpha:launch-call
pisar read work/team/alpha/sources/meetings/launch-call/transcript.md --include work
pisar inventory --exclude personal --space work/alpha
pisar check
```

The IDs and paths above are synthetic examples. Discover your actual addresses
with `spaces`. Every read command (`spaces`, `check`, `inventory`, `search`,
`read`) selects domains with `--include a,b` (only these) and `--exclude x,y`
(all except these). Both are comma separated and repeatable; neither means all
domains; `--exclude` applies after `--include`; an unknown domain id is an error.
A directory with a broken marker still counts as a domain for selection. Invalid
markers and spaces block only the read commands whose selection includes their
domain; writes (`capture`, `save`, `triage`) require every domain to be valid.
`search` and `inventory` support optional `--space DOMAIN/ID`. Related meetings
appear for each related space but retain their owner's single canonical reference.

### Domains

A **domain** is a top-level directory of the root that contains a regular file
`.domain.toml`. Top-level directories without it (docs, schemas, templates, ...)
are not domains and are not searched. A domain may be an ordinary directory or an
initialized Git submodule. The marker:

```toml
schema_version = 1
id = "acme"                 # [a-z0-9][a-z0-9-]*, equals the directory name
title = "Acme Corp"
[layout]                    # optional; kind -> folder inside the domain, defaults shown
project = "10-projects"
area = "20-areas"
resource = "30-resources"
archive = "40-archives"
inbox = "inbox"
[ids]
min_segments = 1            # optional; space ids need >= N dash-separated segments
[sensitive]
aliases = []                # optional; other names of the company
terms = []                  # optional; extra sensitive terms
```

Unknown keys, a wrong type, an `id` different from the directory name, layout
folders that escape the domain or coincide, and symlinked markers or domain
directories are errors reported with the marker's path. `[layout]` and `[ids]`
describe where new spaces belong and how they are named; discovery itself finds
spaces anywhere inside the domain. `personal` is an ordinary domain.

A root made for pisar 0.3 or earlier has no domains until `personal/` and `work/`
each get a `.domain.toml` (ids `personal` and `work`); then rewrite front-matter
`space_ids` and `wiki:` references as addresses. `pisar check` lists what is left.

**Upgrade order.** Pending operations of pisar 0.3 use plain space ids, so first
complete or abandon every pending `save`, `capture` and `triage` batch with 0.3,
and only then migrate. Journals and triage manifests carry a format marker. When
this pisar meets one without it (an old `save` plan id, an old capture of the same
space and id, an old triage batch) it never resumes or reinterprets it: it stops
with a diagnostic naming the state file and writes nothing. To abandon such an
operation explicitly, move that state file out of the state directory, then
review `git status` and `pisar check` for files it already wrote, commit or
discard them yourself, and start the work again with addresses. Limitation: the
history of old completed operations is not carried over; retrying an old
completed capture is reported with the same diagnostic (or, once its state file
was moved away, as a duplicate document id).

The domain is the confidentiality boundary. Related spaces, document and source
references, and in-root sources copied into a space must stay within one domain;
pisar refuses anything that crosses it.

### Spaces, addresses, and references

Discovery walks every domain, including initialized local Git submodules, and
finds `.wiki.toml` files. It requires `schema_version = 1`, a stable `id`,
`kind = "project"|"area"|"resource"`, and `status = "active"|"archived"`. IDs use
`[a-z0-9][a-z0-9-]*` and are unique **within their domain**, archived spaces
included; different domains may reuse an id. A domain directory cannot itself be
a space. A space's **address** is `DOMAIN/ID` (for example `work/alpha`), and
every option, plan field, front-matter field, and triage item that names a space
takes its address. `pisar spaces` reports each space's `domain` and `address`.

The nearest enclosing space owns a document. A stable `wiki:DOMAIN/SPACE:DOCUMENT`
reference resolves by current metadata after moving a space inside its domain;
duplicate IDs cause errors rather than arbitrary selection. Document IDs are
unique **within their owner**. The pre-domain form `wiki:SPACE:DOCUMENT` no longer
resolves; `check` reports each such legacy reference so it can be rewritten.

Documents use TOML front matter between separate `+++` lines:

```toml
+++
schema_version = 1
id = "launch-call"
type = "meeting"
title = "Launch call"
space_ids = ["work/alpha", "work/beta"]
sources = ["sources/meetings/launch-call/transcript.md#00:12"]
occurred_at = "2026-10-04T11:00:00+02:00"
+++
```

`space_ids` holds addresses: the actual owner first, then related spaces in the
same domain.
Supported types: `source`, `meeting`, `overview`, `decision`, `note`, `recipe`,
`movie`, `series`, `book`. Ordinary personal topic knowledge can use `note`.
`sources` is an array, possibly empty. Local source paths are relative to the
owning space; a `#timestamp-or-fragment` is optional. Stable wiki references can
also carry fragments. URLs and `urn:` identifiers are retained as provenance;
their content is not fetched or verified. Unknown dates should be omitted.
Known `occurred_at`, `imported_at`, and `updated_at` must be quoted RFC3339 with
an explicit offset or `Z`. Saved metadata cannot contain template placeholders.

`check` validates actual front-matter documents, IDs, ownership, related spaces,
timestamps, local source existence, body/source wiki references, cross-domain
references, and symlinks. Plain README files and raw sources do not need metadata.
Raw files under `sources/` are opaque originals, even if they resemble front
matter. Metadata descriptors in `inbox/` are checked and discoverable.

Search is explicitly **lexical**: case-insensitive substring matching over
metadata documents, with one matching line per document. It has no semantic
backend and does not claim to infer meaning or prove absence. Inventory performs
full traversal, lists raw source files and metadata documents, and has no top-k
limit. Symlinks, `.git`, and `.ruwana` internals are excluded from traversal.
Files are reread for results; there is no cache or stale evidence index.
Disconnected submodules contribute no unavailable evidence.

## Creating and organising domains and spaces

```sh
pisar domain list
pisar domain add --id acme --title 'Acme Corp'
pisar domain add --id acme --title 'Acme Corp' --repo git@host:acme/wiki.git
pisar space create --domain acme --kind project --title 'Launch Plan'
pisar space find launch --include acme
pisar space move acme/launch-plan --to area
pisar space archive acme/launch-plan
pisar space restore acme/launch-plan
```

Every write here holds the writer lock, requires clean Git trees (the owning
repository and every parent), records a journal in the runtime directory, makes
**one local commit** of explicit paths (inside a submodule first, then the parent
gitlink) and can be retried with the same inputs: a finished operation reports
`"changed": false`, an interrupted one resumes. Results are JSON.

- `domain list` prints each domain's `id`, `title`, `path` and `layout`.
- `domain add --id ID --title T [--layout para|none]` creates `ID/.domain.toml`.
  With `--layout para` (the default) a new, empty or blank directory also gets the
  layout folders (`10-projects`, `20-areas`, `30-resources`, `40-archives`,
  `inbox`) with a `.gitkeep` each; a directory that already holds files only gets
  the marker. Repeating it with the same title is a no-op; another title is an
  error.
- `domain add ... --repo URL_OR_PATH` runs `git submodule add` for the domain
  directory, writes the marker inside the submodule, commits there and then
  commits the parent's `.gitmodules` and gitlink. **This clones over the network
  when the location is a URL: it is the only place pisar itself asks Git to
  reach the network, and only on that explicit command.** A local path is cloned
  with `protocol.file.allow=always` for that one command; nothing else relaxes Git.
  Locations beginning with `-` or containing `::` are refused. The repository
  needs at least one commit (Git cannot add an empty one). If a clone already
  carries a different `.domain.toml`, pisar stops and leaves the submodule for you
  to inspect.
- `space create --domain D --kind project|area|resource --title T [--id ID]`
  derives the id from the title (lowercase ASCII kebab; the kind is never added).
  A title with no ASCII letters or digits needs `--id`. The id must have at least
  the domain's `[ids] min_segments` segments and be unused in the domain,
  **archived spaces included**; a conflict names the owner (`ai: area, archived`)
  and suggests free alternatives. It writes `.wiki.toml` and a README whose H1 is
  the title under `<domain>/<layout.kind>/<id>/`.
- `space find TEXT [--include/--exclude]` matches the id, the README's first H1
  and the optional `aliases = [...]` array of `.wiki.toml`, case-insensitively.
- `space move ADDRESS --to project|area|resource` changes `kind` in `.wiki.toml`
  and `git mv`s the directory to `<layout.to>/<name>` inside the same domain, in
  one commit. Moves across domains do not exist. It refuses a dirty tree, an
  existing target, an archived space (restore first), a space that is itself a
  submodule, and any space an incomplete journaled operation still touches; moving
  to the current kind reports `"changed": false`. `ruwana` tasks live inside the
  directory and move with it.
- `space archive ADDRESS` sets `status = "archived"` and moves the directory to
  `<layout.archive>/`; `space restore ADDRESS` sets it back to `active` and moves
  it to the folder of its (unchanged) kind.

## Known-space inbox capture

```sh
pisar capture --space work/alpha --id capture-launch --source /external/transcript.txt
```

Optional `--sha256 HEX` requires an exact original hash. Capture commits:

- Unchanged bytes at `sources/captures/capture-launch/original`.
- A source descriptor at `inbox/capture-launch.md`, with
  `ingest_status = "pending"`, source hash, original basename, and import time.

Full original paths are recorded only in the external capture journal's
`source_path`, not in new committed descriptors. Existing descriptors are not
automatically rewritten. If the selected source is inside the knowledge root, it
must be inside the destination's domain; same-domain sources and the selected
space inbox are supported. External file classification
remains the agent's responsibility; the CLI does not infer its contents' domain.

The descriptor is the pending space ingestion queue; inspect it through
`inventory --space work/alpha` or `read wiki:work/alpha:capture-launch`. Capture does not
perform semantic processing and does not remove external originals.

A source manually dropped as an **untracked regular file in the target space's
inbox** is supported by the same command. Capture commits that explicitly
selected source along with its immutable copy and descriptor. Every other
untracked, modified, or staged path still blocks the write. Staged/modified
tracked sources are not silently adopted. Finish or preserve that work first.

Rerunning the same space/id/source/hash uses the external operation journal
(`capture-DOMAIN-SPACE-ID-HASH`, where the short hash of the exact address and
capture ID keeps captures with equal dash-joined names apart) and does not create
another commit. Reusing an operation ID with changed inputs is
a conflict. Repeating capture after successful ingest retains the descriptor's
processed status. Capture IDs and meeting document IDs must be distinct.

## Apply an external meeting plan

An external agent reads the actual source and prepares a JSON file **outside all
Git repositories**. The CLI applies that explicit plan; it does not summarize,
infer task agreement, invent assignees, or invent dates. Plans use this contract:

```json
{
  "schema_version": 1,
  "operation_id": "ingest-launch-one",
  "space_id": "work/alpha",
  "capture_id": "capture-launch",
  "source": {
    "path": "/absolute/knowledge/work/team/alpha/sources/captures/capture-launch/original",
    "sha256": "REPLACE_WITH_ACTUAL_64_CHARACTER_SHA256"
  },
  "meeting": {
    "id": "launch-call",
    "title": "Launch call",
    "related_space_ids": ["work/beta"],
    "body": "# Launch call\n\n## Context\nDate/time and participants unknown. Source is linked in metadata.\n\n## Summary\nAgreed to prepare a launch draft.\n\n## Timeline and themes\nRecord the discussion and evidence here.\n\n## Decisions\nPrepare the draft.\n\n## Proposals\nBroader release remains a proposal.\n\n## Tasks at the meeting\nPrepare draft; owner and due unknown.\n\n## Open questions\nLaunch scope.\n\n## Contradictions\nNone recorded.\n\n## Completeness and limitations\nDescribe missing or uncertain evidence.\n"
  },
  "tasks": [
    {"id": "prepare-launch", "space_id": "work/alpha", "title": "Prepare launch draft", "agreed": true},
    {"id": "review-launch", "space_id": "work/beta", "title": "Review launch draft", "agreed": true}
  ],
  "pages": []
}
```

```sh
python3 -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("/external/transcript.txt").read_bytes()).hexdigest())'
pisar --ruwana /absolute/bin/ruwana save --plan /external/launch-plan.json
```

`capture_id` is optional. When present it must identify a pending inbox descriptor
and `source.path` must be its preserved raw source. A successful save marks the
descriptor `processed`, adds `processed_by` and `meeting_reference`, and retains
all original bytes. On ruwana failure it remains pending. For a direct source
save, omit `capture_id` and use an absolute original file path with its actual
hash. Sources inside the selected root must be in the destination's domain;
copies from one domain into another are refused before writes.
The JSON plan itself stays external.

Required meeting fields: `id`, `title`, nonempty filled Markdown `body`.
`related_space_ids` defaults to `[]`; `occurred_at` is optional quoted RFC3339.
The external agent supplies the coherent summary sections; the CLI checks
metadata and references, not whether the narrative accurately represents the
discussion. Do not include `## Ruwana references` in `body`: the CLI generates
that section from verified actual task IDs.

Save preserves bytes at `sources/meetings/MEETING/transcript.md` and creates
exactly one `meetings/MEETING.md`. The CLI creates metadata and the source link.
The initial file commit records task creation as pending; verified task files
and task references follow in further local commits. It never fabricates IDs.

`tasks` defaults to `[]`. Each task requires a unique stable kebab `id`, a
`space_id` address, nonempty `title`, and explicit `agreed: true`. Targets must
be the owner or a related space in the same domain, of any kind (project, area or
resource). Optional `description` preserves
known context; optional `due` is explicit RFC3339 with offset. Omit unknown
owners/dates. Ruwana has no assignee flag; retain known assignees in the meeting
and description. Proposals belong in the meeting body, not in agreed tasks.

Task text follows ruwana 0.1.1 normalization: title edges lose Unicode
`White_Space` characters; internal title spacing stays exact. An omitted, empty,
or whitespace-only description means no description (`null` in ruwana JSON).
Every character of a nonempty description, including leading/trailing whitespace
and Markdown indentation, is preserved and checked on retry. Python-only strip
characters such as U+001C are preserved, matching ruwana. The adapter uses these
same rules for creation and verification; the input plan and its journal
fingerprint stay unchanged. Retry interrupted saves with their original plan,
including saves that created a task before its verification/checkpoint.

Ruwana 0.1.1 is a **day-based** tracker: it stores due at 23:59:59 in the runner's
local timezone. pisar takes the calendar date as written in the plan's `due`
offset (the first `YYYY-MM-DD`), passes that explicit date to ruwana, and checks
the exact resulting local end-of-day and offset. Time-of-day and the input offset
do not turn a task into an instant deadline. For example, under
`TZ=Europe/Warsaw`, both `2026-10-10T12:00:00+02:00` and
`2026-10-10T23:30:00Z` select October 10 and store
`2026-10-10T23:59:59+02:00`; a December date stores `23:59:59+01:00`.
This explicit-date conversion avoids ruwana's natural-language parser shifting a
`Z` timestamp to another date. Preserve any precise deadline in the narrative
and description. Use the same runner timezone and timezone rules on retries
(set `TZ` consistently); a changed date, end-of-day time, or offset is a conflict.

### Ruwana adapter

ruwana's public CLI still selects its repository through the legacy `WIKI_ROOT`
variable. As a documented compatibility exception, pisar sets `WIKI_ROOT` to the
selected root only in the environment of the ruwana child process; pisar itself
never reads `WIKI_ROOT`, and ruwana's interface is used unchanged.

Ruwana runs through `add`, `list --all --format json`, and `show --format json`
with that explicit `WIKI_ROOT` and a root-relative `--project`. Each task gets both the
canonical meeting reference and `wiki:DOMAIN/OWNER:MEETING#task-TASK-ID` as sources.
The task marker distinguishes multiple tasks from one meeting and deduplicates
even closed tasks. Titles/descriptions/due dates are checked on retry; conflicts
and duplicate source identities are errors. `.ruwana/*.toml` is written only by
ruwana, then committed by explicit file paths. Missing binaries, subprocess
failure/warnings, malformed output, missing tasks, and mismatched IDs produce
nonzero failure and an incomplete journal. Retry with the same plan and the
working binary. Current status is authoritative only in ruwana.

Replaying a completed save verifies current files/tasks without writing them or
changing its completed journal. Dirty task status changes are reported; commit
the intended task change, then retry. Later legitimate meeting edits are
reported as conflicts and preserved; they do not demote completed history or
authorize replay to overwrite the edits.

`pages` defaults to `[]`. An explicitly requested derived page uses:

```json
{
  "space_id": "work/alpha",
  "path": "overview.md",
  "content": "FULL_MARKDOWN_WITH_VALID_TOML_FRONT_MATTER",
  "base_sha256": "ACTUAL_HASH_OF_EXISTING_PAGE"
}
```

Paths are relative to that page's selected space. To create a new file, omit
`base_sha256`; existing destinations then conflict. To update a page, supply its
exact current hash and retain its stable ID. Sources, inbox originals, meetings,
and task files cannot be rewritten as derived pages. Metadata, paths, IDs, domains,
and references are validated before applying any proposed files. Only reusable
knowledge explicitly included in `pages` is updated.

## Global inbox triage

The runtime is selected by `--state-dir`, `PISAR_STATE_DIR`, or the default
`$XDG_DATA_HOME/pisar` (see Configuration). The global inbox is `<runtime>/inbox`. Store routing JSON, agent drafts, journals, and all
pending originals outside every Git repository. `--state-dir` overrides runtime
for every operation, including triage and processed originals.

Snapshot an external inbox without semantic routing:

```sh
pisar triage prepare --batch morning-one
pisar triage report --batch morning-one
pisar triage reroute --batch morning-one --item item-ACTUAL-ID --space work/alpha
pisar triage accept --batch morning-one --item item-ACTUAL-ID
```

`prepare --inbox /external/inbox` selects another external inbox. Only immediate
regular files are accepted. The snapshot has generated stable IDs; late arrivals
belong to the next batch. Unknown destinations remain pending. No semantic
runner or nightly service is installed or started.

Alternatively, use an external agent's routing JSON:

```json
{
  "schema_version": 1,
  "items": [
    {
      "id": "routed-launch",
      "source": "/external/transcript.txt",
      "sha256": "ACTUAL_64_CHARACTER_SHA256",
      "space_id": "work/alpha"
    }
  ]
}
```

```sh
pisar triage prepare --batch morning-one --plan /external/routing.json
pisar triage report --batch morning-one
pisar triage defer --batch morning-one --item routed-launch
pisar triage reroute --batch morning-one --item routed-launch --space work/beta
pisar triage accept --batch morning-one --item routed-launch
```

`space_id` is a space address and can be `null` until explicit reroute. A routing item can include
`plan` containing the meeting-plan object above, with matching owner/source/hash
and no `capture_id`. Prepare snapshots it privately; accept captures the original,
assigns `capture_id`, and applies the plan. Item ID and meeting ID must differ.
Changing destination through reroute discards the old semantic plan and records
that fact in the report; the captured item then awaits fresh processing in its
new owner's inbox. It does not transfer old related pages/tasks across domains.

Before accept, `<runtime>/triage/BATCH/` holds `manifest.json`, `report.md`, raw
snapshots, and proposed draft JSON. Nothing is written into any destination
repository, including uncommitted drafts. Duplicate item IDs, source paths, or
identical source hashes within a batch are rejected. The manifest stores the
explicit root namespace; another knowledge root cannot use it.

Only `accept --item ID` applies that item and commits its intended paths. Source
and draft hashes are revalidated; ambiguous or changed inputs stay unapplied.
`defer` leaves an item external; explicit reroute reopens a deferred item. Once
application has started, reroute/defer refuse: retry accept or diagnose the
journal instead. One failed item does not undo accepted items or apply siblings.
This restriction includes **incomplete** items: their earlier commits/tasks may
already exist. Restore the required inputs/dependency and retry the same accept.
There is no automatic abandonment command. To abandon, inspect the recorded
partial commits and perform a deliberate task-preserving rollback, keeping the
external journal/originals for diagnosis. Changing a plan or operation ID cannot
bypass an already-created meeting ID; do not delete the journal to force it.

After verified completion, the original is copied and hash-verified at
`<runtime>/processed/BATCH/ITEM/original`. For inbox-directory snapshots only,
the verified inbox original is then removed; bytes remain in processed,
the batch snapshot, and the destination source. Explicit files from routing
plans are never removed. Crash recovery after removal checks the durable capture
identity and processed bytes before finishing the manifest; it does not recapture
under a changed path. Repeating accept verifies completion and does not duplicate
commits or tasks. Keep pending originals and runtime state in your backup.

## Write safety, recovery, and rollback

Writes require the knowledge root to be its own Git top-level. They reject absolute
destination paths, `..`, symlinks, Git internals, destination ownership changes,
ignored files, duplicate IDs, stale source hashes, and changed base hashes.
Unrelated dirty/staged paths block a write; no reset, stash, `git add .`, remote,
or publication is used. Git subprocesses ignore inherited `GIT_*` overrides;
commits disable hooks and signing to avoid running publication scripts.

An initialized registered submodule receives its data commit first. Parent
gitlink updates are separate commits with the same operation ID, including
nested parent chains. Partial completion is recorded; this is not an atomic
transaction across repositories or ruwana. Unregistered nested repositories
cannot be written through the root.

Journals live at `<runtime>/roots/ROOT-HASH/operations/OPERATION.json`; root hashes
namespace state rather than serving as a committed registry. Journals contain
planned artifact bytes/hashes, task identities, Git HEAD checkpoints, commit IDs,
status, and errors. Runtime paths and their ancestors are checked for Git
repositories and symlinks, including bare repositories. Files use atomic replace
and file fsync; full power-loss durability across Git, directory entries, and
ruwana is not guaranteed.

A nonblocking local writer lock serializes pisar operations for the selected
root/runtime. Use one root and one runtime configuration for all writers to that
root, and one writer per space. Direct ruwana/Git/external editors do not
participate in the lock and ruwana has no compare-and-swap. Do not change those
files during an incomplete operation. Unexpected HEAD/file changes cause
conflicts. Retry can recognize its own Git commit made before a missing journal
checkpoint, and its own ruwana task created before a missing task checkpoint.
Concurrent hostile filesystem replacement is outside this local safety model.

To diagnose, read the external operation journal and triage report, then run
`git -C /absolute/owner status --short` and inspect the recorded commits. Fix the
reported external dependency or restore the exact required input; rerun the same
command with the same stable operation ID. Do not delete incomplete journals to
force through a conflict.

For an older incomplete save whose task was created before a due-verification
failure, inspect it with public `ruwana list --all` and `show --format json`.
If its date differs from the documented plan date, reconcile it deliberately
through `ruwana edit TASK --project ROOT_RELATIVE_PROJECT --due=YYYY-MM-DD`
under the chosen runner `TZ`, retaining its stable source markers. Retry the
unchanged plan before making separate Git commits to the incomplete operation's
owned paths; unexpected HEAD changes remain conflicts. The CLI does not silently
accept an arbitrary conflicting due value.

## Task-preserving rollback

Do **not** revert complete operation commits wholesale: commits containing
`.ruwana/*.toml` can delete tasks or restore an older status. First diagnose any
incomplete operation, preserve dirty work, and inspect its journal's commits.
Task fate is a separate decision through the public ruwana CLI, with explicit
`WIKI_ROOT` and a **root-relative project**. If changing task status, commit its
exact task file in the **owning repository**, where its Git path may differ from
that root-relative project. A submodule task commit needs a separate parent
gitlink commit. Leave existing task files and status intact during rollback.

Set `OWNER_REPO` to an absolute owning-repository path and `OPERATION` to the
recorded operation ID, then run the following in Bash. It stages inverse patches
only for knowledge files; task files and all current submodule gitlinks are
excluded. It stops on dirty trees or a conflicting later knowledge edit. Review
the staged diff before committing.

```sh
set -euo pipefail
: "${OWNER_REPO:?Set the absolute owning repository path}"
: "${OPERATION:?Set the recorded operation id}"
cd "$OWNER_REPO"
test -z "$(git status --porcelain)"
git log --format='%h %s' --grep="^wiki: $OPERATION\$"
excludes=(':(exclude,glob)**/.ruwana/**' ':(exclude,literal).ruwana')
while IFS= read -r -d '' entry; do
  if [[ "$entry" == '160000 '* ]]; then
    excludes+=(":(exclude,literal)${entry#*$'\t'}")
  fi
done < <(git ls-files --stage -z)
for sha in $(git log --format=%H --grep="^wiki: $OPERATION\$"); do
  git show --format= --binary "$sha" -- . "${excludes[@]}" \
    | git apply -R --index --allow-empty
done
git diff --cached --stat
```

Inspect `git diff --cached`, then commit only the exact staged knowledge paths
as a new rollback commit. **Skip the commit when the staged diff is empty**:
a task-only owner can legitimately have no knowledge to roll back. If an inverse
patch fails, nothing is committed;
restore only the explicitly staged rollback paths to HEAD using `git restore
--source=HEAD --staged --worktree -- PATH...`, then reconcile the later knowledge
edit deliberately. Start only from a clean tree; do not reset unrelated work.

For submodules, run this procedure inside each affected child first. After
committing the child's knowledge rollback, explicitly stage and commit its
**new child HEAD** at the parent-relative gitlink path. This makes the parent
clean before running its own recipe, which excludes all gitlinks. Repeat outward
for nested submodules. If the child rollback staged no knowledge and made no
commit, leave its current HEAD/gitlink alone; do not make an empty gitlink commit.
For a root-owned meeting with tasks in a child, the task-only child therefore
stays intact while the root recipe removes only root knowledge and preserves
every gitlink. Verify the parent's recorded child SHA against the child's
HEAD, both statuses, `pisar check`, and `ruwana show --format json`/`list --all`.
Task status and history stay as they were; kept tasks may still cite a rolled-back
meeting, so reconcile their source context deliberately in ruwana. External
journals remain historical records. Rollback is not replay of a completed plan;
Git is not a backup of submodules or external inbox originals.

## Guard: report possibly sensitive text

```sh
pisar guard check --file /abs/draft.md --from acme --to personal
pisar guard check --text 'How do others size a launch team?' --outbound
```

`guard check` scans a UTF-8 file or a text and prints
`{"findings": [{id, tier, category, line, excerpt, hint}], "limits": "..."}`.
It exits 0 whenever the scan ran: findings are information, not errors. Unknown
domains, unreadable input and malformed `.domain.toml` files exit 1. Pass text
that starts with a dash as `--text=VALUE`. Lesson operations and outbound
research queries use the guard; capture, save and other writes never call it.

| Tier | Categories |
| --- | --- |
| `severe` | `private-key` (PEM blocks), `api-token` (cloud/API token patterns), `jwt`, `credential` (`password`/`secret`/`token`/`api_key` `=` or `:` value), `email`, `phone`, `code-block` (fenced code: possible code or architecture) |
| `ask` | `quote` (blockquotes, quoted strings of 30+ characters and 4+ words), `meeting` (line-leading timestamps, `Speaker:` lines), `url`, `hostname`, `ip` |
| `warn` | `term` (terms of the source domain), `date` (dates and times with day or finer precision) |

Amounts and metrics are not reported. Terms are the domain `id` and `title`,
`[sensitive] aliases` and `terms` from `.domain.toml`, and the `.wiki.toml` ids
and README H1 titles of the spaces under the domain. They match
case-insensitively on word boundaries, ignoring terms shorter than three
characters. Only the `--from` domain is used; `--outbound` (research queries
leaving the machine) uses every domain. `--to` names the destination domain
and must exist.

A finding `id` is the first 10 hex digits of
`sha256(category|normalized-match|occurrence)`, where the normalized match is the
matched text case-folded with whitespace collapsed and the occurrence counts equal
matches from the top. It stays the same across runs and across edits elsewhere in
the text. The `excerpt` is the first line of the match, at most 80 characters.

The guard is deterministic, lexical and **best effort**. It misses paraphrases,
misspellings, inflected or unlisted names, images and encoded content, and it can
flag harmless text. It only reports; it never edits, strips or redacts anything,
and the input file is only read. Decide on each finding with the user.

## Lessons: carry a reviewed insight into a space

```sh
pisar lesson start --from acme --to personal/career-notes --title 'Sizing a launch team'
pisar lesson check --batch lesson-3fa9c1d2b4
pisar lesson review --batch lesson-3fa9c1d2b4 --file /abs/review.json
pisar lesson show --batch lesson-3fa9c1d2b4
pisar lesson accept --batch lesson-3fa9c1d2b4 [--keep F1,F2] [--mention-origin] [--skip-review]
pisar lesson discard --batch lesson-3fa9c1d2b4
```

A lesson is a general insight written from experience in one domain (`--from`, a
domain id or a space address) and filed as a `note` in a space of any domain
(`--to`, an address). The guard and a reviewer only report; the agent edits the
draft after asking the user; `accept` writes exactly what was reviewed.

The batch lives outside Git and outside the wiki, in `<state>/lessons/<batch>/`:
`draft.md` (edited freely), `meta.json`, `revisions/rN.md`, `rN.report.json`,
`rN.review.json` and `journal.json`. Batch ids are generated and unique. A state
directory inside the wiki root or any Git repository is refused.

- `check` snapshots `draft.md` as the next immutable revision (sha256 recorded;
  an unchanged draft keeps the current revision and its review), runs the guard
  with the terms of the `--from` domain, stores the report with stable finding
  ids and prints the findings and the diff against the previous revision.
- `review` attaches reviewer JSON to the **current** revision:

  ```json
  {"schema_version": 1, "revision": "r2", "sha256": "<sha256 of that revision>",
   "verdict": "clear|concerns|block",
   "findings": [{"tier": "severe|ask|warn", "category": "...", "excerpt": "...",
                 "comment": "...", "suggestion": "..."}],
   "reviewer": {"model": "..."}}
  ```

  `check` prints the current `revision` and `sha256` as JSON fields and `show`
  prints them as the lines `revision: rN` and `sha256: <hex>`; a reviewer copies
  both into its JSON. A file for another revision or hash is refused as stale. The verdict is
  advisory: even `block` does not stop `accept` once the user decided.
- `show` prints a human-readable report: destination, current text, findings by
  tier, suggested generalisations, the review, open decisions and the diff.
- `accept` refuses unless the current revision has a review (`--skip-review`
  accepts without one and is recorded), `draft.md` equals the current revision,
  and every finding of the current revision is decided: removed by a later edit,
  or listed in `--keep` (the user's explicit decision; unknown ids are refused).
  It writes exactly the reviewed text as `notes/<title-slug>.md` with valid front
  matter (`type = "note"`, `sources = []`) and no reference to the origin;
  `--mention-origin` adds only a plain-text `Origin: <domain title>` line. The
  write uses the normal machinery: writer lock, journal (operation id equals the
  batch id), commit of explicit paths, submodules committed before their parent.
  Keeps, skip and mention are recorded in the batch journal. Rerunning a
  completed `accept` verifies and writes nothing; changed flags are refused.
- `discard` removes the workspace and keeps the journal under the runtime
  directory (`roots/<id>/lessons/<batch>.json`).

Source and target may be any domains. Across domains, `start`, `check`, `show`
and `accept` print a prominent `CROSS-DOMAIN` warning; it never blocks, but the
decision to proceed is the user's. Nothing is silently stripped: the guard is a
best-effort lexical scan and the reviewer is a model, so neither guarantees
confidentiality.

## Research through an isolated web researcher

```sh
pisar research "How do teams run a short launch review?" [--from DOMAIN] \
    [--model M] [--effort E] [--confirm-outbound]
```

`research` is the **second exception to "offline"** after `domain add --repo`:
it starts an isolated `claude -p` that can only use `WebSearch` and `WebFetch`,
so the question leaves the machine. pisar never contacts a service itself.

- **Guard first.** The question is scanned with the outbound guard (secrets,
  URLs, code, quotes and the terms of every domain). Findings are never
  stripped or rewritten. Without `--confirm-outbound` the command stops before
  starting anything, prints the findings and exits 1; the agent shows them to
  the user and reruns with the flag only on their decision. The flag is
  recorded in the stored record. `--from DOMAIN` names the domain the
  question comes from and must exist.
- **Isolation.** The subprocess gets exactly `--tools=WebSearch,WebFetch`
  (also the only allowed tools), `--strict-mcp-config` without any MCP file,
  `--no-session-persistence`, a replaced researcher system prompt that treats
  page content as untrusted data, a fresh temporary working directory that is
  removed afterwards, and a minimal environment (below). It
  shares the launcher's configuration directory `<state>/agents/claude` (same
  login), validated like the launcher does: outside Git, no symlinks. The
  question is sent on standard input, never as an argument.
- **Environment allowlist.** The subprocess receives only these variables, by
  exact name (no `ANTHROPIC_*`/`CLAUDE_CODE_*` prefix match, so unrelated or
  session variables of a parent Claude Code never pass): `PATH`, `HOME`,
  `USER`, `LOGNAME`, `TERM`, and the locale variables `LANG`, `LANGUAGE`,
  `LC_ALL`, `LC_CTYPE`, `LC_NUMERIC`, `LC_TIME`, `LC_COLLATE`, `LC_MONETARY`,
  `LC_MESSAGES`, `LC_PAPER`, `LC_NAME`, `LC_ADDRESS`, `LC_TELEPHONE`,
  `LC_MEASUREMENT`, `LC_IDENTIFICATION` (named one by one, no prefix match); the proxy variables
  (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` and lowercase forms); the certificate
  variables (`SSL_CERT_FILE`, `SSL_CERT_DIR`, `NODE_EXTRA_CA_CERTS`,
  `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`); Anthropic credentials, endpoint and
  models (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
  `ANTHROPIC_CUSTOM_HEADERS`, `ANTHROPIC_MODEL`,
  `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`,
  `CLAUDE_CODE_OAUTH_TOKEN`); and the provider selectors
  `CLAUDE_CODE_USE_BEDROCK|VERTEX|FOUNDRY`. Cloud credentials pass only for the
  provider that is selected (value `1`, `true`, `yes` or `on`): Bedrock
  `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`,
  `AWS_PROFILE`, `AWS_REGION`, `AWS_DEFAULT_REGION`, `AWS_BEARER_TOKEN_BEDROCK`,
  `ANTHROPIC_BEDROCK_BASE_URL`; Vertex `GOOGLE_APPLICATION_CREDENTIALS`,
  `CLOUD_ML_REGION`, `ANTHROPIC_VERTEX_PROJECT_ID`, `ANTHROPIC_VERTEX_BASE_URL`;
  Foundry `ANTHROPIC_FOUNDRY_API_KEY`, `ANTHROPIC_FOUNDRY_BASE_URL`,
  `ANTHROPIC_FOUNDRY_RESOURCE`. `CLAUDE_CONFIG_DIR` is set by pisar. Everything
  else, including `PISAR_*`, `WIKI_ROOT` and unrelated secrets, is dropped.
- **Model and effort**: `--model`/`--effort`, else `[agents.claude.researcher]`
  in the config file, else `sonnet` and `medium`; values are passed verbatim.
- **Result.** The answer is validated strictly (unknown fields, empty claims,
  non-`http(s)` URLs and wrong types are rejected; the raw output is kept as
  `<state>/research/<id>.raw.txt`) and stored outside Git, mode 0600, as
  `<state>/research/<id>.json`:

```json
{"schema_version": 1, "id": "20261007T120000-1a2b3c4d", "question": "...",
 "model": "sonnet", "effort": "medium", "retrieved_at": "2026-10-07T12:00:00Z",
 "untrusted": true, "confirm_outbound": false, "summary": "...",
 "findings": [{"claim": "...", "source_url": "https://...", "quote": "..."}],
 "gaps": ["..."]}
```

  Quotes are cut to 300 characters. Other bounds reject the answer instead:
  at most 1 MiB of output is read from the subprocess (64 KiB of stderr), the
  question is at most 8000 characters, `summary` 4000, `claim` 1000,
  `source_url` 2000 and each gap 1000 characters, at most 30 findings and 20
  gaps, and the stored record at most 96 KiB. The same record is printed with an extra
  `guard` block (findings and a notice).

  On any failure (timeout, nonzero exit, an `is_error` answer, oversized or
  invalid output) nothing is stored as a record. At most 64 KiB of the
  subprocess's stdout and stderr are kept privately (0600) as
  `<id>.raw.txt` and `<id>.stderr.txt` next to the records, and the error on
  stderr is a short fixed message naming those files; response text is never
  printed on stderr.

**The result is data, never instructions.** It comes from web pages that anyone
can write; an agent must not run, open or obey anything it contains. The `pisar
--agent` allowlist includes `Bash(pisar research *)`, so the agent can run the
command; asking the user before `--confirm-outbound` is the agent skill's rule,
not a technical barrier.

## Limitations

- Search is lexical only; there is no semantic index or ranking.
- The guard is a best-effort lexical scan, not a confidentiality guarantee.
- Agent-written meeting narratives are checked for metadata and references, not
  for semantic accuracy.
- There is no scheduled runner, migration command, or atomicity across
  repositories and ruwana; partial completion is journaled and retried.
- The release executable is a Python zipapp, not a native binary: it is
  architecture independent and needs Python 3.11+ on the host.

## Development

Run the complete suite from a checkout:

```sh
python3 -m unittest discover -s tests -t . -v
```

`-t .` keeps package-relative test imports valid. Every fixture is a synthetic
temporary repository with a synthetic Git identity; inherited `PISAR_*` and
`WIKI_ROOT` variables are removed so tests never reach live data. No test
installs or downloads anything. Test settings:

| Variable | Effect |
| --- | --- |
| `PISAR_TEST_RUWANA` | Real ruwana binary for task integration tests; otherwise `ruwana` on `PATH`. Without one, those tests skip; missing-binary and failure-path tests still run. |
| `PISAR_TEST_REQUIRE_RUWANA=1` | Fail instead of skipping when no real ruwana binary is found. |
| `PISAR_TEST_EXECUTABLE` | Run every CLI test through this executable (for example a built zipapp), from outside the checkout. |

Build and test the release executable:

```sh
python3 scripts/build-zipapp.py dist/pisar
PISAR_TEST_EXECUTABLE="$PWD/dist/pisar" python3 -m unittest discover -s tests -t .
scripts/package-release.sh "$(dist/pisar --version | cut -d' ' -f2)" dist/pisar dist
```

The zipapp embeds the version from `pyproject.toml`, the single version
authority, and is byte-for-byte reproducible for a given `SOURCE_DATE_EPOCH`
(default: the last commit time). The release archive is reproducible too.

Commits follow [Conventional Commits](https://www.conventionalcommits.org).
[release-please](https://github.com/googleapis/release-please) maintains the
version, changelog and a release pull request. Merging it creates a draft release
and tag; the release workflow resolves the tag once to a commit SHA, checks that
the tag still names it before building and publishing, verifies that commit on
Python 3.11 and 3.14, builds the zipapp and archive, tests the packaged
executable outside the checkout, writes `SHA256SUMS`, attests build provenance,
uploads and re-downloads the assets to verify them, and only then publishes the
release as latest and fast-forwards the `stable` branch. A failed run can be retried for an existing
draft release with the workflow's `tag` input; that recovery run never runs
release-please and refuses tags whose release is already published.

This repository does not include a license yet.
