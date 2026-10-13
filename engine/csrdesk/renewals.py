"""CSR 6 — renewals. Which policies are coming up, what is due on each, and does it need remarketing?

    scan(accounts, as_of)    the book sweep: every policy expiring within RENEWAL_SCAN_HORIZON_DAYS becomes
                             a RENEWAL ticket (one per policy, idempotent by policy number + expiration)
    run(ctx)                 one renewal ticket: days left, rungs due, and the NOTIFY to CSR 5 at
                             RENEWAL_NOTICE_DAYS (the spec), plus the earlier rungs to CSR 2/9/10/4
    remarket_reasons(...)    the remarket fork, in code, after CSR 2/9/10 have reported

Lapsed (days left < 0) skips every rung and escalates: an expired policy is the AE's phone call today.
"""
from __future__ import annotations

from . import scoring, staterules, thresholds
from .desk import Ctx, message, policy_by_number, result


def scan(accounts: list, as_of: str) -> list:
    out = []
    for acct in accounts:
        for p in acct.get("prior_policies") or []:
            left = scoring.days_until(p["expiration"], as_of)
            renewed = any(q["line"] == p["line"] and q["effective"] == p["expiration"] for q in acct.get("prior_policies") or [])
            if renewed or left > thresholds.RENEWAL_SCAN_HORIZON_DAYS or left < -30:
                continue
            out.append({"ticket_id": f"RNW-{p['policy_number']}-{p['expiration']}", "insured_name": acct["insured_name"],
                        "policy_number": p["policy_number"], "line": p["line"], "carrier": p["carrier"],
                        "expiration": p["expiration"], "days_to_expiration": left,
                        "client_notice_due": left <= thresholds.RENEWAL_NOTICE_DAYS})
    return sorted(out, key=lambda r: (r["days_to_expiration"], r["ticket_id"]))


def rungs_due(days_left: int) -> list:
    return [{"days": d, "owner": owner, "task": task} for d, owner, task in thresholds.RENEWAL_LADDER if days_left <= d]


def run(ctx: Ctx) -> dict:
    a, tid = ctx.account, ctx.ticket["ticket_id"]
    p = policy_by_number(a, ctx.request["policy_number"])
    if p is None:
        return result("CSR6", "renewal", "escalate", f"Renewal: policy {ctx.request['policy_number']} not on the account.",
                      findings={"policy_number": ctx.request["policy_number"]}, escalate="renewal ticket for an unknown policy")
    left = scoring.days_until(p["expiration"], ctx.as_of)
    if left < 0:
        return result("CSR6", "renewal", "escalate", f"Renewal: {p['policy_number']} EXPIRED {-left} days ago.",
                      findings={"policy_number": p["policy_number"], "days_to_expiration": left, "lapsed": True, "rungs_due": []},
                      escalate=f"{p['line']} policy {p['policy_number']} has lapsed")
    due = rungs_due(left)
    msgs = []
    if left <= thresholds.RENEWAL_NOTICE_DAYS:
        msgs.append(message("CSR6", "CSR5", "NOTIFY", tid, "renewal coming up",
                            f"{p['line']} {p['policy_number']} ({p['carrier']}) expires {p['expiration']}: {left} days. Send the client renewal notice."))
    for owner in sorted({r["owner"] for r in due if r["owner"] not in ("CSR5", "AE")}):
        msgs.append(message("CSR6", owner, "NOTIFY", tid, "renewal task due",
                            "; ".join(r["task"] for r in due if r["owner"] == owner)))
    renewed = next((q for q in a.get("prior_policies") or [] if q["line"] == p["line"] and q["effective"] == p["expiration"]), None)
    urgent = left <= thresholds.RENEWAL_ESCALATE_DAYS and renewed is None
    state = (a.get("mailing_address") or {}).get("state")
    rows = staterules.load(ctx.config.get("state_rules"))
    rule = staterules.rule_for(state, p["line"], ctx.as_of, rows) if state else {"status": "needs_data", "value": None,
                                                                                    "why": "no mailing state on the account"}
    if rule["status"] in ("ok", "unconfirmed"):
        n = rule["value"]["nonrenewal_notice_days"]
        rule_txt = (f"{state} non-renewal notice {n} days before expiration"
                    + (" (researched, compliance has not confirmed)" if rule["status"] == "unconfirmed" else ""))
        if n is not None and left < n and not p.get("nonrenewal_notice"):
            rule_txt += f"; {left} days left and no non-renewal notice on file"
    else:
        rule_txt = f"{state} notice rules: {rule['why']}"
    return result("CSR6", "renewal", "escalate" if urgent else "done",
                  f"Renewal: {p['line']} {p['policy_number']} expires in {left} days; {len(due)} rung(s) due. {rule_txt}.",
                  findings={"policy_number": p["policy_number"], "line": p["line"], "days_to_expiration": left,
                            "lapsed": False, "rungs_due": due, "client_notice_due": left <= thresholds.RENEWAL_NOTICE_DAYS,
                            "state_notice_rule": rule, "renewal_on_file": renewed["policy_number"] if renewed else None},
                  messages=msgs, escalate=f"{p['policy_number']} unbound with {left} days left" if urgent else None)


def remarket_reasons(ctx: Ctx) -> list:
    """Remarket when any of these hold (each a number or a fact, so it is code):
       carrier indicated increase >= REMARKET_RATE_CHANGE_PCT; loss ratio >= REMARKET_LOSS_RATIO;
       a non-renewal notice; a HIGH dec-page gap; a HIGH/CRITICAL exclusion listed on the expiring forms."""
    p = policy_by_number(ctx.account, ctx.request["policy_number"]) or {}
    reasons = []
    rate = p.get("renewal_rate_change_pct")
    if rate is not None and rate >= thresholds.REMARKET_RATE_CHANGE_PCT:
        reasons.append(f"indicated renewal change +{rate}% (threshold {thresholds.REMARKET_RATE_CHANGE_PCT}%)")
    lr = ((ctx.out("CSR2:loss_runs").get("loss_summary") or {}).get(p.get("line")) or {}).get("loss_ratio")
    if lr is not None and lr >= thresholds.REMARKET_LOSS_RATIO:
        reasons.append(f"loss ratio {lr} (threshold {thresholds.REMARKET_LOSS_RATIO})")
    if p.get("nonrenewal_notice"):
        reasons.append("carrier issued a non-renewal notice")
    if any(g["severity"] == "HIGH" for g in ctx.out("CSR9:dec_review").get("gaps") or []):
        reasons.append("high-severity coverage gap on the expiring dec page")
    if any(f["status"] == "listed" and f["severity"] in ("HIGH", "CRITICAL") for f in ctx.out("CSR10:exclusions").get("flags") or []):
        reasons.append("restrictive exclusion listed on the expiring forms")
    return reasons
