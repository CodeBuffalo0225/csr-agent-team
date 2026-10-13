"""Market onboarding: the carriers and wholesalers an agency can actually place with, set up at deployment.

An appetite guide is worthless for a carrier the agency is not appointed with, so CSR 4 starts from the
agency's own market list, not from what carriers exist:

    data/agency/markets.csv    (template data/agency/markets.example.csv)
      market, kind (carrier | wholesaler | mga), admitted (Y/N), appointed (Y/N), lines ("GL|AUTO" or *),
      states ("NJ|NY" or *), appetite_url, contact, notes

Deployment flow (bin/csr setup ...):
  1. validate        the agency fills markets.csv; this checks it
  2. research        one research task per appointed market for the csr-04-market subagent: fetch the market's
                     public appetite page, propose appetite rows with the URL and the date read
  3. propose <file>  proposed rows land in data/agency/appetite.proposed.csv, schema-checked, never used yet
  4. confirm <market> --by <name>
                     a person at the agency confirms one market's proposed rows; they move into
                     data/appetite.csv with confirmed_by set. CSR 4 uses only confirmed rows.

Admitted first: CSR 4 ranks admitted, appointed carriers. Only when no admitted market is eligible for a line
does it open the wholesale path — appointed wholesalers/MGAs that write the line and state — and attach the
state's surplus lines diligent-effort rule (staterules.surplus_rule), because the declinations come first.
"""
from __future__ import annotations

import csv
from pathlib import Path

from . import schemas, thresholds

ROOT = Path(__file__).resolve().parents[2]
AGENCY = ROOT / "data" / "agency"
MARKETS = AGENCY / "markets.csv"
PROPOSED = AGENCY / "appetite.proposed.csv"
APPETITE = ROOT / "data" / "appetite.csv"
MARKET_COLUMNS = ("market", "kind", "admitted", "appointed", "lines", "states", "appetite_url", "contact", "notes")
KINDS = ("carrier", "wholesaler", "mga")


def _path(p, default):
    p = Path(p) if p else default
    return p if p.is_absolute() else ROOT / p


def load_markets(path=None) -> list:
    p = _path(path, MARKETS)
    if not p.exists():
        return None
    with p.open(newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if (r.get("market") or "").strip()]


def _split(v) -> list:
    return [x.strip() for x in (v or "").split("|") if x.strip()]


def validate_markets(rows: list) -> list:
    errors, seen = [], set()
    for i, r in enumerate(rows or [], start=2):
        name, where = (r.get("market") or "").strip(), f"row {i} ({r.get('market')})"
        if name.lower() in seen:
            errors.append(f"{where}: duplicate market")
        seen.add(name.lower())
        if (r.get("kind") or "").strip() not in KINDS:
            errors.append(f"{where}: kind must be one of {list(KINDS)}")
        for k in ("admitted", "appointed"):
            if (r.get(k) or "").strip().upper() not in ("Y", "N"):
                errors.append(f"{where}: {k} must be Y or N")
        lines = _split(r.get("lines"))
        if not lines or any(l not in schemas.LINES + ("*",) for l in lines):
            errors.append(f"{where}: lines must be from {list(schemas.LINES)} or *")
        states = _split(r.get("states"))
        if not states or any(s != "*" and not schemas.STATE.match(s) for s in states):
            errors.append(f"{where}: states must be two-letter codes or *")
        url = (r.get("appetite_url") or "").strip()
        if url and not schemas.URL.match(url):
            errors.append(f"{where}: appetite_url is not an http(s) URL")
        if (r.get("kind") or "").strip() == "carrier" and (r.get("admitted") or "").strip().upper() == "N" \
                and "surplus" not in (r.get("notes") or "").lower():
            errors.append(f"{where}: a non-admitted carrier is placed through a wholesaler or as surplus lines; say which in notes")
    return errors


def appointed(rows: list) -> dict:
    return {r["market"].strip(): r for r in rows or [] if (r.get("appointed") or "").strip().upper() == "Y"}


def writes(row: dict, line: str, state: str) -> bool:
    lines, states = _split(row.get("lines")), _split(row.get("states"))
    return ("*" in lines or line in lines) and ("*" in states or state in states)


def research_tasks(rows: list) -> list:
    """One task per appointed market for the csr-04-market subagent. The subagent proposes; it never confirms."""
    tasks = []
    for name, r in sorted(appointed(rows).items()):
        tasks.append({"market": name, "kind": r["kind"].strip(), "admitted": r["admitted"].strip().upper() == "Y",
                      "lines": _split(r["lines"]), "states": _split(r["states"]),
                      "start_at": (r.get("appetite_url") or "").strip() or None,
                      "instructions": ("Read this market's public appetite / class guide. For each line it writes, propose one "
                                       "appetite row (columns of data/appetite.example.csv) with the exact page URL as appetite_url "
                                       "and today's date as observed. Leave confirmed_by blank. A value the page does not state "
                                       "is blank, never a guess. No page found -> propose nothing and say so.")})
    return tasks


def _read_csv(p: Path) -> list:
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(p: Path, rows: list, columns) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def propose(rows_in: list, markets: list, as_of: str, proposed_path=None) -> dict:
    """Add proposed appetite rows. Each must name an appointed market, cite a URL and a date, and carry no
    confirmation (a subagent cannot confirm its own research)."""
    from . import market as market_mod
    names = appointed(markets)
    errors, ok = [], []
    for i, r in enumerate(rows_in):
        where = f"proposed[{i}] ({r.get('carrier')}/{r.get('line')})"
        if (r.get("carrier") or "").strip() not in names:
            errors.append(f"{where}: not an appointed market in markets.csv")
            continue
        if (r.get("confirmed_by") or "").strip():
            errors.append(f"{where}: a proposal cannot carry confirmed_by; a person confirms it with setup confirm")
            continue
        if not (r.get("appetite_url") or "").strip() or not (r.get("observed") or "").strip():
            errors.append(f"{where}: needs the appetite_url it was read from and the observed date")
            continue
        try:
            schemas.check_evidence({"status": "VERIFIED", "url": r["appetite_url"].strip(), "observed": r["observed"].strip()}, as_of)
        except schemas.GateError as e:
            errors.append(f"{where}: {e.code}")
            continue
        if (r.get("am_best") or "").strip() not in thresholds.AM_BEST_ORDER:
            errors.append(f"{where}: AM Best rating missing or unrecognised (read it from AM Best, not the carrier page)")
            continue
        ok.append({c: (r.get(c) or "") for c in market_mod.APPETITE_COLUMNS})
    if ok:
        p = _path(proposed_path, PROPOSED)
        _write_csv(p, _read_csv(p) + ok, market_mod.APPETITE_COLUMNS)
    return {"accepted": len(ok), "rejected": len(errors), "errors": errors}


def confirm(market_name: str, by: str, proposed_path=None, appetite_path=None) -> dict:
    """A named person confirms one market's proposed rows; they move into the live appetite file."""
    from . import market as market_mod
    if not (by or "").strip():
        raise ValueError("confirm needs --by <the person at the agency confirming it>")
    pp, ap = _path(proposed_path, PROPOSED), _path(appetite_path, APPETITE)
    proposed = _read_csv(pp)
    mine = [r for r in proposed if r["carrier"].strip() == market_name]
    if not mine:
        raise ValueError(f"no proposed rows for {market_name!r}")
    for r in mine:
        r["confirmed_by"] = by.strip()
    _write_csv(ap, _read_csv(ap) + mine, market_mod.APPETITE_COLUMNS)
    _write_csv(pp, [r for r in proposed if r["carrier"].strip() != market_name], market_mod.APPETITE_COLUMNS)
    return {"market": market_name, "confirmed_rows": len(mine), "by": by.strip()}
