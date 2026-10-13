"""The agency: who is deploying the desk, and is it ready to run.

    load_profile() / validate_profile(p)   <home>/agency/agency.json
    init(home)                             a fresh agency home from the shipped templates (never overwrites)
    readiness()                            the deployment checklist: READY, or exactly what is left and how

The profile is what makes the desk the agency's own: the producer block on every ACORD 25, the signature on
every client email, the states and lines whose rules must be loaded before renewals can be trusted, and which
AMS CSR 8 talks to.

agency.json
    {"agency_name": "...", "dba": null, "address": {"street","city","state","zip"}, "phone": "...", "email": "...",
     "website": null, "producer": {"name": "...", "npn": "<digits>", "license_state": "NJ"},
     "states_written": ["NJ","NY"], "lines_written": ["GL","AUTO",...],
     "ams_system": "applied_epic" | "hawksoft" | "ezlynx" | "none", "compliance_owner": "...", "service_hours": null}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import ams, markets, paths, replay, schemas, staterules

PROFILE_KEYS = ("agency_name", "dba", "address", "phone", "email", "website", "producer", "states_written",
                "lines_written", "ams_system", "compliance_owner", "service_hours")
PRODUCER_KEYS = ("name", "npn", "license_state")
AMS_CHOICES = tuple(ams.SYSTEMS) + ("none",)
REPLAY_TARGET = 20                      # closed tickets replayed before the desk is trusted unsupervised

TEMPLATE = {"agency_name": None, "dba": None, "address": {"street": None, "city": None, "state": None, "zip": None},
            "phone": None, "email": None, "website": None,
            "producer": {"name": None, "npn": None, "license_state": None},
            "states_written": [], "lines_written": [], "ams_system": "applied_epic",
            "compliance_owner": None, "service_hours": None}


def profile_path() -> Path:
    return paths.agency_dir() / "agency.json"


def load_profile(path=None):
    p = Path(path) if path else profile_path()
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def validate_profile(p) -> list:
    """Problems as strings; [] means the profile is complete."""
    if p is None:
        return ["agency profile not found: bin/csr init, then fill <home>/agency/agency.json"]
    errors = []
    unknown = [k for k in p if k not in PROFILE_KEYS]
    if unknown:
        errors.append(f"unknown key(s) {unknown}")
    for k in ("agency_name", "phone", "email", "compliance_owner"):
        if not isinstance(p.get(k), str) or not p[k].strip():
            errors.append(f"{k} is required")
    if isinstance(p.get("email"), str) and p["email"] and not schemas.EMAIL.match(p["email"]):
        errors.append("email is not an email address")
    try:
        schemas._address(p, "address", "$", nullable=False)
    except schemas.GateError as e:
        errors.append(f"address: {e.detail}")
    prod = p.get("producer") or {}
    if not isinstance(prod.get("name"), str) or not prod["name"].strip():
        errors.append("producer.name is required (the licensed producer on certificates)")
    if not re.fullmatch(r"\d{1,10}", str(prod.get("npn") or "")):
        errors.append("producer.npn must be the producer's National Producer Number (digits)")
    if not schemas.STATE.match(str(prod.get("license_state") or "")):
        errors.append("producer.license_state must be a two-letter state")
    states = p.get("states_written") or []
    if not states or any(not schemas.STATE.match(str(s)) for s in states):
        errors.append("states_written must list two-letter states the agency writes in")
    lines = p.get("lines_written") or []
    if not lines or any(l not in schemas.LINES for l in lines):
        errors.append(f"lines_written must list lines from {list(schemas.LINES)}")
    if p.get("ams_system") not in AMS_CHOICES:
        errors.append(f"ams_system must be one of {list(AMS_CHOICES)}")
    return errors


def init(home=None) -> dict:
    """Create an agency home. Existing files are never overwritten: re-running init is safe."""
    h = Path(home).expanduser() if home else paths.home()
    created, kept = [], []

    def put(rel: str, content: str):
        f = h / rel
        if f.exists():
            kept.append(rel)
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        created.append(rel)

    put("agency/agency.json", json.dumps(TEMPLATE, indent=2) + "\n")
    put("agency/markets.csv", ",".join(markets.MARKET_COLUMNS) + "\n")
    put("runtime/desk.json", json.dumps({"live": False}) + "\n")
    put("replay/README.md", "Closed tickets, each with an `expected` block written from what the CSRs actually did.\n"
                            "Format: engine/csrdesk/replay.py. These hold client PII: keep this folder private.\n")
    for d in ("runtime/tickets", "runtime/audit"):
        (h / d).mkdir(parents=True, exist_ok=True)
    return {"home": str(h), "created": created, "kept": kept,
            "next": [f"export {paths.ENV}={h}", "fill agency/agency.json", "list markets in agency/markets.csv",
                     "bin/csr readiness"]}


def _item(step: str, ok: bool, detail: str, fix: str = None, required: bool = True) -> dict:
    return {"step": step, "status": "done" if ok else ("todo" if required else "info"), "detail": detail,
            "fix": None if ok else fix, "required": required}


def readiness(as_of: str) -> dict:
    """Every deployment step, in the order an agency does them. READY when every required step is done."""
    items = []
    prof = load_profile()
    perrs = validate_profile(prof)
    items.append(_item("1 agency profile", not perrs, "complete" if not perrs else "; ".join(perrs),
                       "fill <home>/agency/agency.json"))
    mlist = markets.load_markets()
    merrs = markets.validate_markets(mlist) if mlist is not None else ["markets.csv not found"]
    appt = markets.appointed(mlist) if mlist is not None else {}
    ok = mlist is not None and not merrs and bool(appt)
    items.append(_item("2 markets (carriers + wholesalers/MGAs)", ok,
                       f"{len(appt)} appointed market(s)" if ok else "; ".join(merrs or ["no appointed markets"]),
                       "list appointed carriers and wholesalers/MGAs in <home>/agency/markets.csv; bin/csr setup validate"))
    from . import market as market_mod
    rows = market_mod.load_appetite() or []
    confirmed = {r["carrier"].strip() for r in rows if (r.get("confirmed_by") or "").strip()}
    pending = sorted(n for n, r in appt.items() if r["kind"].strip() == "carrier" and n not in confirmed)
    items.append(_item("3 confirmed appetite", bool(appt) and not pending,
                       f"{len(confirmed)} market(s) with confirmed appetite" + (f"; awaiting: {', '.join(pending)}" if pending else ""),
                       "bin/csr setup research -> the csr-04-market agent proposes -> a person runs bin/csr setup confirm"))
    states = (prof or {}).get("states_written") or []
    lines = (prof or {}).get("lines_written") or []
    gaps, unconf = [], []
    for st in states:
        for ln in lines:
            r = staterules.rule_for(st, ln, as_of)
            if r["status"] == "needs_data" or r["status"] == "stale":
                gaps.append(f"{st}/{ln}")
            elif r["status"] == "unconfirmed":
                unconf.append(f"{st}/{ln}")
        s = staterules.surplus_rule(st, as_of)
        if s["status"] in ("needs_data", "stale"):
            gaps.append(f"{st}/surplus lines")
        elif s["status"] == "unconfirmed":
            unconf.append(f"{st}/surplus lines")
    rules_ok = bool(states and lines) and not gaps and not unconf
    detail = ("all confirmed" if rules_ok else
              "; ".join(filter(None, [f"not loaded: {', '.join(sorted(set(gaps)))}" if gaps else "",
                                      f"researched, awaiting compliance: {', '.join(sorted(set(unconf)))}" if unconf else "",
                                      "" if states and lines else "states_written / lines_written not set"])))
    items.append(_item("4 state notice + surplus lines rules", rules_ok, detail,
                       "compliance checks each row against its source_url, then runs bin/csr rules confirm <STATE> --by <name> "
                       "in their own terminal; states not loaded need rows added with source + verified date"))
    system = (prof or {}).get("ams_system")
    if system == "none":
        items.append(_item("5 AMS", True, "no AMS: masked export (bin/csr ams export) is the bridge"))
    else:
        st = ams.connector_status()
        items.append(_item("5 AMS", st["status"] == "connected",
                           f"{ams.SYSTEMS.get(system, {}).get('label', system)}: {st['status']}"
                           + (f"; missing {', '.join(st['missing'])}" if st["missing"] else ""),
                           "obtain vendor API access (" + "; ".join(st["requires"]) + "), then <home>/agency/ams.json + token env var"))
    cases = sorted(paths.replay_dir().glob("*.json")) if paths.replay_dir().exists() else []
    rep = None
    if cases:
        try:
            rep = replay.run_dir(paths.replay_dir())
        except ValueError as e:
            rep = {"cases": len(cases), "agree": 0, "error": str(e)}
    ok = bool(rep) and rep.get("cases", 0) >= REPLAY_TARGET and rep.get("disagree", 1) == 0
    items.append(_item("6 replay closed tickets", ok,
                       f"{rep['agree']}/{rep['cases']} agree (target {REPLAY_TARGET})" if rep else f"0 of {REPLAY_TARGET} cases",
                       f"put {REPLAY_TARGET} closed tickets with expected blocks in <home>/replay/; bin/csr replay <home>/replay"))
    ks = (paths.runtime() / "STOP").exists()
    items.append(_item("kill switch", not ks, "clear" if not ks else "STOP file present: every ticket halts",
                       "remove <home>/runtime/STOP when the agency means to resume", required=True))
    from . import orchestrator
    live = orchestrator.agency_live()
    items.append(_item("live switch", live, "live: releases and AMS writes allowed with approvals" if live else
                       "drafts only (the agency turns this on last, in <home>/runtime/desk.json)", required=False))
    todo = [i for i in items if i["required"] and i["status"] != "done"]
    return {"home": str(paths.home()), "verdict": "READY" if not todo else "NOT READY", "todo": len(todo), "items": items}


def signature(profile) -> str:
    """The sign-off on every client draft. Empty when no profile is loaded (the packet flags that)."""
    if not profile or validate_profile(profile):
        return ""
    prod = profile["producer"]["name"]
    return f"\n\n{prod}\n{profile['agency_name']}\n{profile['phone']} · {profile['email']}"


def producer_block(profile):
    if not profile or validate_profile(profile):
        return None
    a = profile["address"]
    return {"agency": profile["agency_name"], "address": a, "contact": profile["producer"]["name"],
            "phone": profile["phone"], "email": profile["email"]}
