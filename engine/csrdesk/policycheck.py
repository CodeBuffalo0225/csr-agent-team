"""CSR 11 — policy checking. Does the policy the carrier issued match what the client was sold?

The biggest E&O control in an agency, and the one the ten-CSR design left out. The issued policy's dec page
(transcribed by CSR 9) is compared field by field with the proposal or binder. Every difference is a finding;
the policy does not go to the client until the AE clears them.

  HIGH  a limit lower than proposed or missing · a deductible higher than proposed · a proposed form or
        endorsement not on the policy · an exclusion on the policy the proposal did not show ·
        a different carrier or policy term
  LOW   a limit higher or a deductible lower than proposed (better for the client, still a mismatch to note)
  premium difference: reported with the computed amount; HIGH when the policy costs more than proposed

No dec page on the issued policy -> needs_data (CSR 9 transcribes it first), never "matches".
"""
from __future__ import annotations

import re

from .desk import Ctx, message, money, policy_by_number, result


def _norm(x: str) -> str:
    return re.sub(r"\s+", " ", x.replace("-", " ")).strip().lower()


def _contains(listed: list, wanted: str) -> bool:
    """A form matches on its number when it has one ("CG 20 10 ..." vs "CG 20 10 04 13"), else on its words."""
    w = _norm(wanted)
    num = re.match(r"^([a-z]{2} \d{2} \d{2})", w)
    key = num.group(1) if num else w
    return any(key in _norm(x) for x in listed)


def run(ctx: Ctx) -> dict:
    a, r, tid = ctx.account, ctx.request, ctx.ticket["ticket_id"]
    prop = r["proposal"]
    pol = policy_by_number(a, r["policy_number"])
    if pol is None:
        return result("CSR11", "policy_check", "escalate", f"Policy check: {r['policy_number']} not on the account.",
                      findings={"policy_number": r["policy_number"], "mismatches": []},
                      escalate="issued policy number is not on the account record")
    dec = pol.get("dec_page")
    if dec is None:
        return result("CSR11", "policy_check", "needs_data", f"Policy check: no dec page on {pol['policy_number']} yet.",
                      findings={"policy_number": pol["policy_number"], "mismatches": []},
                      messages=[message("CSR11", "CSR9", "REQUEST", tid, "transcribe issued dec page",
                                        f"Issued {pol['line']} policy {pol['policy_number']} needs its dec page transcribed for policy checking.")])
    mm = []

    def add(sev, field, proposed, issued, detail):
        mm.append({"severity": sev, "field": field, "proposed": proposed, "issued": issued, "detail": detail})

    for f in ("line", "carrier", "effective", "expiration"):
        if prop[f] != pol[f]:
            add("HIGH", f, prop[f], pol[f], f"{f} differs: proposed {prop[f]}, issued {pol[f]}")
    limits = dec.get("limits") or {}
    for name, want in (prop.get("limits") or {}).items():
        have = limits.get(name)
        if have is None:
            add("HIGH", f"limit.{name}", want, None, f"{name} proposed at {money(want)}; not on the issued policy")
        elif have < want:
            add("HIGH", f"limit.{name}", want, have, f"{name} issued at {money(have)}, proposed {money(want)}")
        elif have > want:
            add("LOW", f"limit.{name}", want, have, f"{name} issued at {money(have)}, above the proposed {money(want)}")
    deds = dec.get("deductibles") or {}
    for name, want in (prop.get("deductibles") or {}).items():
        have = deds.get(name)
        if have is None:
            add("HIGH", f"deductible.{name}", want, None, f"{name} deductible proposed at {money(want)}; not shown on the policy")
        elif have > want:
            add("HIGH", f"deductible.{name}", want, have, f"{name} deductible issued at {money(have)}, proposed {money(want)}")
        elif have < want:
            add("LOW", f"deductible.{name}", want, have, f"{name} deductible issued at {money(have)}, below the proposed {money(want)}")
    listed = (dec.get("forms") or []) + (dec.get("endorsements") or [])
    for f in (prop.get("forms") or []) + (prop.get("endorsements") or []):
        if not _contains(listed, f):
            add("HIGH", "form", f, None, f"proposed {f} is not on the issued policy")
    for x in dec.get("exclusions") or []:
        if not _contains(prop.get("exclusions") or [], x):
            add("HIGH", "exclusion", None, x, f"issued policy carries {x}, which the proposal did not show")
    if prop.get("premium") is not None and pol.get("premium") is not None and prop["premium"] != pol["premium"]:
        diff = pol["premium"] - prop["premium"]
        add("HIGH" if diff > 0 else "LOW", "premium", prop["premium"], pol["premium"],
            f"premium issued at {money(pol['premium'])}, proposed {money(prop['premium'])} ({'+' if diff > 0 else '-'}{money(abs(diff))})")
    high = [m for m in mm if m["severity"] == "HIGH"]
    msgs = [message("CSR11", "CSR8", "NOTIFY", tid, "policy check complete", f"{len(mm)} mismatch(es), {len(high)} high.")]
    if high:
        msgs.append(message("CSR11", "CSR3", "NOTIFY", tid, "hold certificates on this policy",
                            f"{pol['policy_number']} has {len(high)} unresolved mismatch(es); certify from the issued dec page only."))
    return result("CSR11", "policy_check", "escalate" if high else "done",
                  f"Policy check: {pol['policy_number']} — {len(mm)} mismatch(es), {len(high)} high"
                  + ("; hold before it goes to the client." if high else "; matches the proposal."),
                  findings={"policy_number": pol["policy_number"], "mismatches": mm},
                  messages=msgs, escalate=f"{len(high)} issued-vs-proposed mismatch(es) on {pol['policy_number']}" if high else None)
