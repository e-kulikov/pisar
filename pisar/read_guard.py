"""PreToolUse read restriction guard for Claude Code.

Intercepts Read, Glob, and Grep tool calls to ensure pisar data and state are read
only through pisar commands, while permitting access to the active plugin generation
and lesson workspace.
"""
import glob
import json
import os
from pathlib import Path
import sys

WIKI_HINT = (
    'pisar data must be read through pisar: use pisar read <root-relative-path>, '
    'pisar search <query>, or pisar inventory.'
)
STATE_HINT = (
    'pisar state must be read through pisar: use pisar lesson show --batch ID '
    'for lesson drafts or pisar inbox read --id ID for global inbox items.'
)


def _is_ancestor(ancestor: Path, descendant: Path) -> bool:
    try:
        return descendant.is_relative_to(ancestor) and ancestor != descendant
    except (ValueError, RuntimeError):
        return False


def run(root: str | Path, state_dir: str | Path, plugin_dir: str | Path) -> int:
    try:
        raw_input = sys.stdin.read()
        try:
            payload = json.loads(raw_input)
        except Exception as error:
            sys.stderr.write(f'read guard: invalid json: {error}\n')
            return 2

        if not isinstance(payload, dict):
            sys.stderr.write('read guard: payload must be a json object\n')
            return 2

        tool_name = payload.get('tool_name')
        if tool_name not in ('Read', 'Glob', 'Grep'):
            sys.stderr.write(f'read guard: unexpected tool_name {tool_name!r}\n')
            return 2

        tool_input = payload.get('tool_input')
        if not isinstance(tool_input, dict):
            sys.stderr.write('read guard: tool_input must be a dictionary\n')
            return 2

        cwd = payload.get('cwd')
        if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd) or not os.path.isdir(cwd):
            sys.stderr.write('read guard: cwd must be an existing absolute directory\n')
            return 2

        canonical_root = Path(root).resolve()
        canonical_state = Path(state_dir).resolve()

        plugin_path = Path(plugin_dir)
        canonical_plugin = plugin_path.resolve()

        chk = plugin_path
        has_symlink = False
        while chk != canonical_state and chk != chk.parent:
            if chk.is_symlink():
                has_symlink = True
                break
            chk = chk.parent

        expected_plugin_parent = (canonical_state / 'agents' / 'claude' / 'plugin').resolve()
        if (has_symlink or plugin_path.is_symlink() or not plugin_path.is_dir() or
                not canonical_plugin.is_relative_to(canonical_state) or
                canonical_plugin == canonical_state or
                canonical_plugin.parent != expected_plugin_parent):
            canonical_plugin = None

        lessons_path = canonical_state / 'lessons'
        if lessons_path.is_symlink() or not lessons_path.is_dir() or lessons_path.resolve() != lessons_path:
            canonical_lessons = None
        else:
            canonical_lessons = lessons_path

        if tool_name == 'Read':
            file_path = tool_input.get('file_path')
            if not isinstance(file_path, str) or not file_path:
                sys.stderr.write('read guard: file_path must be a non-empty string\n')
                return 2
            raw_target = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)
        elif tool_name == 'Glob':
            pattern = tool_input.get('pattern')
            if not isinstance(pattern, str):
                sys.stderr.write('read guard: pattern must be a string\n')
                return 2
            if pattern.startswith('/') or pattern.startswith('~') or '..' in pattern:
                sys.stderr.write('read guard: forbidden glob pattern\n')
                return 2
            search_path = tool_input.get('path')
            if search_path is not None and (not isinstance(search_path, str) or not search_path):
                sys.stderr.write('read guard: path must be a non-empty string\n')
                return 2
            if search_path is None:
                raw_target = cwd
            elif os.path.isabs(search_path):
                raw_target = search_path
            else:
                raw_target = os.path.join(cwd, search_path)
            if os.path.isdir(raw_target):
                candidates = set()
                try:
                    for m in glob.glob(pattern, root_dir=raw_target, recursive=True, include_hidden=True):
                        candidates.add(m)
                except Exception:
                    pass
                if '/' in pattern:
                    pattern_dir = os.path.dirname(pattern)
                    try:
                        for d in glob.glob(pattern_dir, root_dir=raw_target, recursive=True, include_hidden=True):
                            candidates.add(d)
                    except Exception:
                        pass

                for m in candidates:
                    curr = Path(raw_target) / m
                    while curr != Path(raw_target) and curr != curr.parent:
                        resolved_m = curr.resolve()
                        lexical_m = Path(os.path.abspath(curr))

                        # 1. Wiki target traversed
                        if resolved_m.is_relative_to(canonical_root) or lexical_m.is_relative_to(canonical_root):
                            sys.stderr.write(f'{WIKI_HINT}\n')
                            return 2

                        # 2. Check if search began in plugin
                        if canonical_plugin is not None and Path(os.path.realpath(raw_target)).is_relative_to(canonical_plugin):
                            if not resolved_m.is_relative_to(canonical_plugin):
                                if resolved_m.is_relative_to(canonical_state):
                                    sys.stderr.write(f'{STATE_HINT}\n')
                                    return 2
                                sys.stderr.write('read guard: glob pattern escapes plugin directory\n')
                                return 2

                        # 3. State target traversed (outside plugin)
                        if resolved_m.is_relative_to(canonical_state) or lexical_m.is_relative_to(canonical_state):
                            if canonical_plugin is None or not resolved_m.is_relative_to(canonical_plugin):
                                sys.stderr.write(f'{STATE_HINT}\n')
                                return 2

                        curr = curr.parent
        elif tool_name == 'Grep':
            pattern = tool_input.get('pattern')
            if pattern is not None and not isinstance(pattern, str):
                sys.stderr.write('read guard: pattern must be a string\n')
                return 2
            search_path = tool_input.get('path')
            if search_path is not None and (not isinstance(search_path, str) or not search_path):
                sys.stderr.write('read guard: path must be a non-empty string\n')
                return 2
            if search_path is None:
                raw_target = cwd
            elif os.path.isabs(search_path):
                raw_target = search_path
            else:
                raw_target = os.path.join(cwd, search_path)
        else:
            sys.stderr.write(f'read guard: unsupported tool {tool_name}\n')
            return 2

        lexical_path = Path(os.path.abspath(raw_target))
        resolved_path = Path(os.path.realpath(raw_target))

        # 1. Wiki targets denial
        if resolved_path.is_relative_to(canonical_root) or lexical_path.is_relative_to(canonical_root):
            sys.stderr.write(f'{WIKI_HINT}\n')
            return 2

        # Traversal out of plugin directory
        if canonical_plugin is not None and Path(cwd).resolve().is_relative_to(canonical_plugin):
            if not lexical_path.is_relative_to(canonical_plugin):
                if resolved_path.is_relative_to(canonical_root):
                    sys.stderr.write(f'{WIKI_HINT}\n')
                    return 2
                if resolved_path.is_relative_to(canonical_state):
                    sys.stderr.write(f'{STATE_HINT}\n')
                    return 2
                sys.stderr.write('read guard: traversal out of plugin directory is denied\n')
                return 2

        # 2. Active plugin generation directory exception
        if canonical_plugin is not None:
            if lexical_path.is_relative_to(canonical_plugin):
                if not resolved_path.is_relative_to(canonical_plugin):
                    if resolved_path.is_relative_to(canonical_root):
                        sys.stderr.write(f'{WIKI_HINT}\n')
                        return 2
                    if resolved_path.is_relative_to(canonical_state):
                        sys.stderr.write(f'{STATE_HINT}\n')
                        return 2
                    sys.stderr.write('read guard: symlink escapes plugin directory\n')
                    return 2
                return 0
            if resolved_path.is_relative_to(canonical_plugin) and not lexical_path.is_relative_to(canonical_state):
                # Accessed from outside state via symlink pointing into plugin_dir
                return 0

        # 3. Lesson workspace exception (Read tool only)
        if tool_name == 'Read' and canonical_lessons is not None:
            if lexical_path.is_relative_to(canonical_lessons):
                if not resolved_path.is_relative_to(canonical_lessons):
                    if resolved_path.is_relative_to(canonical_root):
                        sys.stderr.write(f'{WIKI_HINT}\n')
                        return 2
                    if resolved_path.is_relative_to(canonical_state):
                        sys.stderr.write(f'{STATE_HINT}\n')
                        return 2
                    sys.stderr.write('read guard: symlink escapes lessons directory\n')
                    return 2
                return 0

        # 4. State directory targets denial
        if resolved_path.is_relative_to(canonical_state) or lexical_path.is_relative_to(canonical_state):
            sys.stderr.write(f'{STATE_HINT}\n')
            return 2

        # 5. Ancestor searches denial for Glob and Grep
        if tool_name in ('Glob', 'Grep'):
            if _is_ancestor(resolved_path, canonical_root) or _is_ancestor(lexical_path, canonical_root):
                sys.stderr.write(f'{WIKI_HINT}\n')
                return 2
            if _is_ancestor(resolved_path, canonical_state) or _is_ancestor(lexical_path, canonical_state):
                sys.stderr.write(f'{STATE_HINT}\n')
                return 2

        # 6. Outside targets allowed
        return 0

    except Exception as error:
        sys.stderr.write(f'read guard failed: {error}\n')
        return 2
