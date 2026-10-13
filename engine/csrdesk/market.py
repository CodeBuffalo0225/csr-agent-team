"""CSR 4 — carrier research and market advice: where should each line go to market?

The agency's own appetite file is the only source (config "appetite", default data/appetite.csv). No
appetite file -> needs_key/needs_data, never a list of carriers from memory: a model "recalling" a
carrier's appetite is how a submission lands with a market that left the class two years ago.

    1. hard filters (code, logged per drop): line, state, NAICS prefix, revenue band, AM Best >= floor,
       appetite row verified within APPETITE_MAX_AGE_DAYS
    2. CSR 4 subagent judges each survivor on five 0-5 dimensions (schemas.check_judgment)
    3. scoring.score_market totals them; scoring.admit keeps >= MARKET_FLOOR, top MARKET_CAP

Without judgments the step returns the filtered list as `needs_judgment`: eligible, not ranked.

appetite.csv columns: carrier,line,states,naics_prefixes,min_revenue,max_revenue,am_best,appetite_url,observed,notes
  states / naics_prefixes are "|"-separated or "*"; revenue blank = unbounded; observed = YYYY-MM-DD.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from . import renewals, schemas, scoring, thresholds
from .desk import Ctx, message, result, ticket_lines

ROOT = Path(__file__).resolve().parents[2]


def load_appetite(path) -> list:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return None
    with p.open(newline="", encoding="utf-8") as f:
        return [row for row in csv.DictReader(f) if (row.get("carrier") or "").strip()]


def _best_ok(rating: str) -> bool:
    order = thresholds.AM_BEST_ORDER
    r = (rating or "").strip()
    return r in order and order.index(r) <= order.index(thresholds.AM_BEST_FLOOR)


def filter_markets(rows: list, line: str, state: str, naics: str, revenue, as_of: str) -> tuple:
    kept, dropped = [], []
    for row in rows:
        why = None
        states = [s.strip() for s in row["states"].split("|")]
        prefixes = [s.strip() for s in row["naics_prefixes"].split("|")]
        if row["line"].strip() != line:
            continue
        if "*" not in states and state not in states:
            why = f"not writing in {state}"
        elif "*" not in prefixes and not any(naics.startswith(p) for p in prefixes):
            why = f"NAICS {naics} outside appetite"
        elif revenue is not None and row.get("min_revenue") and revenue < float(row["min_revenue"]):
            why = "revenue below band"
        elif revenue is not None and row.get("max_revenue") and revenue > float(row["max_revenue"]):
            why = "revenue above band"
        elif not _best_ok(row.get("am_best")):
            why = f"AM Best {row.get('am_best') or 'unrated'} below {thresholds.AM_BEST_FLOOR}"
        elif not row.get("observed"):
            why = "appetite row has no verified date"
        elif scoring.age_days(row["observed"], as_of) > thresholds.APPETITE_MAX_AGE_DAYS:
            why = f"appetite last verified {row['observed']}: stale"
        if why:
            dropped.append({"carrier": row["carrier"], "line": line, "why": why})
        else:
            kept.append({"carrier": row["carrier"], "line": line, "am_best": row["am_best"],
                         "evidence": schemas.check_evidence(
                             {"status": "VERIFIED" if row.get("appetite_url") else "INFERENCE",
                              "url": row.get("appetite_url") or None, "observed": row["observed"]}, as_of),
                         "notes": row.get("notes") or ""})
    return kept, dropped


def run(ctx: Ctx) -> dict:
    a, tid = ctx.account, ctx.ticket["ticket_id"]
    why = renewals.remarket_reasons(ctx) if ctx.ticket["type"] == "RENEWAL" else []
    lead = f"Remarket ({'; '.join(why)}). " if why else ""
    intake = ctx.out("CSR1:intake")
    lines = intake["ready_lines"] if intake else ticket_lines(ctx.ticket)
    rows = load_appetite(ctx.config.get("appetite", "data/appetite.csv"))
    if rows is None:
        return result("CSR4", "market", "needs_data",
                      lead + "Market: no agency appetite file loaded; CSR 4 will not recommend carriers from memory.",
                      findings={"status": "needs_key", "value": None, "lines": lines, "remarket_reasons": why,
                                "fix": "load the agency's appetite guide into data/appetite.csv (see market.py docstring)"})
    if not a.get("naics"):
        return result("CSR4", "market", "needs_data", lead + "Market: NAICS not set; appetite cannot be filtered.",
                      findings={"status": "needs_data", "value": None, "lines": lines, "remarket_reasons": why})
    state = (a.get("mailing_address") or {}).get("state")
    judgments = {(j["carrier"]): j for j in ctx.config.get("judgments", [])}
    by_line, all_dropped, needs_judgment = {}, [], False
    for line in lines:
        kept, dropped = filter_markets(rows, line, state, a["naics"], a.get("annual_revenue"), ctx.as_of)
        all_dropped += dropped
        scored = []
        for k in kept:
            j = judgments.get(k["carrier"])
            if j is None:
                needs_judgment = True
                continue
            s = scoring.score_market(j)
            scored.append({"id": k["carrier"], "score": s["total"], "detail": s, **k})
        admitted = scoring.admit(scored, thresholds.MARKET_FLOOR, thresholds.MARKET_CAP)
        by_line[line] = {"eligible": [k["carrier"] for k in kept], "ranked": admitted,
                         "below_floor_or_capped": [s["id"] for s in scored if s not in admitted]}
    msgs = [message("CSR4", "AE", "NOTIFY", tid, "market list ready" if not needs_judgment else "eligible markets filtered",
                    "; ".join(f"{l}: {len(v['ranked']) or len(v['eligible'])} market(s)" for l, v in by_line.items()))]
    summary = lead + "Market: " + "; ".join(f"{l} {len(v['eligible'])} eligible" + (f", {len(v['ranked'])} ranked" if v["ranked"] else "")
                                     for l, v in by_line.items()) + f"; {len(all_dropped)} dropped by rule."
    return result("CSR4", "market", "needs_data" if needs_judgment else "done", summary,
                  findings={"status": "needs_judgment" if needs_judgment else "ranked", "by_line": by_line, "remarket_reasons": why,
                            "dropped": all_dropped, "floor": thresholds.MARKET_FLOOR, "cap": thresholds.MARKET_CAP},
                  messages=msgs)


APPETITE_COLUMNS = ("carrier", "line", "states", "naics_prefixes", "min_revenue", "max_revenue", "am_best",
                    "appetite_url", "observed", "notes")


def validate_appetite(path, as_of: str) -> dict:
    """Check the agency's appetite file before CSR 4 relies on it. Every problem is listed by row; the file
    is `ok` only with no problems. Stale rows are warnings: filter_markets already drops them."""
    rows = load_appetite(path)
    if rows is None:
        return {"ok": False, "status": "needs_key", "rows": 0, "errors": [f"{path} not found"], "warnings": []}
    p = Path(path) if Path(path).is_absolute() else ROOT / path
    with p.open(newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), [])
    errors, warnings = [], []
    missing = [c for c in APPETITE_COLUMNS if c not in header]
    if missing:
        errors.append(f"header missing {missing}")
    for i, r in enumerate(rows, start=2):
        where = f"row {i} ({r.get('carrier')}/{r.get('line')})"
        if (r.get("line") or "").strip() not in schemas.LINES:
            errors.append(f"{where}: line not in {list(schemas.LINES)}")
        for k in ("states", "naics_prefixes"):
            if not (r.get(k) or "").strip():
                errors.append(f"{where}: {k} blank (use * for all)")
        bad_states = [x for x in (r.get("states") or "").split("|") if x.strip() not in ("*",) and not schemas.STATE.match(x.strip())]
        if bad_states:
            errors.append(f"{where}: bad state code(s) {bad_states}")
        for k in ("min_revenue", "max_revenue"):
            v = (r.get(k) or "").strip()
            try:
                v and float(v)
            except ValueError:
                errors.append(f"{where}: {k} is not a number")
        if (r.get("am_best") or "").strip() not in thresholds.AM_BEST_ORDER:
            errors.append(f"{where}: AM Best rating {r.get('am_best')!r} not recognised")
        try:
            schemas.check_evidence({"status": "VERIFIED" if (r.get("appetite_url") or "").strip() else "INFERENCE",
                                    "url": (r.get("appetite_url") or "").strip() or None,
                                    "observed": (r.get("observed") or "").strip() or None}, as_of)
            if not (r.get("observed") or "").strip():
                errors.append(f"{where}: observed date blank (when was this appetite verified?)")
            elif scoring.age_days(r["observed"].strip(), as_of) > thresholds.APPETITE_MAX_AGE_DAYS:
                warnings.append(f"{where}: verified {r['observed']}, stale — CSR 4 will skip it")
        except schemas.GateError as e:
            errors.append(f"{where}: {e.code} {e.detail}")
    if not rows:
        errors.append("no carrier rows: fill it from your carrier appetite guides")
    return {"ok": not errors, "status": "ok" if not errors else ("empty" if not rows else "invalid"), "rows": len(rows),
            "errors": errors, "warnings": warnings}


def load_judgments(path) -> list:
    """A file of CSR 4 judgments, each through the schema gate. One bad judgment fails the file."""
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    return [schemas.check_judgment(j, f"$[{i}]") for i, j in enumerate(items)]
