"""Replay closed tickets from the real book and measure how often the desk agrees with what the CSRs did.

This is the evaluation the desk needs before anyone trusts it: not "does the demo look right" but "on 20
tickets the agency already closed, did the desk route, block, escalate and remarket the way the people did?"
The measurement is retrospective only — agreement on tickets that already happened, never a forecast.

A replay case is a JSON file: a normal ticket plus `"as_of"`, `"now"` and an `"expected"` block written from
what the CSRs actually did (any subset of these keys):

    "expected": {
      "steps_include":   ["CSR2:loss_runs", ...],    every one must have run
      "steps_exclude":   ["CSR4:market"],             none may have run
      "escalated":       true,                         an escalation reached the AE
      "status":          "AWAITING_AE",                final status
      "blocking_lines":  ["AUTO"],                     CSR 1's blocked lines
      "remarket":        true,                         renewal went to CSR 4
      "coi_gate":        "passed" | "requirement_not_supported" | ...,
      "claim_urgency":   "URGENT" | "ROUTINE" | "LATE_NOTICE" | "COVERAGE_QUESTION"
    }

Cases run offline by default (no NHTSA/Census calls) so a replay is reproducible; pass live=True to decode
for real. Real client tickets hold PII: keep replay cases outside the repo or in data/runtime (gitignored).
"""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from . import orchestrator, schemas

CASE_KEYS = ("as_of", "now", "expected")


def _offline(url):
    raise OSError("replay runs offline: no network")


def _check(run: dict, exp: dict) -> list:
    steps = set(run["steps"])
    f = run["packet"]["findings"]
    misses = []

    def want(name, ok, got):
        if not ok:
            misses.append({"check": name, "expected": exp[name], "got": got})

    if "steps_include" in exp:
        want("steps_include", set(exp["steps_include"]) <= steps, sorted(steps))
    if "steps_exclude" in exp:
        want("steps_exclude", not (set(exp["steps_exclude"]) & steps), sorted(steps))
    if "escalated" in exp:
        got = bool(run["packet"]["escalations"])
        want("escalated", got == exp["escalated"], got)
    if "status" in exp:
        want("status", run["status"] == exp["status"], run["status"])
    if "blocking_lines" in exp:
        got = (f.get("CSR1:intake") or {}).get("blocking_lines")
        want("blocking_lines", sorted(got or []) == sorted(exp["blocking_lines"]), got)
    if "remarket" in exp:
        got = "CSR4:market" in steps
        want("remarket", got == exp["remarket"], got)
    if "coi_gate" in exp:
        got = (f.get("CSR3:coi") or {}).get("gate")
        want("coi_gate", got == exp["coi_gate"], got)
    if "claim_urgency" in exp:
        got = (f.get("CSR7:fnol") or {}).get("urgency")
        want("claim_urgency", got == exp["claim_urgency"], got)
    return misses


def run_case(case: dict, *, live: bool = False, config: dict = None) -> dict:
    case = copy.deepcopy(case)
    meta = {k: case.pop(k) for k in CASE_KEYS if k in case}
    if "expected" not in meta or not meta["expected"]:
        raise ValueError(f"{case.get('ticket_id')}: replay case has no expected block")
    tmp = Path(tempfile.mkdtemp(prefix="csr-replay-"))
    cfg = {"db": str(tmp / "agency.db"), "audit_dir": str(tmp / "audit"), "tickets_dir": str(tmp / "tickets"),
           "kill_switch": str(tmp / "STOP"), "sleep": lambda s: None, **(config or {})}
    as_of = meta.get("as_of") or case["received"][:10]
    now = schemas.parse_timestamp(meta.get("now") or case["received"])
    run = orchestrator.advance(orchestrator.open_ticket(case), as_of=as_of, now=now, config=cfg,
                               fetch=None if live else _offline)
    misses = _check(run, meta["expected"])
    return {"ticket_id": case["ticket_id"], "type": case["type"], "agree": not misses,
            "checks": len(meta["expected"]), "misses": misses}


def run_dir(path, *, live: bool = False, config: dict = None) -> dict:
    files = sorted(Path(path).glob("*.json"))
    if not files:
        raise ValueError(f"no replay cases in {path}")
    results = [run_case(json.loads(f.read_text(encoding="utf-8")), live=live, config=config) for f in files]
    agree = sum(r["agree"] for r in results)
    checks = sum(r["checks"] for r in results)
    missed = sum(len(r["misses"]) for r in results)
    return {"cases": len(results), "agree": agree, "disagree": len(results) - agree,
            "checks": checks, "checks_passed": checks - missed, "results": results}
