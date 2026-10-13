"""CSR 2 — loss runs. Do we hold currently-valued loss history for every line, and what does it say?

Per line, in code:
  new_venture     no prior coverage and zero years in business: nothing to order, say so
  current         runs on file cover the required years and the newest is valued within LOSS_RUN_MAX_AGE_DAYS
  stale           runs exist but the newest valuation is older than that: re-request
  short_history   runs are current but do not reach back the required years: request the gap
  missing         prior coverage exists, no runs on file: request from every prior carrier on the line

Required years = LOSS_RUN_YEARS, or years in business if fewer. Most carriers release loss runs only
with the insured's signed authorization, so every request draft is paired with an LOA ask to CSR 5.
The arithmetic (incurred, loss ratio, large losses) is scoring.loss_summary; no prose computes it.
"""
from __future__ import annotations

import datetime as dt

from . import scoring, thresholds
from .desk import Ctx, d, draft, message, money, result, ticket_lines


def _premiums_in_window(account: dict, line: str, start: dt.date, end: dt.date) -> float:
    """Total written premium for `line` across [start, end], or None unless the policies on file cover the
    whole window without a gap and every one has a premium. Five years of losses over one year of premium
    is not a loss ratio; it is a misleading number, and an unknown is not a benign value."""
    pols = sorted((p for p in account.get("prior_policies") or []
                   if p["line"] == line and d(p["expiration"]) > start and d(p["effective"]) < end),
                  key=lambda p: p["effective"])
    if not pols or d(pols[0]["effective"]) > start or any(p.get("premium") is None for p in pols):
        return None
    for prev, nxt in zip(pols, pols[1:]):
        if d(nxt["effective"]) > d(prev["expiration"]):
            return None
    return float(sum(p["premium"] for p in pols))


def _years_before(day: dt.date, years: int) -> dt.date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:                      # Feb 29 -> Feb 28
        return day.replace(year=day.year - years, day=28)


def required_start(priors: list, as_of: dt.date, years: int) -> dt.date:
    """Loss history is counted in policy terms: N years means the current term plus the N-1 before it,
    so the runs must reach back to the current term's effective date minus N-1 years. With no term in
    force, fall back to N calendar years before as_of."""
    current = [d(p["effective"]) for p in priors if d(p["effective"]) <= as_of]
    return _years_before(max(current), years - 1) if current else _years_before(as_of, years)


def run(ctx: Ctx) -> dict:
    a, as_of = ctx.account, d(ctx.as_of)
    lines = ticket_lines(ctx.ticket)
    yib = a.get("years_in_business")
    years = min(thresholds.LOSS_RUN_YEARS, yib) if isinstance(yib, int) and yib > 0 else thresholds.LOSS_RUN_YEARS
    per_line, drafts, msgs = {}, [], []
    tid = ctx.ticket["ticket_id"]

    for line in lines:
        runs = [r for r in a.get("loss_runs") or [] if r["line"] == line]
        priors = [p for p in a.get("prior_policies") or [] if p["line"] == line]
        if not runs and not priors:
            # no history on this line: a new venture, or a new line (umbrella over existing GL/auto) whose
            # carriers underwrite from the underlying lines' runs
            status = "new_venture" if (yib == 0 or not a.get("prior_policies")) else "new_line"
            per_line[line] = {"status": status, "valued_as_of": None, "valuation_age_days": None, "years_required": years}
            continue
        newest = max((r["valued_as_of"] for r in runs), default=None)
        earliest = min((r["period_start"] for r in runs), default=None)
        age = scoring.age_days(newest, as_of) if newest else None
        if not runs:
            status = "missing"
        elif age > thresholds.LOSS_RUN_MAX_AGE_DAYS:
            status = "stale"
        elif d(earliest) > required_start(priors, as_of, years):
            status = "short_history"
        else:
            status = "current"
        per_line[line] = {"status": status, "valued_as_of": newest, "valuation_age_days": age,
                          "years_required": years, "covers_from": earliest}
        if status != "current":
            carriers = sorted({p["carrier"] for p in priors} | {r["carrier"] for r in runs})
            for carrier in carriers:
                nums = ", ".join(p["policy_number"] for p in priors if p["carrier"] == carrier) or "all policies"
                body = (f"Please provide currently valued loss runs for {a['insured_name']}, {line} coverage, "
                        f"policy {nums}, covering the most recent {years} years. A signed letter of authorization "
                        f"from the insured is attached. Please send to our service desk.")
                drafts.append(draft("letter", f"{carrier} loss run department", f"Loss run request - {a['insured_name']} - {line}",
                                    body, "send_email", [f"{years} years", nums]))
    if drafts:
        msgs.append(message("CSR2", "CSR5", "REQUEST", tid, "signed LOA needed",
                            f"Carriers need the insured's signed letter of authorization to release {len(drafts)} loss run(s)."))

    premiums = {}
    for line in lines:
        priors = [p for p in a.get("prior_policies") or [] if p["line"] == line]
        premiums[line] = _premiums_in_window(a, line, required_start(priors, as_of, years), as_of)
    summary_by_line = scoring.loss_summary([r for r in a.get("loss_runs") or [] if r["line"] in lines], premiums)
    large = [dict(l, line=line) for line, s in summary_by_line.items() for l in s["large_losses"]]
    if large:
        msgs.append(message("CSR2", "CSR4", "NOTIFY", tid, "large loss on file",
                            f"{len(large)} loss(es) at or above {money(thresholds.LARGE_LOSS_USD)}: markets need the narrative."))

    pending = [l for l, v in per_line.items() if v["status"] in ("stale", "missing", "short_history")]
    parts = [f"{l} {v['status']}" for l, v in per_line.items()]
    summary = "Loss runs: " + "; ".join(parts) + (f"; {len(drafts)} request(s) drafted." if drafts else ".")
    return result("CSR2", "loss_runs", "needs_data" if pending else "done", summary,
                  findings={"per_line": per_line, "loss_summary": summary_by_line, "large_losses": large,
                            "pending_lines": pending, "years_required": years},
                  messages=msgs, drafts=drafts)
