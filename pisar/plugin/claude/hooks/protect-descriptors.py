"""PreToolUse hook: deny direct edits of `.wiki.toml` and `.domain.toml`.

These descriptors are created and changed by pisar commands, which keep ids
unique and commit consistently. Reads the hook payload from stdin; anything
unreadable is allowed (this hook only reports, it never guesses).
"""
import json
import os
import sys

PROTECTED = ('.wiki.toml', '.domain.toml')
REASON = ('{name} is a pisar descriptor and must not be edited directly. '
          'Use `pisar domain` for domains and `pisar space` for spaces '
          '(create, move, archive, restore) instead.')


def main():
    try:
        payload = json.load(sys.stdin)
        path = payload['tool_input']['file_path']
        name = os.path.basename(path)
    except (ValueError, KeyError, TypeError):
        return 0
    if name in PROTECTED:
        json.dump({'hookSpecificOutput': {
            'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
            'permissionDecisionReason': REASON.format(name=name)}}, sys.stdout)
    return 0


raise SystemExit(main())
