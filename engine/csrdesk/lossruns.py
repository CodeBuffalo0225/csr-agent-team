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
    """EARNED premium for `line` across [start, end] (each term's premium pro-rated to the days it overlaps
    the window, so the current term counts only what has elapsed), or None unless the policies on file cover
    the whole window without a gap and every one has a premium. Five years of losses over one year of premium
    is not a loss ratio; it is a misleading number, and an unknown is not a benign value."""
    pols = sorted((p for p in account.get("prior_policies") or []
                   if p["line"] == line and d(p["expiration"]) > start and d(p["effective"]) < end),
                  key=lambda p: p["effective"])
    if not pols or d(pols[0]["effective"]) > start or any(p.get("premium") is None for p in pols):
        return None
    for prev, nxt in zip(pols, pols[1:]):
        if d(nxt["effective"]) > d(prev["expiration"]):
            return None
    earned = 0.0
    for p in pols:
        eff, exp = d(p["effective"]), d(p["expiration"])
        overlap = (min(exp, end) - max(eff, start)).days
        earned += float(p["premium"]) * max(overlap, 0) / (exp - eff).days
    return round(earned, 2)


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
            if yib == 0:
                status = "new_venture"            # truly new: nothing to order
            elif a.get("prior_policies"):
                status = "new_line"               # e.g. an umbrella over existing GL/auto: underlying runs apply
            else:
                status = "prior_unknown"          # an established business with no prior policy on file: ask, never assume
            per_line[line] = {"status": status, "valued_as_of": None, "valuation_age_days": None, "years_required": years}
            continue
        start_needed = required_start(priors, as_of, years)
        window_carriers = {p["carrier"] for p in priors if d(p["expiration"]) > start_needed}
        run_carriers = {r["carrier"] for r in runs}
        missing_carriers = sorted(window_carriers - run_carriers)
        # freshness is judged on the run that covers the most recent period (the current carrier's), never on
        # whichever carrier happened to send the newest valuation
        current_run = max(runs, key=lambda r: (r["period_end"], r["valued_as_of"])) if runs else None
        newest = current_run["valued_as_of"] if current_run else None
        earliest = min((r["period_start"] for r in runs), default=None)
        age = scoring.age_days(newest, as_of) if newest else None
        stale_carriers = sorted({r["carrier"] for r in runs if scoring.age_days(r["valued_as_of"], as_of) > thresholds.LOSS_RUN_MAX_AGE_DAYS
                                 and r["carrier"] == current_run["carrier"]}) if runs else []
        if not runs or missing_carriers:
            status = "missing"
        elif age > thresholds.LOSS_RUN_MAX_AGE_DAYS:
            status = "stale"
        elif d(earliest) > start_needed:
            status = "short_history"
        else:
            status = "current"
        per_line[line] = {"status": status, "valued_as_of": newest, "valuation_age_days": age,
                          "years_required": years, "covers_from": earliest, "missing_carriers": missing_carriers}
        if status != "current":
            carriers = (missing_carriers + stale_carriers) if status in ("missing", "stale") and runs else \
                sorted({p["carrier"] for p in priors} | run_carriers)
            carriers = sorted(set(carriers)) or sorted({p["carrier"] for p in priors} | run_carriers)
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

    premiums, windowed = {}, []
    for line in lines:
        priors = [p for p in a.get("prior_policies") or [] if p["line"] == line]
        start = required_start(priors, as_of, years)
        premiums[line] = _premiums_in_window(a, line, start, as_of)
        # losses and premium over the same window: a 2017 claim does not belong in a five-term loss ratio
        for r in a.get("loss_runs") or []:
            if r["line"] == line:
                windowed.append(dict(r, claims=[c for c in r["claims"] if d(c["date_of_loss"]) >= start]))
    summary_by_line = scoring.loss_summary(windowed, premiums)
    large = [dict(l, line=line) for line, s in summary_by_line.items() for l in s["large_losses"]]
    if large:
        msgs.append(message("CSR2", "CSR4", "NOTIFY", tid, "large loss on file",
                            f"{len(large)} loss(es) at or above {money(thresholds.LARGE_LOSS_USD)}: markets need the narrative."))

    pending = [l for l, v in per_line.items() if v["status"] in ("stale", "missing", "short_history", "prior_unknown")]
    parts = [f"{l} {v['status']}" for l, v in per_line.items()]
    summary = "Loss runs: " + "; ".join(parts) + (f"; {len(drafts)} request(s) drafted." if drafts else ".")
    return result("CSR2", "loss_runs", "needs_data" if pending else "done", summary,
                  findings={"per_line": per_line, "loss_summary": summary_by_line, "large_losses": large,
                            "pending_lines": pending, "years_required": years},
                  messages=msgs, drafts=drafts)
