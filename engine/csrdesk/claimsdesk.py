"""CSR 7 — claims. First notice of loss, packaged for the carrier, and the AE told when it matters.

if/else, first match wins:
  no policy for the line in force on the date of loss   -> escalate (coverage question; never "not covered" to the client)
  injury, or estimated loss >= CLAIM_URGENT_USD          -> URGENT: FNOL drafted, AE paged now
  reported > LATE_NOTICE_DAYS after the loss             -> late-notice flag on the FNOL; AE reviews before submission
  otherwise                                              -> FNOL drafted, AE approves submission

The FNOL is drafted, never filed: filing is IRREVERSIBLE (policy.classify("file_claim")). Every claim
also NOTIFYs CSR 6 (renewal pricing will move), CSR 2 (loss runs re-ordered once the carrier reserves)
and CSR 8 (the record).
"""
from __future__ import annotations

from . import scoring, thresholds
from .desk import Ctx, active_policy, draft, message, money, result

FNOL_FORM = {"PROPERTY": "ACORD 1 Property Loss Notice", "BOP": "ACORD 1 Property Loss Notice",
             "INLAND_MARINE": "ACORD 1 Property Loss Notice", "AUTO": "ACORD 2 Automobile Loss Notice",
             "GL": "ACORD 3 Liability Notice of Occurrence / Claim", "LIQUOR": "ACORD 3 Liability Notice of Occurrence / Claim",
             "UMBRELLA": "ACORD 3 Liability Notice of Occurrence / Claim", "WC": "state First Report of Injury"}


def run(ctx: Ctx) -> dict:
    a, r, tid = ctx.account, ctx.request, ctx.ticket["ticket_id"]
    pol = active_policy(a, r["line"], r["date_of_loss"])
    lag = scoring.age_days(r["date_of_loss"], ctx.as_of)
    notify = [message("CSR7", "CSR6", "NOTIFY", tid, "new claim on account", f"{r['line']} loss {r['date_of_loss']}: renewal pricing affected."),
              message("CSR7", "CSR2", "NOTIFY", tid, "loss runs will change", f"Re-order {r['line']} loss runs after the carrier sets a reserve."),
              message("CSR7", "CSR8", "NOTIFY", tid, "claim to record", f"{r['line']} loss {r['date_of_loss']} reported {ctx.as_of}.")]
    if pol is None:
        return result("CSR7", "fnol", "escalate", f"Claim: no {r['line']} policy in force on {r['date_of_loss']}.",
                      findings={"policy_number": None, "urgency": "COVERAGE_QUESTION", "report_lag_days": lag},
                      messages=notify, escalate=f"loss on {r['date_of_loss']} has no {r['line']} policy in force: AE handles, client is not told 'not covered'")
    est = r.get("estimated_amount")
    urgent = r["injury"] or (est is not None and est >= thresholds.CLAIM_URGENT_USD)
    late = lag > thresholds.LATE_NOTICE_DAYS
    urgency = "URGENT" if urgent else ("LATE_NOTICE" if late else "ROUTINE")
    form = FNOL_FORM.get(r["line"], "carrier loss notice")
    body = (f"{form}\nInsured: {a['insured_name']}\nPolicy: {pol['carrier']} {pol['policy_number']} "
            f"({pol['effective']} to {pol['expiration']})\nDate of loss: {r['date_of_loss']}\nReported: {ctx.as_of}\n"
            f"Injury: {'yes' if r['injury'] else 'no'}\nEstimated amount: {money(est) if est is not None else 'unknown'}\n"
            f"Description: {r['description']}\nReported by: {r.get('reported_by') or 'insured'}")
    allowed = [form, pol["policy_number"], pol["effective"], pol["expiration"], r["date_of_loss"], ctx.as_of, r["description"]]
    if est is not None:
        allowed.append(money(est))
    return result("CSR7", "fnol", "escalate" if urgent else "done",
                  f"Claim: {urgency} {r['line']} FNOL drafted on {pol['policy_number']}"
                  + (f"; reported {lag} days after loss" if late else "") + ".",
                  findings={"policy_number": pol["policy_number"], "carrier": pol["carrier"], "urgency": urgency,
                            "report_lag_days": lag, "late_notice": late, "form": form},
                  messages=notify,
                  drafts=[draft("fnol", f"{pol['carrier']} claims", f"FNOL - {a['insured_name']} - {r['date_of_loss']}",
                                body, "file_claim", allowed)],
                  escalate=("injury reported" if r["injury"] else f"estimated loss {money(est)}") if urgent else None)
