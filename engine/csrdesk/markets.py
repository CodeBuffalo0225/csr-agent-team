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

from . import paths, schemas, thresholds

ROOT = Path(__file__).resolve().parents[2]
MARKET_COLUMNS = ("market", "kind", "admitted", "appointed", "lines", "states", "appetite_url", "contact", "notes")
KINDS = ("carrier", "wholesaler", "mga")


def _path(p, default):
    return paths.resolve(p, default)


def load_markets(path=None) -> list:
    p = _path(path, paths.agency_dir() / "markets.csv")
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


def check_proposed(r: dict, names: dict, as_of: str):
    """The reason a proposed appetite row cannot be used, or None. Run at propose AND again at confirm, so a row
    hand-edited into the proposals file between the two is caught."""
    where = f"({r.get('carrier')}/{r.get('line')})"
    if (r.get("carrier") or "").strip() not in names:
        return f"{where}: not an appointed market in markets.csv"
    if (r.get("line") or "").strip() not in schemas.LINES:
        return f"{where}: line not in {list(schemas.LINES)}"
    if not (r.get("states") or "").strip() or not (r.get("naics_prefixes") or "").strip():
        return f"{where}: states and naics_prefixes are required (* for all)"
    if not (r.get("appetite_url") or "").strip() or not (r.get("observed") or "").strip():
        return f"{where}: needs the appetite_url it was read from and the observed date"
    try:
        schemas.check_evidence({"status": "VERIFIED", "url": r["appetite_url"].strip(), "observed": r["observed"].strip()}, as_of)
    except schemas.GateError as e:
        return f"{where}: {e.code}"
    if (r.get("am_best") or "").strip() not in thresholds.AM_BEST_ORDER:
        return f"{where}: AM Best rating missing or unrecognised (read it from AM Best, not the carrier page)"
    return None


def propose(rows_in: list, markets: list, as_of: str, proposed_path=None) -> dict:
    """Add proposed appetite rows. Each must name an appointed market, cite a URL and a date, and carry no
    confirmation (a subagent cannot confirm its own research). Identical proposals are not added twice."""
    from . import market as market_mod
    names = appointed(markets)
    errors, ok = [], []
    p = _path(proposed_path, paths.agency_dir() / "appetite.proposed.csv")
    existing = _read_csv(p)
    seen = {tuple((x.get(c) or "") for c in market_mod.APPETITE_COLUMNS) for x in existing}
    for i, r in enumerate(rows_in):
        if (r.get("confirmed_by") or "").strip():
            errors.append(f"proposed[{i}]: a proposal cannot carry confirmed_by; a person confirms it with setup confirm")
            continue
        why = check_proposed(r, names, as_of)
        if why:
            errors.append(f"proposed[{i}] {why}")
            continue
        row = {c: (r.get(c) or "") for c in market_mod.APPETITE_COLUMNS}
        key = tuple(row[c] for c in market_mod.APPETITE_COLUMNS)
        if key not in seen:
            seen.add(key)
            ok.append(row)
    if ok:
        _write_csv(p, existing + ok, market_mod.APPETITE_COLUMNS)
    return {"accepted": len(ok), "rejected": len(errors), "errors": errors}


def confirm(market_name: str, by: str = None, proposed_path=None, appetite_path=None, *, markets_rows: list = None,
            as_of: str = None, attest=None) -> dict:
    """A person at the agency confirms one market's proposed rows. Each row is re-checked; a confirmed row replaces
    any earlier confirmed row for the same market and line (a narrowed appetite does not leave the old one live).
    Human-only: an interactive terminal and the market's name typed back (humans.attest)."""
    import datetime as _dt
    from . import humans
    from . import market as market_mod
    as_of = as_of or _dt.date.today().isoformat()
    pp = _path(proposed_path, paths.agency_dir() / "appetite.proposed.csv")
    ap = _path(appetite_path, paths.appetite())
    proposed = _read_csv(pp)
    mine = [r for r in proposed if r["carrier"].strip() == market_name]
    if not mine:
        raise ValueError(f"no proposed rows for {market_name!r}")
    names = appointed(markets_rows if markets_rows is not None else (load_markets() or []))
    bad = [w for w in (check_proposed(r, names, as_of) for r in mine) if w]
    if bad:
        raise ValueError(f"proposed rows for {market_name!r} no longer pass the checks: {bad}")
    who = (attest or humans.attest)(f"Confirm {len(mine)} appetite row(s) for {market_name} (read each against its appetite_url)",
                                    market_name, by)
    for r in mine:
        r["confirmed_by"] = who
    lines = {r["line"].strip() for r in mine}
    kept = [r for r in _read_csv(ap) if not (r["carrier"].strip() == market_name and r["line"].strip() in lines)]
    _write_csv(ap, kept + mine, market_mod.APPETITE_COLUMNS)
    _write_csv(pp, [r for r in proposed if r["carrier"].strip() != market_name], market_mod.APPETITE_COLUMNS)
    return {"market": market_name, "confirmed_rows": len(mine), "by": who, "superseded_lines": sorted(lines)}
