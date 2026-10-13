"""Tool-policy gate: how an outside call is classified, decided and budgeted.

    classify(tool_name)            S10  READ | WRITE | IRREVERSIBLE; what cannot be classified is IRREVERSIBLE
    decide(...)                    S8   one approval, one action, one target
    Budget(limits).check(...)      S9   calls and writes; the budget is checked before the schema
    vendor_readiness(...)          S6   a connector is "connected" only after a call completed

Derived from northpike's policy.py. The insurance-specific change is the IRREVERSIBLE list. In an
agency the acts that cannot be taken back are not only "send": binding coverage, issuing a
certificate, submitting to a carrier, filing a first notice of loss, endorsing, cancelling or
non-renewing a policy, and accepting a quote all create E&O exposure the moment they happen. Every
one of them needs an AE approval scoped to "<tool>:<target>" (AGENTS.md hard rule 2).

Ordering and direction of error are northpike's: a false READ binds coverage; a false IRREVERSIBLE
costs the AE one click. IRREVERSIBLE rules match loosely, READ rules match whole verbs, and a name
no rule recognises is IRREVERSIBLE. Names are normalised first: camelCase -> snake_case, lowercase,
"-", "." and spaces -> "_".
"""
from __future__ import annotations

import re
from typing import Iterable

RISKS = ("READ", "WRITE", "IRREVERSIBLE")


# ── Classification (spine S10) ────────────────────────────────────────────────

def normalize_tool(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return re.sub(r"[\s.\-]+", "_", s).lower()


def split_mcp(tool_name: str) -> tuple:
    """mcp__<server>__<tool> -> (server, tool). Non-MCP names return ('', name)."""
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        if len(parts) == 3:
            return parts[1], parts[2]
    return "", tool_name


def _word(alternatives: str) -> str:
    """Whole-word match on a normalised name: 'get' matches get_thread, not target."""
    return rf"(?:^|_)(?:{alternatives})(?:_|$)"


# (risk, pattern, why). First match wins, top to bottom.
RULES = tuple((risk, re.compile(pattern), why) for risk, pattern, why in (
    ("READ", r"^(?:(?:get|list|show|read|fetch|view|check|search|query)_)?(?:submissions?|claims?|fnol|endorsements?|payments?|"
     r"binders?|certificates?)_(?:status|show|list|index|get|search)$|^(?:get_)?send_status$",
     "reading the status of a submission, claim, endorsement or payment is not the act; anchored, so "
     "update_claim_status or send_status_update never lands here"),
    ("IRREVERSIBLE", _word("bind|binds|binder|issue|issues|certify|certificates?|cois?|acord_?25|evidence_of_insurance|"
                           "submit|submits|submission|endorse|endorsement|"
                           "cancel|cancellation|nonrenew|non_renew|reinstate|rescind|fnol|accept|decline_quote"),
     "creates or changes coverage, or certifies it to a third party: E&O exposure the moment it happens"),
    ("IRREVERSIBLE", r"(?:file|open|report|submit)_?(?:a_)?(?:claim|loss)",
     "filing a claim with a carrier cannot be withdrawn cleanly"),
    ("IRREVERSIBLE", _word("exports?|downloads?|purchases?|buy|billing|payments?|pay|refund|charge|mvr|credit_report"),
     "data leaves the system, money moves, or a consumer report is pulled (FCRA permissible purpose)"),
    ("IRREVERSIBLE", r"bulk_(?:create|update|upsert|insert|delete)",
     "a bulk call has a blast radius one approval should cover"),
    ("IRREVERSIBLE", r"send|reply|forward|delete|remove|merge|purge|" + _word("post") + r"|publish|execute",
     "cannot be taken back"),
    ("IRREVERSIBLE", _word("filters?|forwarding|vacation|auto_?reply|autoreply|rules?|delegates?"),
     "standing mail rules keep acting after the session ends"),
    ("IRREVERSIBLE",
     r"^(?=.*" + _word("events?|meetings?|invites?|invitations?") + r")"
     r"(?=.*" + _word("create|update|respond|accept|decline|add|set|schedule|book|reschedule|move|cancel") + r")",
     "a calendar write commits the AE to a time and mails other people"),
    ("WRITE", _word("complete|skip|snooze|reopen|create|update|upsert|add|log|insert|patch|set|tag|label|unlabel|"
                    "apply|assign|edit|modify|save|import|associate|attach|write|draft|note"),
     "changes the agency's own system of record; the plural is the noun, so list_labels is not a write"),
    ("READ", _word("get|list|search|query|find|read|describe|fetch|lookup|view|retrieve|count|show|index|"
                   "decode|geocode|summarize|analyze|research|check|validate|verify|compare|scan"),
     "reads only"),
))

# Read-only tools whose names carry no read verb. Exact names; a noun is never evidence of a read.
KNOWN_READS = frozenset({"nhtsa_vpic", "census_geocoder"})

VENDORS = (
    ("nhtsa", re.compile(r"nhtsa|vpic|vin")),
    ("census", re.compile(r"census|geocod")),
    ("ams", re.compile(r"ams|epic|hawksoft|ezlynx|qq_?catalyst|nowcerts")),
    ("gmail", re.compile(r"gmail|outlook|mail")),
    ("carrier", re.compile(r"carrier|ivans|portal")),
)


def classify(tool_name: str) -> dict:
    """{"tool", "vendor", "risk", "why"}. `vendor` is None when the names say nothing (UUID servers);
    `risk` is always one of RISKS."""
    tool_name = str(tool_name or "")
    server, tool = split_mcp(tool_name)
    t = normalize_tool(tool)
    haystack = normalize_tool(f"{server}__{tool}") if server else t
    vendor = next((name for name, pat in VENDORS if pat.search(haystack)), None)
    if t in KNOWN_READS:
        return {"tool": tool_name, "vendor": vendor, "risk": "READ", "why": "reviewed read-only tool"}
    for risk, pattern, why in RULES:
        if pattern.search(t):
            return {"tool": tool_name, "vendor": vendor, "risk": risk, "why": why}
    return {"tool": tool_name, "vendor": vendor, "risk": "IRREVERSIBLE",
            "why": "no rule recognises this tool: fail closed (spine S10)"}


# ── Decision (spine S8) ───────────────────────────────────────────────────────

def decide(tool: str, target: str, risk: str, *, live: bool, approvals: Iterable[str],
           content_ok: bool, harness: bool) -> dict:
    """Evaluation order, first match wins:
    READ allows; a content-gate ERROR denies (CONTENT_BLOCKED) even over an approval; dry run denies
    (DRY_RUN); WRITE allows; IRREVERSIBLE allows only on an approval scoped exactly "<tool>:<target>",
    otherwise asks (NEEDS_APPROVAL) under a harness and denies (NO_APPROVAL) without one. A risk that
    is not READ or WRITE is treated as IRREVERSIBLE."""
    if risk == "READ":
        return {"decision": "allow"}
    if not content_ok:
        return {"decision": "deny", "code": "CONTENT_BLOCKED"}
    if not live:
        return {"decision": "deny", "code": "DRY_RUN"}
    if risk == "WRITE":
        return {"decision": "allow"}
    if f"{tool}:{target}" in set(approvals or ()):
        return {"decision": "allow"}
    if harness:
        return {"decision": "ask", "code": "NEEDS_APPROVAL"}
    return {"decision": "deny", "code": "NO_APPROVAL"}


# ── Budget (spine S9) ─────────────────────────────────────────────────────────

class Budget:
    """Counts what has been consumed against `limits` ({"calls": n, "writes": n}; an absent key is
    unlimited). The budget is checked before the schema. A BUDGET refusal consumes nothing; an
    invalid call within budget is SCHEMA and still costs one call. A write (anything not READ) counts
    against both calls and writes. `used` can be seeded so a ticket's budget survives a restart."""

    def __init__(self, limits: dict, used: dict = None):
        self.limits = dict(limits)
        self.used = {"calls": 0, "writes": 0, **(used or {})}

    def check(self, risk: str, valid: bool = True) -> str:
        want = {"calls": 1, "writes": int(risk != "READ")}
        for key, n in want.items():
            if n and key in self.limits and self.used[key] + n > int(self.limits[key]):
                return "BUDGET"
        if not valid:
            self.used["calls"] += 1
            return "SCHEMA"
        for key, n in want.items():
            self.used[key] += n
        return "OK"


# ── Honest degradation (spine S6) ─────────────────────────────────────────────

def vendor_readiness(entries: Iterable[dict], vendor: str) -> dict:
    """A connector is "connected" only once the log shows a call that completed. Until then it is
    needs_key with no value: never assumed to work, never a zero. An entry is
    {"event": "call_ok", "vendor": <name>, "ts": <ISO timestamp>}."""
    done = [e for e in entries if e.get("event") == "call_ok" and e.get("vendor") == vendor]
    if not done:
        return {"status": "needs_key", "value": None}
    return {"status": "connected", "value": {"completed_calls": len(done), "last": done[-1].get("ts")}}
