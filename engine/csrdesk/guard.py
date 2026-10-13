"""The plugin's PreToolUse guard: what an agent inside an agency's Claude Code may do without a human.

    decide(event) -> {"decision": "allow" | "ask" | "deny", "code", "reason"}

  mcp__* tools          classified by policy.classify (S10: by tool name, so UUID-named connectors are governed)
                          READ          allow
                          WRITE         allow when the agency's desk is live, else ask
                          IRREVERSIBLE  ask, always: the person clicking approve IS the scoped approval for that one
                                        call (send, bind, issue a certificate, submit, file a claim, pay, pull an MVR)
  Write/Edit/...        deny on the human-only files: the live switch, confirmed appetite, the agency's confirmed rules,
                        audit logs, the store, the FEIN pepper, the kill switch
  Bash                  deny the human-only commands (setup confirm, rules confirm) and any command touching the
                        human-only files; everything else proceeds to Claude Code's own permission system

Fails closed: an event this module cannot read is denied with the reason.
"""
from __future__ import annotations

import re

from . import orchestrator, policy

PROTECTED = (
    (re.compile(r"(^|/)runtime/desk\.json$"), "the live switch is the agency's to set"),
    (re.compile(r"(^|/)appetite\.csv$"), "confirmed appetite is changed only through bin/csr setup confirm, by a person"),
    (re.compile(r"(^|/)(state_rules|surplus_lines_rules)\.csv$"), "rules are confirmed by compliance with bin/csr rules confirm"),
    (re.compile(r"(^|/)runtime/audit/"), "the audit trail is append-only and written by the engine"),
    (re.compile(r"(^|/)runtime/agency\.db"), "CSR 8's store is written by the engine only"),
    (re.compile(r"(^|/)runtime/pepper$"), "the FEIN pepper is a secret"),
    (re.compile(r"(^|/)runtime/STOP$"), "the kill switch is the agency's"),
)
HUMAN_COMMANDS = re.compile(r"\b(?:setup\s+confirm|rules\s+confirm)\b")
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
READ_ONLY = re.compile(r"^\s*(?:cat|head|tail|less|wc|grep|ls|stat|file|diff)\b")


def _protected(path: str):
    p = (path or "").replace("\\", "/")
    return next((why for pat, why in PROTECTED if pat.search(p)), None)


def decide(event: dict, live: bool = None) -> dict:
    if not isinstance(event, dict) or not isinstance(event.get("tool_name"), str):
        return {"decision": "deny", "code": "BAD_EVENT", "reason": "hook event unreadable: failing closed"}
    tool, inp = event["tool_name"], event.get("tool_input") or {}
    if tool.startswith("mcp__"):
        c = policy.classify(tool)
        if c["risk"] == "READ":
            return {"decision": "allow", "code": "READ", "reason": c["why"]}
        if c["risk"] == "WRITE":
            is_live = orchestrator.agency_live() if live is None else live
            if is_live:
                return {"decision": "allow", "code": "WRITE_LIVE", "reason": c["why"]}
            return {"decision": "ask", "code": "WRITE_NOT_LIVE",
                    "reason": f"{tool} writes to a system of record and the desk is not live: a person approves this one call"}
        return {"decision": "ask", "code": "IRREVERSIBLE",
                "reason": f"{tool}: {c['why']}. A person approves this one call; approving it is the scoped approval."}
    if tool in FILE_TOOLS:
        why = _protected(inp.get("file_path") or inp.get("notebook_path") or "")
        if why:
            return {"decision": "deny", "code": "HUMAN_ONLY_FILE", "reason": why}
        return None
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        if HUMAN_COMMANDS.search(cmd):
            return {"decision": "deny", "code": "HUMAN_ONLY_COMMAND",
                    "reason": "confirmations are made by a person in their own terminal, never by an agent"}
        if READ_ONLY.match(cmd) and not re.search(r"[>|;&`$]", cmd):
            return None                              # looking at a protected file is fine; changing it is not
        for token in re.findall(r"[\w./~-]+", cmd):
            why = _protected(token)
            if why:
                return {"decision": "deny", "code": "HUMAN_ONLY_FILE", "reason": why}
        return None
    return None
