"""The plugin's PreToolUse guard: what an agent inside an agency's Claude Code may do without a person.

    decide(event, live=None) -> {"decision": "allow" | "ask" | "deny", "code", "reason"} | None (no opinion)

The hook runs on every tool (matcher ".*"). Layers, cheapest first:

  command tools (Bash, PowerShell, Monitor, anything with a `command` input)
      csr CLI invocations are parsed with shlex: the human-only subcommands (`confirm`, `live`, `lock`, `unlock`)
      are refused wherever they appear among the arguments. Any other command that mentions a protected name —
      the runtime directory, the live switch, the kill switch, the store, the pepper, the audit trail, ticket
      state, the AMS config, confirmed appetite, rule confirmations — is refused, case-insensitively, whatever
      the quoting, `cd`, variables or globs around it. A plain one-line read (cat/head/ls/grep...) of a protected
      file is allowed, except the pepper.
  file tools (Write, Edit, MultiEdit, NotebookEdit, Read for the pepper)
      the path is normalized (realpath, case-folded) and refused if it lands on a protected file or anywhere
      under an agency's runtime/.
  mcp__* tools
      classified by policy.classify (S10). READ allows; WRITE allows only when live, else asks; IRREVERSIBLE always
      asks. Any non-READ MCP call whose arguments mention a protected name is refused.

This is defense in depth, not a sandbox. An agent with a shell can build a path at runtime; that is why the
human-only steps also require an interactive terminal (humans.attest), approvals are verified against the audit
chain (orchestrator.release), and `bin/csr lock` adds OS-level immutability. Fails closed: an unreadable event is
denied.
"""
from __future__ import annotations

import os
import re
import shlex

from . import orchestrator, policy

# Names whose mere mention in a mutating command is refused (casefolded substring match).
PROTECTED_NAMES = ("runtime", "desk.json", "agency.db", "pepper", "appetite.csv", "appetite.proposed.csv",
                   "rule_confirmations", "ams.json", "ams_calls", "/stop", "audit")
# Path patterns for file tools (matched on the normalized, casefolded path).
PROTECTED_PATHS = (
    (re.compile(r"(^|/)runtime(/|$)"), "an agency's runtime/ (live switch, kill switch, tickets, audit, store, pepper) is the engine's"),
    (re.compile(r"(^|/)appetite\.csv$"), "confirmed appetite changes only through bin/csr setup confirm, by a person at a terminal"),
    (re.compile(r"(^|/)appetite\.proposed\.csv$"), "proposals go in through bin/csr setup propose, which checks them"),
    (re.compile(r"(^|/)rule_confirmations\.csv$"), "rule confirmations are made by compliance with bin/csr rules confirm"),
    (re.compile(r"(^|/)(state_rules|surplus_lines_rules)\.csv$"), "rules files are maintained by compliance and the plugin, not agents"),
    (re.compile(r"(^|/)ams\.json$"), "the AMS config (endpoints, token variable) is the agency's integration owner's"),
)
HUMAN_SUBCOMMANDS = {"confirm", "live", "lock", "unlock"}
COMMAND_TOOLS = ("Bash", "PowerShell", "Monitor")
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
READ_ONLY = re.compile(r"^\s*(?:cat|head|tail|less|wc|grep|ls|stat|file|diff)\s")


def _norm(path: str) -> str:
    p = os.path.expanduser(str(path or ""))
    try:
        p = os.path.realpath(p)
    except (OSError, ValueError):
        p = os.path.normpath(p)
    return os.path.normcase(p).replace("\\", "/").casefold()


def _protected_path(path: str):
    p = _norm(path)
    return next((why for pat, why in PROTECTED_PATHS if pat.search(p)), None)


def _mentions_protected(text: str):
    t = str(text or "").casefold()
    return next((n for n in PROTECTED_NAMES if n in t), None)


def _csr_human_step(cmd: str):
    """A csr CLI call whose arguments include a human-only subcommand, however the options are ordered or quoted."""
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        tokens = cmd.split()
    for i, tok in enumerate(tokens):
        if tok.rstrip("/").endswith(("bin/csr", "csrdesk")) or tok == "csr":
            if HUMAN_SUBCOMMANDS & {t.strip("'\"").casefold() for t in tokens[i + 1:]}:
                return True
    return bool(re.search(r"\b(markets|staterules|humans)\s*\.\s*(confirm|attest)\b|\bset_live\b|\block_files\b", cmd))


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def decide(event: dict, live: bool = None):
    if not isinstance(event, dict) or not isinstance(event.get("tool_name"), str):
        return {"decision": "deny", "code": "BAD_EVENT", "reason": "hook event unreadable: failing closed"}
    tool, inp = event["tool_name"], event.get("tool_input") or {}
    if not isinstance(inp, dict):
        return {"decision": "deny", "code": "BAD_EVENT", "reason": "tool input unreadable: failing closed"}

    if tool.startswith("mcp__"):
        c = policy.classify(tool)
        if c["risk"] == "READ":
            return {"decision": "allow", "code": "READ", "reason": c["why"]}
        hit = next((n for s in _strings(inp) for n in [_mentions_protected(s)] if n), None)
        if hit:
            return {"decision": "deny", "code": "HUMAN_ONLY_FILE", "reason": f"{tool} would touch a protected desk file ({hit})"}
        if c["risk"] == "WRITE":
            if orchestrator.agency_live() if live is None else live:
                return {"decision": "allow", "code": "WRITE_LIVE", "reason": c["why"]}
            return {"decision": "ask", "code": "WRITE_NOT_LIVE",
                    "reason": f"{tool} writes to a system of record and the desk is not live: a person approves this one call"}
        target = next(iter(_strings(inp)), "")[:120]
        return {"decision": "ask", "code": "IRREVERSIBLE",
                "reason": f"{tool}: {c['why']}. Target: {target or 'n/a'}. A person approves this one call."}

    if tool in FILE_TOOLS or (tool == "Read"):
        path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
        if tool == "Read":
            if "pepper" in _norm(path).rsplit("/", 1)[-1]:
                return {"decision": "deny", "code": "SECRET", "reason": "the FEIN pepper is a secret"}
            return None
        why = _protected_path(path)
        return {"decision": "deny", "code": "HUMAN_ONLY_FILE", "reason": why} if why else None

    cmd = inp.get("command")
    if tool in COMMAND_TOOLS or isinstance(cmd, str):
        cmd = str(cmd or "")
        if _csr_human_step(cmd):
            return {"decision": "deny", "code": "HUMAN_ONLY_COMMAND",
                    "reason": "confirmations, the live switch and file locks are a person's, in their own terminal"}
        if re.search(r"\b(chflags|chattr|attrib|icacls|setfacl)\b", cmd):
            return {"decision": "deny", "code": "HUMAN_ONLY_COMMAND", "reason": "file protection flags are the agency's to change"}
        hit = _mentions_protected(cmd)
        if hit:
            plain = cmd.replace("${CLAUDE_PLUGIN_ROOT}", "").replace("$CLAUDE_PLUGIN_ROOT", "")
            one_line_read = READ_ONLY.match(plain) and not re.search(r"[\n\r>|;&`$()]", plain)
            is_csr = bool(re.match(r"^\s*\"?(?:\S*/)?(?:bin/)?csr\"?\s", plain)) and not re.search(r"[\n\r>|;&`$()]", plain)
            if (one_line_read and "pepper" not in cmd.casefold()) or is_csr:
                return None
            return {"decision": "deny", "code": "HUMAN_ONLY_FILE",
                    "reason": f"command touches a protected desk file or directory ({hit}); the engine changes these, not a shell"}
        return None
    return None
