"""CSR 3 — certificates and ACORD applications.

Two tasks:

  applications   QUOTE: the ACORD field sets each requested line feeds (intake.ACORD_FORMS), filled from the
                 masked account. Unfilled fields are listed, not invented. ACORD forms are licensed; this
                 module produces field data for the agency's licensed forms/AMS, never the form itself.

  coi            COI: the ACORD 25 field set, after these gates, in order (first failure wins):
                   1. every requested line has a policy in force today          else escalate: cannot certify
                   2. CSR 9 reviewed the dec page for every line                 else needs_data
                   3. no HIGH gap from CSR 9 (limit short, endorsement missing)  else escalate: needs endorsement
                   4. no CRITICAL flag from CSR 10 (e.g. residential exclusion)  else escalate
                   5. draft ACORD 25: ADDL INSD / SUBR WVD marked Y only where CSR 9 found the endorsement
                 A certificate is informational and does not amend coverage; the draft never says more than
                 the dec page shows. Issuing it is IRREVERSIBLE (policy.classify("issue_coi")) and needs the
                 AE's scoped approval.
"""
from __future__ import annotations

from .desk import Ctx, active_policy, draft, money, result
from .intake import ACORD_FORMS

APP_FIELDS = {
    "ACORD 125 Commercial Insurance Application": ("insured_name", "dba", "fein", "entity_type", "naics",
                                                   "description_of_operations", "years_in_business",
                                                   "mailing_address", "contact", "annual_revenue", "employees"),
    "ACORD 126 Commercial General Liability Section": ("locations", "annual_revenue", "description_of_operations"),
    "ACORD 127 Business Auto Section": ("drivers",),
    "ACORD 129 Vehicle Schedule": ("vehicles",),
    "ACORD 130 Workers Compensation Application": ("fein", "payroll", "employees", "locations"),
    "ACORD 140 Property Section": ("locations", "property_schedule"),
    "ACORD 131 Umbrella / Excess Section": ("prior_policies",),
}
OPTIONAL = ("dba",)       # blank is a valid answer; never an open field
CANCELLATION = ("Should any of the above described policies be cancelled before the expiration date thereof, "
                "notice will be delivered in accordance with the policy provisions.")


def applications(ctx: Ctx) -> dict:
    a = ctx.account
    decoded = {r["vin"]: r for r in ctx.out("CSR5:verify").get("vehicles", [])}
    forms, unfilled = {}, []
    wanted = list(ACORD_FORMS["COMMON"])
    for line in ctx.request["lines"]:
        wanted += [f for f in ACORD_FORMS.get(line, []) if f not in wanted]
    for form in wanted:
        fields = APP_FIELDS.get(form)
        if fields is None:
            forms[form] = {"status": "carrier_supplemental", "fields": {}}
            continue
        filled = {}
        for f in fields:
            v = a.get(f)
            if f == "vehicles" and v:
                v = [dict(veh, decoded=(decoded.get(veh["vin"].upper()) or {}).get("value")) for veh in v]
            filled[f] = v
            if v in (None, [], "") and f not in OPTIONAL:
                unfilled.append(f"{form}: {f}")
        forms[form] = {"status": "complete" if all(filled[f] not in (None, [], "") or f in OPTIONAL for f in fields) else "partial",
                       "fields": filled}
    summary = f"ACORD apps: {sum(v['status'] == 'complete' for v in forms.values())} of {len(forms)} complete; {len(unfilled)} field(s) open."
    return result("CSR3", "applications", "done" if not unfilled else "needs_data", summary,
                  findings={"forms": forms, "unfilled": unfilled})


def coi(ctx: Ctx) -> dict:
    a, r = ctx.account, ctx.request
    lines, req = r["lines"], r.get("requirements") or {}
    in_force = {line: active_policy(a, line, ctx.as_of) for line in lines}
    lapsed = [l for l, p in in_force.items() if p is None]
    if lapsed:
        return result("CSR3", "coi", "escalate", f"COI: no policy in force for {', '.join(lapsed)}; cannot certify.",
                      findings={"gate": "no_policy_in_force", "lines": lapsed},
                      escalate=f"certificate requested for {', '.join(lapsed)} with no policy in force")
    review = ctx.out("CSR9:dec_review")
    baseline = review.get("baseline") or {}
    unreviewed = [l for l in lines if (baseline.get(l) or {}).get("status") != "reviewed"]
    if unreviewed:
        return result("CSR3", "coi", "needs_data", f"COI: dec page not reviewed for {', '.join(unreviewed)}.",
                      findings={"gate": "dec_page_unreviewed", "lines": unreviewed})
    high = [g for g in review.get("gaps") or [] if g["severity"] == "HIGH" and g["line"] in lines]
    if high:
        return result("CSR3", "coi", "escalate", f"COI: {len(high)} requirement(s) not supported by the dec page.",
                      findings={"gate": "requirement_not_supported", "gaps": high},
                      escalate="holder requirements exceed what the policy shows: endorsement or limit change needed first")
    critical = [f for f in ctx.out("CSR10:exclusions").get("flags") or [] if f["severity"] == "CRITICAL"]
    if critical:
        return result("CSR3", "coi", "escalate", "COI: critical exclusion for this project.",
                      findings={"gate": "critical_exclusion", "flags": critical}, escalate=critical[0]["why"])

    coverages, insurers, allowed = [], {}, []
    for line in lines:
        p, b = in_force[line], baseline[line]
        letter = insurers.setdefault(p["carrier"], "ABCDEF"[len(insurers)])
        checks = {c["requirement"]: c["status"] for c in b.get("endorsement_checks", [])}
        coverages.append({"insurer": letter, "line": line, "policy_number": p["policy_number"],
                          "effective": p["effective"], "expiration": p["expiration"], "limits": b["limits"],
                          "addl_insd": "Y" if checks.get("additional_insured") == "listed" else "N",
                          "subr_wvd": "Y" if checks.get("waiver_of_subrogation") == "listed" else "N"})
        allowed += [p["policy_number"]] + [money(v) for v in b["limits"].values()]
    ai_lines = [c["line"] for c in coverages if c["addl_insd"] == "Y"]
    desc = r.get("description") or ""
    if ai_lines and req.get("additional_insured"):
        desc += (f" Certificate holder is included as additional insured on {', '.join(ai_lines)} where required by written"
                 f" contract, per the policy's endorsement.")
    if req.get("waiver_of_subrogation") and any(c["subr_wvd"] == "Y" for c in coverages):
        desc += " Waiver of subrogation applies where required by written contract, per the policy's endorsement."
    if req.get("primary_noncontributory") and any(ck.get("requirement") == "primary_noncontributory" and ck["status"] == "listed"
                                                   for b in baseline.values() for ck in b.get("endorsement_checks", [])):
        desc += " Coverage is primary and noncontributory where required by written contract, per the policy's endorsement."
    acord25 = {"form": "ACORD 25 Certificate of Liability Insurance", "insured": a["insured_name"],
               "insured_address": a.get("mailing_address"),
               "insurers": {v: k for k, v in insurers.items()}, "coverages": coverages,
               "description_of_operations": desc.strip(), "certificate_holder": {"name": r["holder_name"], "address": r["holder_address"]},
               "cancellation": CANCELLATION}
    lines_txt = "\n".join(f"  {c['line']} {c['policy_number']} {c['effective']} to {c['expiration']} | "
                          + ", ".join(f"{k} {money(v)}" for k, v in c["limits"].items())
                          + f" | AI {c['addl_insd']} | WOS {c['subr_wvd']}" for c in coverages)
    body = f"ACORD 25 draft for {r['holder_name']}:\n{lines_txt}\nDescription: {acord25['description_of_operations']}"
    allowed += [c["effective"] for c in coverages] + [c["expiration"] for c in coverages]
    allowed += [acord25["form"], r.get("description") or "", r["holder_name"]]   # fixed form name and the requester's own words
    return result("CSR3", "coi", "done", f"COI: ACORD 25 drafted for {r['holder_name']} on {', '.join(lines)}; awaiting release.",
                  findings={"gate": "passed", "acord25": acord25},
                  drafts=[draft("acord", r["holder_name"], f"Certificate of insurance - {a['insured_name']}", body,
                                "issue_coi", allowed)])
