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
regulation and statute text on 2026-10-13 (sources in each row). They carry an empty `confirmed_by` until the
agency's compliance owner checks them; until then every packet labels them unconfirmed.

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


def load(path=None, name: str = NOTICE_FILE) -> list:
    p = Path(path).expanduser() if path else paths.rules_file(name)
    if not p.is_absolute():
        p = paths.home() / p
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if (r.get("state") or "").strip()]


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
                "why": f"no verified {state} notice rule for {line} in data/state_rules.csv; compliance loads it"}
    value = {k: int(row[k]) if (row.get(k) or "").strip() else None for k in DAY_FIELDS}
    return _status(row, as_of, value)


def _status(row: dict, as_of: str, value: dict) -> dict:
    age = scoring.age_days(row["verified"].strip(), as_of)
    confirmed = (row.get("confirmed_by") or "").strip()
    status = "stale" if age > RULES_MAX_AGE_DAYS else ("ok" if confirmed else "unconfirmed")
    out = {"status": status, "value": value, "confirmed_by": confirmed or None,
           "evidence": {"status": "VERIFIED", "url": row["source_url"].strip(), "observed": row["verified"].strip()},
           "notes": (row.get("notes") or "").strip()}
    if status == "stale":
        out["why"] = f"verified {row['verified']}, more than {RULES_MAX_AGE_DAYS} days ago: re-verify"
    elif status == "unconfirmed":
        out["why"] = f"{row['state']} rule read from {row['source_url'].strip()} on {row['verified']}; compliance has not confirmed it"
    return out


def confirm(state: str, by: str) -> dict:
    """Compliance confirms one state's rows: they are written, with confirmed_by, into the agency's own copy at
    <home>/state_rules.csv and <home>/surplus_lines_rules.csv (the shipped files are never edited). A human act:
    the plugin hook refuses this command from an agent; compliance runs it in their own terminal."""
    if not (by or "").strip():
        raise ValueError("rules confirm needs --by <the compliance owner confirming>")
    out = {}
    for name in (NOTICE_FILE, SURPLUS_FILE):
        rows = load(None, name)
        mine = [r for r in rows if r["state"].strip() == state]
        for r in mine:
            r["confirmed_by"] = by.strip()
        if rows:
            dest = paths.home() / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
        out[name] = len(mine)
    if not any(out.values()):
        raise ValueError(f"no rows for {state}: add them (with source_url and verified) before confirming")
    return {"state": state, "by": by.strip(), "confirmed_rows": out}


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
            "why": f"no verified {state} surplus lines diligent-effort rule in data/surplus_lines_rules.csv"}
