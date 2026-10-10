"""The bundled skills for the Codex agent, written into its isolated CODEX_HOME.

Codex reads skills from ``$CODEX_HOME/skills/<name>/SKILL.md``; there is no plugin
directory like Claude's ``--plugin-dir``, and subagents are replaced by MCP tools
(see mcp.py and codex_reviewer.py). The texts are the Claude plugin's skills, adapted
where the mechanism differs: the lesson reviewer is the ``lesson_review`` tool, and
accepting a lesson is the ``lesson_accept`` tool. Every launch rewrites the files with
one atomic replace each; they hold no state and nothing else in CODEX_HOME is touched.
"""
import os
from pathlib import Path
from importlib.resources import files
from .safety import WikiError

SKILLS = ('pisar', 'lessons', 'research')
CLAUDE_SKILLS = 'plugin/claude/skills'
TOOLS = '''## Tools in this session

You have no shell and no file tools. Run pisar with the `pisar` tool: `args` is
the list that follows `pisar`, for example `["search", "--space", "work/alpha", "launch"]`.
Read documents with the `pisar` tool (`read`), and change them only through pisar.
Run git with the `git` tool: `args` starts with the subcommand, for example
`["mv", "old.md", "new.md"]`. Draft a lesson with `lesson_draft_read` and
`lesson_draft_write` (pass the sha256 you last read), not by editing draft.md.
Accept a lesson only with the `lesson_accept` tool; it asks the user to confirm.
Review a lesson revision with the `lesson_review` tool.
'''
REVIEW_STEP = '''5. Review is mandatory: call the `lesson_review` tool with `["--batch", "ID"]`.
   It sends the current revision (its text and sha256) to the senior reviewer,
   checks the answer and records it. A stale review (the draft changed since) is
   rejected: check again and review again. The verdict is advice; `block` and
   `concerns` are shown to the user, who decides. Use `pisar lesson show --batch ID`
   for the revision, its verdict and open decisions.
'''
ACCEPT_STEP = '''7. When the user agrees, call the `lesson_accept` tool with
   `["--batch", "ID", "--keep", "F1,F2", "--mention-origin"]` (leave out what does
   not apply). The note is written exactly as reviewed, as a `note` in the target
   space. It does not link to its origin unless `--mention-origin` is given.
'''


def _resource(name):
    return files('pisar').joinpath(name).read_text(encoding='utf-8')


def _replace_block(text, start, end, block):
    """BLOCK in place of the text from the line starting with START up to the line starting with END."""
    head, found, rest = text.partition(start)
    if not found:
        raise WikiError(f'bundled skill changed: no {start.strip()!r}')
    _, marker, tail = rest.partition(end)
    if not marker:
        raise WikiError(f'bundled skill changed: no {end.strip()!r}')
    return head + block + marker + tail


def _lessons(text):
    draft = 'Write the note into that\n   file with your edit tool (only the lessons workspace is writable).'
    if draft not in text:
        raise WikiError('bundled skill changed: draft step')
    text = text.replace(draft, 'Write the note with the `lesson_draft_write` tool, after reading it with\n   `lesson_draft_read`.')
    text = _replace_block(text, '5. Review is mandatory:', '6. `pisar lesson show', REVIEW_STEP)
    text = _replace_block(text, '7. When the user agrees, run', '8. If the user gives up', ACCEPT_STEP)
    if "act. `pisar lesson accept` always asks for" not in text:
        raise WikiError('bundled skill changed: lesson accept rule')
    return text.replace("act. `pisar lesson accept` always asks for", "act. The `lesson_accept` tool always asks for")


def _with_tools(text):
    """The skill with the tools section after its frontmatter."""
    _, sep, body = text.partition('\n---\n')
    if not sep:
        raise WikiError('bundled skill has no frontmatter')
    head = text[:len(text) - len(body)]
    return head + '\n' + TOOLS + '\n' + body.lstrip('\n')


def skill_texts():
    """{name: SKILL.md text} for the Codex agent."""
    return {
        'pisar': _with_tools(_resource('skill.md')),
        'lessons': _with_tools(_lessons(_resource(f'{CLAUDE_SKILLS}/lessons/SKILL.md'))),
        'research': _with_tools(_resource(f'{CLAUDE_SKILLS}/research/SKILL.md')),
    }


def _atomic(path, data):
    temp = path.with_name(f'.{path.name}.tmp')
    temp.write_text(data, encoding='utf-8')
    os.chmod(temp, 0o644)
    os.replace(temp, path)


def write_skills(config):
    """Write the skills into CONFIG/skills; returns the skills directory."""
    base = Path(config) / 'skills'
    for name, text in skill_texts().items():
        directory = base / name
        directory.mkdir(parents=True, exist_ok=True)
        _atomic(directory / 'SKILL.md', text)
    return base
