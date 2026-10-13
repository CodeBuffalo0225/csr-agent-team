"""Steps only a person may take, enforced where the step happens rather than only in the hook.

    attest(action, expected) -> the confirming person's name, or raises PermissionError

A human-only step (confirming a market's appetite, confirming a state's rules, turning the desk live or off,
locking the protected files) needs an interactive terminal and the person typing the exact word shown. Claude
Code's Bash tool has no terminal attached, so an agent running the same command cannot pass. A determined
agent with a shell can still reach Python directly; the remaining layer is OS-level protection
(`bin/csr lock`) plus the hook, and docs/DEPLOYMENT.md says so plainly.

Tests pass their own `prompt` and `isatty`.
"""
from __future__ import annotations

import sys


def attest(action: str, expected: str, by: str = None, *, prompt=input, isatty=None) -> str:
    tty = sys.stdin.isatty() if isatty is None else isatty()
    if not tty:
        raise PermissionError(f"{action} is a human-only step: run it yourself in an interactive terminal "
                              f"(agents and scripts have no terminal, by design)")
    who = (by or "").strip() or prompt("Your name (recorded as the person confirming): ").strip()
    if not who:
        raise PermissionError(f"{action}: a name is required")
    typed = prompt(f"{action}\nType {expected!r} to confirm: ").strip()
    if typed != expected:
        raise PermissionError(f"{action}: confirmation did not match; nothing changed")
    return who
