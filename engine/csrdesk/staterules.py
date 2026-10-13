"""State notice rules: cancellation and non-renewal notice periods, as the agency's compliance owner loads them.

These are law, they differ by state and by line, and they change. So the desk ships with none: a value
here that nobody verified would be a legal claim the engine made up. The agency fills
data/state_rules.csv (template: data/state_rules.example.csv), one row per state and line, each with
the statute or bulletin URL and the date someone verified it.

    rule_for(state, line, as_of)  -> {"status": "ok", "value": {...}, "evidence": {...}}       confirmed by compliance
                                     {"status": "unconfirmed", ...}   read from the cited source, not yet confirmed
                                     {"status": "needs_data", "value": None, "why": ...}   no verified row
                                     {"status": "stale", ...}                              verified > RULES_MAX_AGE_DAYS ago
    surplus_rule(state, as_of)    -> diligent-effort declinations before a surplus lines placement, same statuses

The NJ and NY rows shipped in data/state_rules.csv and data/surplus_lines_rules.csv were researched from the
regulation and statute text on 2026-10-13 (sources in each row). They stay "unconfirmed" until the agency's
compliance owner checks them and runs `bin/csr rules confirm <STATE>` at a terminal; the `confirmed_by` column in a
rules file is not trusted (anyone can type a name); only <home>/rule_confirmations.csv, bound to a digest, counts.

CSR 6 attaches the result to every renewal, so the AE sees the configured notice windows next to the
days left, or sees plainly that they are not loaded. The desk never interprets what a missed window means.

state_rules.csv columns: state,line,nonrenewal_notice_days,cancel_nonpay_notice_days,cancel_other_notice_days,source_url,verified,confirmed_by,notes
surplus_lines_rules.csv columns: state,declinations_required,export_list,source_url,verified,confirmed_by,notes
  line may be "*" for every line; a line-specific row wins over "*".
"""
from __future__ import annotations

import csv
from pathlib import Path

from . import paths, schemas, scoring

ROOT = Path(__file__).resolve().parents[2]
NOTICE_FILE = "state_rules.csv"
SURPLUS_FILE = "surplus_lines_rules.csv"
RULES_MAX_AGE_DAYS = 365          # a rule verified longer ago than this is re-verified before it is shown as current
DAY_FIELDS = ("nonrenewal_notice_days", "cancel_nonpay_notice_days", "cancel_other_notice_days")


KEYS = {NOTICE_FILE: ("state", "line"), SURPLUS_FILE: ("state",)}
CONFIRMATIONS = "rule_confirmations.csv"
CONFIRM_COLUMNS = ("state", "confirmed_by", "confirmed_on", "digest")


def _read(p: Path) -> list:
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if (r.get("state") or "").strip()]


def load(path=None, name: str = NOTICE_FILE) -> list:
    """With `path`, that file. Otherwise the shipped researched rows, overlaid by the agency's own rows from
    <home>/<name> (an agency row replaces the shipped row for the same state and line). Shipped files are never
    edited, so a plugin update reaches every agency; a changed row voids its state's confirmation (see confirm)."""
    if path:
        p = Path(path).expanduser()
        return _read(p if p.is_absolute() else paths.home() / p)
    rows = {tuple(r[k].strip() for k in KEYS[name]): r for r in _read(paths.SHIPPED / name)}
    own = paths.home() / name
    if own.resolve() != (paths.SHIPPED / name).resolve():
        for r in _read(own):
            rows[tuple(r[k].strip() for k in KEYS[name])] = dict(r, _origin="agency")
    return list(rows.values())


def state_digest(state: str) -> str:
    """What a confirmation is bound to: every notice and surplus row for the state, minus bookkeeping columns."""
    import hashlib
    import json
    body = []
    for name in (NOTICE_FILE, SURPLUS_FILE):
        for r in load(None, name):
            if r["state"].strip() == state:
                body.append({k: (v or "").strip() for k, v in sorted(r.items()) if k not in ("confirmed_by", "_origin")})
    body.sort(key=lambda r: json.dumps(r, sort_keys=True))
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def confirmations() -> dict:
    return {r["state"].strip(): r for r in _read(paths.home() / CONFIRMATIONS)}


def validate(rows: list, as_of: str) -> list:
    """Row problems, as strings. A row with any problem is never used."""
    errors = []
    for i, r in enumerate(rows, start=2):
        where = f"row {i} ({r.get('state')}/{r.get('line')})"
        if not schemas.STATE.match((r.get("state") or "").strip()):
            errors.append(f"{where}: state must be a two-letter code")
        if r.get("line", "").strip() not in schemas.LINES + ("*",):
            errors.append(f"{where}: line must be one of {list(schemas.LINES)} or *")
        for k in DAY_FIELDS:
            v = (r.get(k) or "").strip()
            if v and not v.isdigit():
                errors.append(f"{where}: {k} must be whole days or blank")
        try:
            schemas.check_evidence({"status": "VERIFIED", "url": (r.get("source_url") or "").strip() or None,
                                    "observed": (r.get("verified") or "").strip() or None}, as_of)
        except schemas.GateError as e:
            errors.append(f"{where}: {e.code} — every rule needs its source URL and the date it was verified")
    return errors


def _bad_rows(rows: list, as_of: str) -> set:
    bad = set()
    for i, r in enumerate(rows):
        if validate([r], as_of):
            bad.add(i)
    return bad


def rule_for(state: str, line: str, as_of: str, rows: list = None) -> dict:
    rows = load() if rows is None else rows
    bad = _bad_rows(rows, as_of)
    good = [r for i, r in enumerate(rows) if i not in bad and r["state"].strip() == state]
    row = next((r for r in good if r["line"].strip() == line), None) or next((r for r in good if r["line"].strip() == "*"), None)
    if row is None:
        return {"status": "needs_data", "value": None,
                "why": f"no verified {state} notice rule for {line}; compliance adds it to <home>/state_rules.csv with its source"}
    value = {k: int(row[k]) if (row.get(k) or "").strip() else None for k in DAY_FIELDS}
    return _status(row, as_of, value)


def _confirmed_by(state: str):
    """The person who confirmed this state's rules, if the rules are still exactly what they confirmed."""
    c = confirmations().get(state)
    if c and c.get("digest") == state_digest(state):
        return c["confirmed_by"].strip()
    return None


def _status(row: dict, as_of: str, value: dict) -> dict:
    age = scoring.age_days(row["verified"].strip(), as_of)
    confirmed = _confirmed_by(row["state"].strip())
    changed = row["state"].strip() in confirmations() and not confirmed
    status = "stale" if age > RULES_MAX_AGE_DAYS else ("ok" if confirmed else "unconfirmed")
    out = {"status": status, "value": value, "confirmed_by": confirmed or None,
           "evidence": {"status": "VERIFIED", "url": row["source_url"].strip(), "observed": row["verified"].strip()},
           "notes": (row.get("notes") or "").strip()}
    if status == "stale":
        out["why"] = f"verified {row['verified']}, more than {RULES_MAX_AGE_DAYS} days ago: re-verify"
    elif status == "unconfirmed":
        out["why"] = (f"{row['state']} rules changed since compliance confirmed them; re-confirm" if changed else
                      f"{row['state']} rule read from {row['source_url'].strip()} on {row['verified']}; compliance has not confirmed it")
    return out


def confirm(state: str, by: str = None, *, attest=None, today: str = None) -> dict:
    """Compliance confirms one state's notice and surplus lines rules, as they stand right now. Recorded in
    <home>/rule_confirmations.csv with a digest of the exact rows; if a row later changes (a plugin update, an
    agency edit) the confirmation stops applying and the packet says to re-confirm. Human-only: needs an
    interactive terminal and the state typed back (humans.attest)."""
    import datetime as _dt
    from . import humans
    state = (state or "").strip().upper()
    n = sum(1 for name in (NOTICE_FILE, SURPLUS_FILE) for r in load(None, name) if r["state"].strip() == state)
    if not n:
        raise ValueError(f"no rows for {state}: add them (with source_url and verified) before confirming")
    who = (attest or humans.attest)(f"Confirm {state} notice and surplus lines rules ({n} rows, each against its source_url)",
                                    state, by)
    rows = [r for r in _read(paths.home() / CONFIRMATIONS) if r["state"].strip() != state]
    rows.append({"state": state, "confirmed_by": who, "confirmed_on": today or _dt.date.today().isoformat(),
                 "digest": state_digest(state)})
    dest = paths.home() / CONFIRMATIONS
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, CONFIRM_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return {"state": state, "by": who, "rows": n}


def surplus_rule(state: str, as_of: str, rows: list = None) -> dict:
    rows = load(None, SURPLUS_FILE) if rows is None else rows
    for r in rows:
        if r["state"].strip() != state:
            continue
        try:
            schemas.check_evidence({"status": "VERIFIED", "url": (r.get("source_url") or "").strip() or None,
                                    "observed": (r.get("verified") or "").strip() or None}, as_of)
            n = int(r["declinations_required"])
        except (schemas.GateError, ValueError, KeyError):
            continue
        return _status(r, as_of, {"declinations_required": n, "export_list": (r.get("export_list") or "").strip()})
    return {"status": "needs_data", "value": None,
            "why": f"no verified {state} surplus lines diligent-effort rule; compliance adds it to <home>/surplus_lines_rules.csv"}
