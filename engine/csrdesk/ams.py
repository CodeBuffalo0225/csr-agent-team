"""CSR 8's bridge to the agency management system (AMS).

No AMS credential exists in this repo, so a live connector is `needs_key` (spine S6), never assumed. What
works today, with no credential, is the export: CSR 8's store written out as neutral files any AMS import
or a CSR can load —

    accounts.csv      one row per insured: account key, name, masked FEIN, state, ZIP, lines in force, updated
    tickets.jsonl     one line per ticket: id, account key, type, status, the AE action items, updated
    events.jsonl      the append-only event log

Everything exported is already masked (the store never held raw PII). When the agency picks its AMS, the
live connector writes through tools that policy.classify rates WRITE (AMS records) or IRREVERSIBLE
(anything that reaches a carrier), and connector_status reads `connected` only after a completed call.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from . import policy

SUPPORTED = ("applied_epic", "hawksoft", "ezlynx", "qqcatalyst", "nowcerts")


def connector_status(call_log: list = None) -> dict:
    """{ams: needs_key | connected}. `call_log` entries are {"event": "call_ok", "vendor": "ams", ...}."""
    state = policy.vendor_readiness(call_log or [], "ams")
    return {"status": state["status"], "value": state["value"], "supported": list(SUPPORTED),
            "fix": None if state["status"] == "connected" else "choose the AMS, add its MCP connector, run one read call"}


def export(store, out_dir) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    db = store.db
    rows = db.execute("SELECT account_key, insured_name, record, updated FROM accounts ORDER BY insured_name").fetchall()
    with (out / "accounts.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["account_key", "insured_name", "fein_masked", "state", "zip", "lines_on_file", "updated"])
        for key, name, record, updated in rows:
            rec = json.loads(record)
            addr = rec.get("mailing_address") or {}
            lines = sorted({p["line"] for p in rec.get("prior_policies") or []})
            w.writerow([key, name, rec.get("fein"), addr.get("state"), addr.get("zip"), "|".join(lines), updated])
    tickets = db.execute("SELECT ticket_id, account_key, type, status, packet, updated FROM tickets ORDER BY updated").fetchall()
    with (out / "tickets.jsonl").open("w", encoding="utf-8") as f:
        for tid, key, kind, status, pkt, updated in tickets:
            p = json.loads(pkt)
            f.write(json.dumps({"ticket_id": tid, "account_key": key, "type": kind, "status": status,
                                "action_items": p.get("action_items", []), "updated": updated}) + "\n")
    events = store.events()
    with (out / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return {"accounts": len(rows), "tickets": len(tickets), "events": len(events), "out": str(out)}
