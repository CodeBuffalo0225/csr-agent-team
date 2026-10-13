"""CSR 9 — declarations-page review. What does the in-force or expiring coverage actually say, and
where does it fall short of what the contract or request needs?

Works from the structured dec page on the policy record (limits, deductibles, forms, endorsements).
Extracting that structure from a PDF is the CSR 9 subagent's job; this module refuses to guess when
it is absent: no dec page -> `needs_data` and a request to CSR 5, never "limits look fine".

Gaps are findings for the AE, not coverage opinions. "Endorsement not listed on the dec page" means
"not listed" — the full policy may carry it; the AE or the carrier confirms.
"""
from __future__ import annotations

import re

from .desk import Ctx, active_policy, message, money, policy_by_number, result, ticket_lines

# Requirement -> form numbers / phrases whose presence on the dec page's forms or endorsements list
# evidences it. ISO form numbers; carriers also use proprietary blanket forms, matched by phrase.
ENDORSEMENT_EVIDENCE = {
    "additional_insured": ("CG 20 10", "CG 20 26", "CG 20 33", "CG 20 38", "CG 20 37", "CA 20 48",
                           "additional insured"),
    "waiver_of_subrogation": ("CG 24 04", "CA 04 44", "WC 00 03 13", "waiver of subrogation",
                              "waiver of transfer of rights"),
    "primary_noncontributory": ("CG 20 01", "primary and noncontributory", "primary and non-contributory"),
}
CLAIMS_MADE = ("CG 00 02", "claims-made", "claims made")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("-", " ")).strip().lower()


def endorsement_status(dec: dict, requirement: str) -> dict:
    listed = [_norm(x) for x in (dec.get("forms") or []) + (dec.get("endorsements") or [])]
    for needle in ENDORSEMENT_EVIDENCE[requirement]:
        n = _norm(needle)
        hit = next((x for x in listed if n in x), None)
        if hit:
            return {"requirement": requirement, "status": "listed", "matched": hit}
    return {"requirement": requirement, "status": "not_listed", "matched": None}


def _target_policies(ctx: Ctx) -> dict:
    """{line: policy} the review is about: in force for QUOTE/COI/SERVICE/CLAIM, the named one for RENEWAL."""
    t, a = ctx.ticket, ctx.account
    if t["type"] == "RENEWAL":
        p = policy_by_number(a, t["request"]["policy_number"])
        return {p["line"]: p} if p else {}
    on = t["request"].get("date_of_loss") or ctx.as_of
    lines = ticket_lines(t) or sorted({p["line"] for p in a.get("prior_policies") or []})
    return {line: active_policy(a, line, on) for line in lines}


def _requirements(ctx: Ctx) -> dict:
    r = ctx.request
    return (r.get("requirements") if ctx.ticket["type"] == "COI" else r.get("contract_requirements")) or {}


def run(ctx: Ctx) -> dict:
    tid, a = ctx.ticket["ticket_id"], ctx.account
    targets, req = _target_policies(ctx), _requirements(ctx)
    baseline, gaps, missing_decs, msgs = {}, [], [], []
    umbrella = active_policy(a, "UMBRELLA", ctx.as_of)
    umb_occ = ((umbrella or {}).get("dec_page") or {}).get("limits", {}).get("each_occurrence")

    for line, pol in targets.items():
        if pol is None:
            baseline[line] = {"status": "no_policy_in_force"}
            continue
        dec = pol.get("dec_page")
        if dec is None:
            baseline[line] = {"status": "dec_page_missing", "policy_number": pol["policy_number"], "carrier": pol["carrier"]}
            missing_decs.append(f"{line} {pol['carrier']} {pol['policy_number']}")
            continue
        baseline[line] = {"status": "reviewed", "policy_number": pol["policy_number"], "carrier": pol["carrier"],
                          "effective": pol["effective"], "expiration": pol["expiration"],
                          "limits": dec.get("limits") or {}, "deductibles": dec.get("deductibles") or {},
                          "forms": dec.get("forms") or [], "endorsements": dec.get("endorsements") or []}
        listed = " | ".join(_norm(x) for x in (dec.get("forms") or []))
        if line == "GL" and any(_norm(c) in listed for c in CLAIMS_MADE):
            gaps.append({"line": line, "kind": "claims_made_form", "severity": "MEDIUM",
                         "detail": "GL is written claims-made: confirm retro date and tail before any carrier change"})
        for key, required in (req.get("limits") or {}).items():
            req_line, name = key.split(".", 1)
            if req_line != line:
                continue
            have = (dec.get("limits") or {}).get(name)
            if have is None:
                gaps.append({"line": line, "kind": "limit_not_on_dec", "severity": "HIGH", "limit": name,
                             "required": required, "have": None,
                             "detail": f"{line} {name} required at {money(required)}; not shown on the dec page"})
            elif have < required:
                short = required - have
                via_umbrella = umb_occ is not None and line in ("GL", "AUTO") and umb_occ >= short
                gaps.append({"line": line, "kind": "limit_shortfall", "severity": "MEDIUM" if via_umbrella else "HIGH",
                             "limit": name, "required": required, "have": have,
                             "umbrella_each_occurrence": umb_occ if via_umbrella else None,
                             "detail": f"{line} {name} is {money(have)} against {money(required)} required"
                                       + (f"; umbrella {money(umb_occ)} may close it if it follows form (verify)" if via_umbrella else "")})
        if line in ("GL", "AUTO", "WC", "BOP"):
            for flag in ("additional_insured", "waiver_of_subrogation", "primary_noncontributory"):
                if not req.get(flag):
                    continue
                if flag == "primary_noncontributory" and line == "WC":
                    continue
                if flag == "additional_insured" and line == "WC":
                    continue
                st = endorsement_status(dec, flag)
                if st["status"] == "not_listed":
                    gaps.append({"line": line, "kind": "endorsement_not_listed", "severity": "HIGH", "requirement": flag,
                                 "detail": f"{line}: {flag.replace('_', ' ')} required; no supporting endorsement listed on the dec page"})
                baseline[line].setdefault("endorsement_checks", []).append(st)

    if missing_decs:
        msgs.append(message("CSR9", "CSR5", "REQUEST", tid, "dec pages needed",
                            "Please obtain the current declarations page(s): " + "; ".join(missing_decs)))
    if gaps:
        msgs.append(message("CSR9", "CSR10", "NOTIFY", tid, "coverage gaps found",
                            f"{len(gaps)} gap(s) on the dec pages; review exclusions on the same forms."))
    high = sum(g["severity"] == "HIGH" for g in gaps)
    status = "needs_data" if missing_decs else "done"
    summary = (f"Dec pages: {sum(b.get('status') == 'reviewed' for b in baseline.values())} reviewed"
               + (f", {len(missing_decs)} missing" if missing_decs else "")
               + f"; {len(gaps)} gap(s), {high} high.")
    return result("CSR9", "dec_review", status, summary,
                  findings={"baseline": baseline, "gaps": gaps, "missing_dec_pages": missing_decs}, messages=msgs)
