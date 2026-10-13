"""The router: given a ticket and what has run, which step is next — and why.

Control is hub-and-spoke even though messaging is a mesh. Any CSR may message any CSR (schemas.check_message),
but only this function moves a ticket from one agent to the next. That is what makes the stop-limiter
enforceable and the trail auditable: ten agents freely handing work to each other is ninety channels and
no owner.

Each plan is an ordered list of (step, condition, rule). The next step is the first one not yet run whose
condition holds. Every decision returns its rule id, so the audit log says why, not only what.

Global forks, before any plan:
  F-1  a step returned `escalate`         -> skip the rest except steps marked essential (instant, local checks
                                             the AE needs alongside the escalation), then CSR8:record, AE:review
  F-2  every plan step run or skipped    -> CSR8:record, then AE:review
"""
from __future__ import annotations

from . import renewals, service, thresholds
from .desk import Ctx

RECORD, REVIEW = "CSR8:record", "AE:review"


def _lines(ctx: Ctx) -> list:
    return list(ctx.request.get("lines") or [])


def _days_left(ctx: Ctx):
    return ctx.out("CSR6:renewal").get("days_to_expiration")


def _ready(ctx: Ctx) -> bool:
    return bool(ctx.out("CSR1:intake").get("ready_lines"))


def plan(ctx: Ctx) -> list:
    """[(step, condition: () -> bool, rule[, essential])] for the ticket's type."""
    a, kind = ctx.account, ctx.ticket["type"]
    if kind == "QUOTE":
        return [
            ("CSR1:intake", lambda: True, "R-Q1 every quote starts at intake"),
            ("CSR5:verify", lambda: bool(a.get("vehicles") or a.get("mailing_address") or a.get("locations")),
             "R-Q2 vehicles or addresses on file: decode VINs, geocode garaging/territory"),
            ("CSR2:loss_runs", lambda: True, "R-Q3 loss history for every requested line (new venture says so)"),
            ("CSR9:dec_review", lambda: bool(a.get("prior_policies")), "R-Q4 prior policies exist: baseline the dec pages"),
            ("CSR10:exclusions", lambda: True, "R-Q5 exclusions by operation"),
            ("CSR3:applications", lambda: _ready(ctx), "R-Q6 at least one line ready: build ACORD field sets"),
            ("CSR4:market", lambda: _ready(ctx), "R-Q7 at least one line ready: filter and rank markets"),
            ("CSR5:info_request", lambda: bool(service.collect_asks(ctx)), "R-Q8 anything outstanding: one client request"),
        ]
    if kind == "RENEWAL":
        live = lambda: not ctx.out("CSR6:renewal").get("lapsed")
        within = lambda days: (lambda: live() and _days_left(ctx) is not None and _days_left(ctx) <= days)
        return [
            ("CSR6:renewal", lambda: True, "R-R1 renewal ladder"),
            ("CSR2:loss_runs", within(thresholds.RENEWAL_SCAN_HORIZON_DAYS), "R-R2 within the first rung: loss runs"),
            ("CSR9:dec_review", within(thresholds.RENEWAL_SCAN_HORIZON_DAYS), "R-R3 within the first rung: expiring dec page"),
            ("CSR10:exclusions", within(90), "R-R4 within 90 days: exclusions on the expiring forms"),
            ("CSR4:market", lambda: within(90)() and bool(renewals.remarket_reasons(ctx)),
             "R-R5 remarket fork true (rate, loss ratio, non-renewal, gap, exclusion)"),
            ("CSR5:renewal_notice", within(thresholds.RENEWAL_NOTICE_DAYS), "R-R6 within RENEWAL_NOTICE_DAYS: CSR 6 told CSR 5"),
            ("CSR5:info_request", lambda: live() and bool(service.collect_asks(ctx)), "R-R7 anything outstanding: one client request"),
        ]
    if kind == "COI":
        r = ctx.request
        return [
            ("CSR9:dec_review", lambda: True, "R-C1 verify limits and endorsements on the dec page"),
            ("CSR10:exclusions", lambda: bool(r.get("project_type") or r.get("description")), "R-C2 a project is named: exclusions for it"),
            ("CSR3:coi", lambda: True, "R-C3 certificate gates, then the ACORD 25 draft"),
        ]
    if kind == "SERVICE":
        ct = ctx.request["change_type"]
        return [
            ("CSR5:verify", lambda: ct == "ADD_VEHICLE", "R-S1 a vehicle is being added: decode the VIN first"),
            ("CSR5:service", lambda: True, "R-S2 service change by type"),
            ("CSR9:dec_review", lambda: ct in ("POLICY_QUESTION", "LIMIT_CHANGE"), "R-S3 coverage question or limit change: dec page"),
            ("CSR10:exclusions", lambda: ct == "POLICY_QUESTION", "R-S4 coverage question: exclusions"),
        ]
    if kind == "CLAIM":
        return [
            ("CSR7:fnol", lambda: True, "R-L1 first notice of loss"),
            ("CSR10:exclusions", lambda: True, "R-L2 loss facts against exclusions (prompts for the AE, never a denial)", True),
            ("CSR9:dec_review", lambda: bool(ctx.out("CSR7:fnol").get("policy_number")), "R-L3 policy found: limits and deductibles", True),
        ]
    raise ValueError(f"unknown ticket type {kind!r}")


def next_step(ctx: Ctx, skipped: set) -> dict:
    """{"step", "rule"}; skipped steps are recorded by the caller so a false condition is not re-asked."""
    done = set(ctx.state)
    escalated = any(r.get("status") == "escalate" for r in ctx.state.values())
    for step, cond, rule, *essential in plan(ctx):
        if step in done or step in skipped:
            continue
        if escalated and not essential:
            continue
        if cond():
            return {"step": step, "rule": rule + (" (essential: runs on the fast lane)" if escalated else "")}
        skipped.add(step)
    if RECORD not in done:
        return {"step": RECORD, "rule": "F-1 escalated: record, then straight to the AE" if escalated else "F-2 plan complete: CSR 8 records"}
    return {"step": REVIEW, "rule": "F-1 escalated: AE review now" if escalated else "F-2 plan complete: AE review"}
