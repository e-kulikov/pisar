---
name: lessons
description: Turn what was learned in one domain into a reusable note in another domain, safely. Use when the user wants to carry a lesson, pattern or recommendation across a confidentiality boundary, or asks for a "lesson" to be written from work material.
---

# Lessons: carrying knowledge across domains

A domain is a confidentiality boundary. A lesson is a short, general note that
helps in a different domain without leaking anything specific from the first.
The workflow is mechanical and journaled; your job is the judgement: write
generally, show the user what was found, and edit only what the user decided.

## Rules

- Never strip, rewrite or hide text on your own to make a finding disappear.
  Findings are information. Show them, ask, then edit the draft only as the
  user said. If the user wants a finding kept, record that with `--keep`.
- Severe findings (secrets, client data such as emails or phone numbers,
  fenced code): warn strongly and say why it is risky. Ask at most twice. If the
  user explicitly insists after the second time, proceed and record the
  decision with `--keep`. Do not ask a third time.
- The guard is lexical and best effort. A clean report does not prove the text
  is safe; the reviewer and the user still decide.
- Between two different companies, say so prominently before you start.
- Accepting is the user's act. `pisar lesson accept` always asks for
  confirmation; never try to bypass it. Use `--skip-review` only when the user
  explicitly asks for it.

## Steps

1. Understand what generalises. Write the lesson in your own words, without
   names, quotes, numbers or paths from the source.
2. `pisar lesson start --from DOMAIN_OR_ADDRESS --to ADDRESS --title "TITLE"`
   prints the batch id and the path of `draft.md`. Write the note into that
   file with your edit tool (only the lessons workspace is writable).
3. `pisar lesson check --batch ID` snapshots the draft as a revision and prints
   the findings by tier with a hint for each.
4. Present the findings briefly to the user: what, where, and a suggested
   generalisation. Apply only the edits the user chooses, then run
   `pisar lesson check --batch ID` again. A finding counts as decided when the
   text changed in a later revision or when the user chose `--keep`.
5. Review is mandatory: start the `lesson-reviewer` subagent on the current
   revision. Give it the revision id, its text and its sha256 (shown by
   `pisar lesson show --batch ID`). It answers with JSON only. Save that JSON
   to a file in the batch directory and run
   `pisar lesson review --batch ID --file FILE`. A stale review (the draft
   changed since) is rejected: check again and re-review. The verdict is advice;
   `block` and `concerns` are shown to the user, who decides.
6. `pisar lesson show --batch ID` gives the full report: destination, text,
   findings by tier, suggestions, review verdict and open decisions.
7. When the user agrees, run
   `pisar lesson accept --batch ID [--keep F1,F2] [--mention-origin]`.
   The note is written exactly as reviewed, as a `note` in the target space.
   It does not link to its origin unless `--mention-origin` is given.
8. If the user gives up, `pisar lesson discard --batch ID`.

Reply in the language the user writes in. Be brief and concrete.
