#!/usr/bin/env python3
"""Claude Code PreToolUse hook entry point for the csr-desk plugin.

    python3 engine/hook.py < event.json

Prints a hookSpecificOutput decision when the guard has one (allow / ask / deny); prints nothing to leave the
call to Claude Code's own permissions. Fails CLOSED: any error denies the call with the error named.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _emit(decision: dict) -> None:
    reason = f"[csr-desk {decision.get('code', '')}] {decision.get('reason', '')}".strip()
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision["decision"],
                                             "permissionDecisionReason": reason}}))


def main() -> int:
    try:
        from csrdesk import guard
        event = json.loads(sys.stdin.read())
        d = guard.decide(event)
        if d:
            _emit(d)
    except Exception as exc:  # noqa: BLE001 — a guard that fails open is decoration
        _emit({"decision": "deny", "code": "GUARD_ERROR", "reason": f"failing closed: {type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
