"""CSR 8's bridge to the agency management system: Applied Epic first, HawkSoft and EZLynx alongside.

All three vendors gate their APIs (researched 2026-10-13; see docs/DEPLOYMENT.md for sources):
  applied_epic  REST platform via the Applied Dev Center; production credentials need Applied's approval and an
                agency-side SDK/API license. The older SOAP Epic SDK is legacy.
  hawksoft      certified Partner API program (vetted partners; competitors excluded); write-back is log notes.
  ezlynx        partner / enterprise access provisioned by EZLynx; applicants, contacts, policy headers, webhooks.

None publishes an open endpoint reference, so this module invents no endpoint. The agency's integration owner
puts the base URL, the paths its approved access grants, and the NAME of the environment variable holding the
token in data/agency/ams.json:

    {"system": "applied_epic", "base_url": "https://...", "token_env": "EPIC_API_TOKEN",
     "paths": {"client": "/...", "note": "/..."}}

Until that file, its token and one completed call exist, status is `needs_key` (spine S6). What works today:
`preview` (the exact payloads each system would receive) and `export` (neutral files for any AMS import).

Writes go through the policy gate: an AMS record or note is WRITE (needs the agency's live switch); nothing here
reaches a carrier. Payloads are built from CSR 8's masked record: no raw FEIN, license or DOB ever leaves.
"""
from __future__ import annotations

import csv
import json
import os
import urllib.request
from pathlib import Path

from . import policy

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "data" / "agency" / "ams.json"
SYSTEMS = {
    "applied_epic": {"label": "Applied Epic", "primary": True, "objects": ("client", "note"),
                     "note_name": "activity", "client_tool": "applied_epic_update_client", "note_tool": "applied_epic_create_activity",
                     "requires": ["Applied Dev Center app registration", "Applied approval for production credentials",
                                  "agency SDK/API license for Epic", "enterprise ID and Epic database name"]},
    "hawksoft": {"label": "HawkSoft", "primary": False, "objects": ("client", "note"),
                 "note_name": "log note", "client_tool": "hawksoft_update_client", "note_tool": "hawksoft_create_log_note",
                 "requires": ["HawkSoft certified Partner API access (vetted program)", "agency enables the integration",
                              "partner credentials and secret from HawkSoft"]},
    "ezlynx": {"label": "EZLynx", "primary": False, "objects": ("client", "note"),
               "note_name": "applicant note", "client_tool": "ezlynx_update_applicant", "note_tool": "ezlynx_create_note",
               "requires": ["EZLynx partner / enterprise API access provisioned by EZLynx", "OAuth client credentials"]},
}


def load_config(path=None) -> dict:
    p = Path(path) if path else CONFIG
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def connector_status(call_log: list = None, config_path=None) -> dict:
    """{status, system, missing, supported}. connected only after a completed call (spine S6)."""
    cfg = load_config(config_path)
    missing = []
    if cfg is None:
        missing.append("data/agency/ams.json (system, base_url, token_env, paths)")
    else:
        if cfg.get("system") not in SYSTEMS:
            missing.append(f"system must be one of {sorted(SYSTEMS)}")
        for k in ("base_url", "token_env"):
            if not cfg.get(k):
                missing.append(k)
        if not (cfg.get("paths") or {}).get("note"):
            missing.append("paths.note")
        if cfg.get("token_env") and not os.environ.get(cfg["token_env"]):
            missing.append(f"environment variable {cfg['token_env']} (the token itself never goes in a file)")
    state = policy.vendor_readiness(call_log or [], "ams")
    system = (cfg or {}).get("system")
    return {"status": "needs_key" if missing else state["status"], "value": None if missing else state["value"],
            "system": system, "missing": missing,
            "requires": SYSTEMS.get(system, SYSTEMS["applied_epic"])["requires"],
            "supported": {k: v["label"] + (" (primary)" if v["primary"] else "") for k, v in SYSTEMS.items()}}


def _client(system: str, acct: dict) -> dict:
    addr = acct.get("mailing_address") or {}
    contact = acct.get("contact") or {}
    base = {"name": acct["insured_name"], "dba": acct.get("dba"), "entity_type": acct.get("entity_type"),
            "address": addr, "contact_name": contact.get("name"), "email": contact.get("email"), "phone": contact.get("phone"),
            "naics": acct.get("naics"), "fein_last4": (acct.get("fein") or "")[-4:] or None}
    if system == "applied_epic":
        return {"accountName": base["name"], "dbaName": base["dba"], "businessType": base["entity_type"],
                "address": {"street1": addr.get("street"), "city": addr.get("city"), "state": addr.get("state"), "zip": addr.get("zip")},
                "primaryContact": {"name": base["contact_name"], "email": base["email"], "phone": base["phone"]},
                "naicsCode": base["naics"]}
    if system == "hawksoft":
        return {"client_name": base["name"], "dba": base["dba"], "address": addr, "contact": {"name": base["contact_name"],
                "email": base["email"], "phone": base["phone"]}}
    return {"applicant_type": "commercial", "business_name": base["name"], "dba": base["dba"], "address": addr,
            "contact": {"name": base["contact_name"], "email": base["email"], "phone": base["phone"]}, "naics": base["naics"]}


def _note(system: str, ticket: dict) -> dict:
    p = ticket.get("packet") or {}
    text = f"[CSR desk] {ticket['type']} {ticket['ticket_id']} — {ticket['status']}\n" + "\n".join(f"- {x}" for x in p.get("action_items", []))
    if system == "applied_epic":
        return {"activityCode": "CSRD", "description": f"CSR desk {ticket['type']} {ticket['ticket_id']}", "notes": text}
    if system == "hawksoft":
        return {"log_note": text, "channel": "CSR desk"}
    return {"note": text, "category": "CSR desk"}


def preview(run: dict, system: str = "applied_epic") -> dict:
    """The exact payloads `system` would receive for this ticket, from the masked record. Never sent."""
    if system not in SYSTEMS:
        raise ValueError(f"system must be one of {sorted(SYSTEMS)}")
    acct = run["ticket"]["account"]
    ticket = {"ticket_id": run["ticket_id"], "type": run["type"], "status": run["status"], "packet": run.get("packet") or {}}
    return {"system": SYSTEMS[system]["label"], "client": _client(system, acct), SYSTEMS[system]["note_name"]: _note(system, ticket)}


def _http(url: str, payload: dict, token: str) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return {"http_status": r.status}


def push(run: dict, *, live: bool, config_path=None, transport=_http) -> dict:
    """Write the ticket note (and client upsert when paths.client is set) to the configured AMS. Every call goes
    through policy.decide; a call the gate denies is not made. Returns per-call results."""
    st = connector_status(config_path=config_path)
    if st["missing"]:
        return {"status": "needs_key", "value": None, "missing": st["missing"]}
    cfg = load_config(config_path)
    sysdef, token = SYSTEMS[cfg["system"]], os.environ[cfg["token_env"]]
    body = preview(run, cfg["system"])
    calls = [("note", sysdef["note_tool"], body[sysdef["note_name"]])]
    if (cfg.get("paths") or {}).get("client"):
        calls.insert(0, ("client", sysdef["client_tool"], body["client"]))
    results = []
    for kind, tool, payload in calls:
        risk = policy.classify(tool)["risk"]
        verdict = policy.decide(tool, run["ticket_id"], risk, live=live, approvals=[], content_ok=True, harness=False)
        if verdict["decision"] != "allow":
            results.append({"call": kind, "tool": tool, "risk": risk, **verdict})
            continue
        try:
            out = transport(cfg["base_url"].rstrip("/") + cfg["paths"][kind], payload, token)
            results.append({"call": kind, "tool": tool, "risk": risk, "decision": "allow", **out})
        except Exception as e:
            results.append({"call": kind, "tool": tool, "risk": risk, "decision": "allow", "error": f"{type(e).__name__}: {e}"})
    return {"status": "pushed" if all("http_status" in r for r in results) else "partial_or_denied", "results": results}


def export(store, out_dir) -> dict:
    """CSR 8's store as neutral import files (accounts.csv, tickets.jsonl, events.jsonl). Already masked."""
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
