"""The contract every CSR worker returns, and the lookups they share.

A worker is `run(ctx) -> result`. It reads the masked ticket and the outputs of the steps before it;
it never writes anywhere (CSR 8 is the only writer) and never moves control (the router does).

    result = {
      "agent": "CSR2", "task": "loss_runs",
      "status": "done" | "needs_data" | "blocked" | "escalate",
      "summary": one deterministic line for the AE packet,
      "findings": {...}            structured, what the router and the AE read,
      "messages": [bus message]    NOTIFY / REQUEST / REPLY to other agents (schemas.check_message),
      "drafts":   [draft]          prose for a human to release; never sent by the engine,
      "escalate": reason | None    set when the step needs the AE now, not at the end,
    }

    draft = {"kind", "to", "subject", "body", "release_tool", "allowed"}
      release_tool  the IRREVERSIBLE tool a human uses to release it (classified by policy.py)
      allowed       every figure the body may contain, traced to the record or computed output;
                    the orchestrator runs grounding.ground(body, allowed) and discards a failing draft
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import schemas

STATUSES = ("done", "needs_data", "blocked", "escalate")


@dataclass
class Ctx:
    ticket: dict                      # masked (pii.masked_ticket)
    state: dict                       # {step_id: result} for steps already run
    as_of: str                        # YYYY-MM-DD the desk treats as today
    budget: object                    # policy.Budget for this ticket
    note: Optional[str] = None        # the AE's note when this step is a REVISE
    config: dict = field(default_factory=dict)
    fetch: Optional[Callable] = None  # injected HTTP fetch for publicdata; None = the real network

    @property
    def account(self) -> dict:
        return self.ticket["account"]

    @property
    def request(self) -> dict:
        return self.ticket["request"]

    def out(self, step: str) -> dict:
        """Findings of an earlier step, {} if it has not run."""
        return (self.state.get(step) or {}).get("findings") or {}


def result(agent: str, task: str, status: str, summary: str, *, findings: dict = None,
           messages: list = None, drafts: list = None, escalate: str = None) -> dict:
    assert status in STATUSES, status
    return {"agent": agent, "task": task, "status": status, "summary": summary, "findings": findings or {},
            "messages": messages or [], "drafts": drafts or [], "escalate": escalate}


def message(sender: str, to: str, kind: str, ticket_id: str, subject: str, body: str) -> dict:
    return schemas.check_message({"sender": sender, "to": to, "kind": kind, "ticket_id": ticket_id,
                                  "subject": subject, "body": body})


def draft(kind: str, to: str, subject: str, body: str, release_tool: str, allowed: list) -> dict:
    return {"kind": kind, "to": to, "subject": subject, "body": body, "release_tool": release_tool,
            "allowed": [str(a) for a in allowed]}


# ── Shared lookups ────────────────────────────────────────────────────────────

def d(value) -> dt.date:
    return schemas.as_date(value)


def active_policy(account: dict, line: str, on: str) -> Optional[dict]:
    """The policy for `line` in force on date `on` (effective <= on < expiration), latest-effective first."""
    hits = [p for p in account.get("prior_policies") or []
            if p["line"] == line and d(p["effective"]) <= d(on) < d(p["expiration"])]
    return max(hits, key=lambda p: p["effective"]) if hits else None


def policy_by_number(account: dict, number: str) -> Optional[dict]:
    return next((p for p in account.get("prior_policies") or [] if p["policy_number"] == number), None)


def ticket_lines(ticket: dict) -> list:
    """The lines a ticket is about, in a stable order."""
    r, kind = ticket["request"], ticket["type"]
    if kind in ("QUOTE", "COI"):
        return list(r["lines"])
    if kind == "CLAIM":
        return [r["line"]]
    if kind == "RENEWAL":
        p = policy_by_number(ticket["account"], r["policy_number"])
        return [p["line"]] if p else []
    if kind == "POLICY_CHECK":
        return [r["proposal"]["line"]]
    return []


def money(x) -> str:
    """$1,000,000 / $2,500.50 — the one way the desk writes an amount, so grounding sees one form."""
    x = float(x)
    return f"${x:,.0f}" if x == int(x) else f"${x:,.2f}"
