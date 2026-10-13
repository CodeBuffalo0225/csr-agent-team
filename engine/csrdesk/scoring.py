"""Fork gate: the arithmetic no agent is allowed to do.

Market fit (MODEL_VERSION csr-market-v1). CSR 4 supplies five judgments, each an integer 0-5, after
reading a carrier's appetite guide (schemas.check_judgment). The weighted total is computed here:

    total = sum over the five dimensions of (score / 5) * weight        range 0.0 - 100.0, one decimal

    dimension          weight   what the 0-5 judges
    appetite_fit         30     the class, state and size sit inside the carrier's written appetite
    coverage_breadth     20     the carrier's form covers what the dec-page review (CSR 9/10) says this risk needs
    loss_history_fit     20     the computed loss history is one this carrier writes
    program_fit          15     a class program or specialty form exists for this operation
    service_fit          15     claims and service capability the AE trusts for this account
                        ---
                        100

Appetite carries the most weight because a submission outside appetite wastes a market the agency
may need next year. Hard filters (line, state, revenue band, AM Best floor) run before any judgment,
in market.py; a carrier that fails one is never scored at all.

Loss math, ages and the ISO size class are here too, so a prompt never has to "work out" a loss
ratio or how many days are left on a policy.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from . import schemas, thresholds

MODEL_VERSION = "csr-market-v1"

WEIGHTS = {
    "appetite_fit": 30,
    "coverage_breadth": 20,
    "loss_history_fit": 20,
    "program_fit": 15,
    "service_fit": 15,
}


def age_days(observed: Any, as_of: Any) -> int:
    """Spine S4: as_of minus observed, in whole days, from dates alone. No model input states an age."""
    return (schemas.as_date(as_of, "as_of") - schemas.as_date(observed, "observed")).days


def days_until(when: Any, as_of: Any) -> int:
    """Days from as_of until `when` (negative once it has passed)."""
    return -age_days(when, as_of)


def score_market(payload: dict) -> dict:
    """Total one carrier. Parses its own input, so a caller cannot skip the schema gate."""
    schemas.check_judgment(payload)
    dimensions: dict = {}
    scaled = 0
    for dim in schemas.JUDGMENT_DIMS:
        score, weight = payload["scores"][dim], WEIGHTS[dim]
        dimensions[dim] = {"score": score, "weight": weight, "points": round(score * weight / 5, 2)}
        scaled += score * weight
    return {"carrier": payload["carrier"], "dimensions": dimensions, "total": round(scaled / 5, 1),
            "model_version": MODEL_VERSION}


def admit(items: Iterable[dict], floor: float, cap: int) -> list:
    """Spine S7: drop items below the floor, sort by score descending then id ascending, admit the
    first `cap`. Room under the cap never admits an item below the floor; a negative cap admits nothing."""
    eligible = [i for i in items if i["score"] >= floor]
    eligible.sort(key=lambda i: (-i["score"], i["id"]))
    return eligible[:max(int(cap), 0)]


# ── Loss history ──────────────────────────────────────────────────────────────

def loss_summary(loss_runs: list, premiums: dict) -> dict:
    """Per line: claim count, open count, paid, reserved, incurred (paid + reserved), large losses
    (incurred >= LARGE_LOSS_USD), and a loss ratio over the loss-run window when the premium for the
    same window is known. Unknown premium -> loss_ratio None (never 0: an unknown is not a benign
    value). `premiums` is {line: total premium over the window}."""
    out: dict = {}
    for run in loss_runs:
        line = run["line"]
        s = out.setdefault(line, {"claims": 0, "open": 0, "paid": 0.0, "reserved": 0.0, "incurred": 0.0,
                                  "large_losses": [], "carriers": []})
        if run["carrier"] not in s["carriers"]:
            s["carriers"].append(run["carrier"])
        for c in run["claims"]:
            incurred = float(c["paid"]) + float(c["reserved"])
            s["claims"] += 1
            s["open"] += c["status"] == "OPEN"
            s["paid"] += float(c["paid"])
            s["reserved"] += float(c["reserved"])
            s["incurred"] += incurred
            if incurred >= thresholds.LARGE_LOSS_USD:
                s["large_losses"].append({"date_of_loss": c["date_of_loss"], "incurred": incurred,
                                          "status": c["status"], "description": c["description"]})
    for line, s in out.items():
        prem = premiums.get(line)
        s["loss_ratio"] = round(s["incurred"] / prem, 3) if prem else None
        for k in ("paid", "reserved", "incurred"):
            s[k] = round(s[k], 2)
    return out


# ── Commercial auto ───────────────────────────────────────────────────────────

_GVWR = re.compile(r"([\d,]+)\s*(?:-\s*([\d,]+))?\s*lb", re.I)


def size_class(gvwr_text: str, stated_gvw: int = None) -> dict:
    """ISO size class from NHTSA's GVWR band ("Class 6: 19,501 - 26,000 lb ...") or a stated GVW.
    When the band straddles a class boundary the class is UNRESOLVED and names both candidates: the
    client must confirm the GVW on the door-jamb sticker. Rating on the lower class would under-rate
    the risk; rating on the upper one overcharges the client. Neither is the engine's call."""
    m = _GVWR.search(gvwr_text or "")
    lo = hi = None
    if m:
        lo = int(m.group(1).replace(",", ""))
        open_ended = re.search(r"and above|or more|\+", (gvwr_text or "")[m.end():m.end() + 12] + (gvwr_text or "")[m.start():m.end()], re.I)
        hi = float("inf") if open_ended and not m.group(2) else int((m.group(2) or m.group(1)).replace(",", ""))
    if stated_gvw:
        if lo is not None and not lo <= stated_gvw <= hi:
            return {"size_class": None, "basis": "stated_gvw_conflicts_with_nhtsa", "candidates": [_class_for(stated_gvw), _class_for(lo)]}
        return {"size_class": _class_for(stated_gvw), "basis": "stated_gvw", "candidates": None}
    if not m:
        return {"size_class": None, "basis": "unknown", "candidates": None}
    a, b = _class_for(lo), _class_for(hi)
    if a == b:
        return {"size_class": a, "basis": "nhtsa_gvwr_band", "candidates": None}
    return {"size_class": None, "basis": "band_straddles_boundary", "candidates": [a, b]}


def _class_for(gvw: int) -> str:
    return next(name for limit, name in thresholds.SIZE_CLASSES if gvw <= limit)
