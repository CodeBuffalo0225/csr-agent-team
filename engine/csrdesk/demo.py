"""bin/csr demo — every example ticket end to end, in an isolated runtime (data/runtime/demo).

Real network calls: NHTSA vPIC (VINs) and the Census geocoder (addresses). Everything else is the
sample account in data/examples — a fictional insured with fictional carriers, marked SAMPLE.
Fixed clock so the run is reproducible: as of 2026-10-12, tickets received 13:00Z, now 13:30Z.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import audit, orchestrator, packet, renewals, schemas

ROOT = Path(__file__).resolve().parents[2]
EX = ROOT / "data" / "examples"
AS_OF = "2026-10-12"
NOW = "2026-10-12T13:30:00Z"


def _ticket(name):
    return json.loads((EX / name).read_text(encoding="utf-8"))


def main(fetch=None, out_dir: Path = None) -> int:
    rt = ROOT / "data" / "runtime" / "demo"
    if rt.exists():
        shutil.rmtree(rt)          # the demo's own scratch runtime, recreated every run
    out = out_dir or rt / "packets"
    out.mkdir(parents=True, exist_ok=True)
    cfg = {"db": str(rt / "agency.db"), "audit_dir": str(rt / "audit"), "tickets_dir": str(rt / "tickets"),
           "kill_switch": str(rt / "STOP")}
    now = schemas.parse_timestamp(NOW)
    log, runs = [], {}

    def show(label, run):
        p = run["packet"]
        (out / f"{run['ticket_id']}.md").write_text(packet.to_markdown(p), encoding="utf-8")
        orchestrator.save(run, cfg)
        runs[run["ticket_id"]] = run
        log.append(f"{label:<44} {run['status']:<16} {len(p['action_items'])} action(s); trail {len(run['trail'])}")
        return run

    def start(name):
        return orchestrator.advance(orchestrator.open_ticket(_ticket(name)), as_of=AS_OF, now=now, config=cfg, fetch=fetch)

    q = show("QUOTE Q-1001 first pass", start("quote.json"))
    q = show("QUOTE Q-1001 after client reply", orchestrator.refresh(q, _ticket("quote_client_reply.json"), as_of=AS_OF, now=now, config=cfg, fetch=fetch))
    q = show("QUOTE Q-1001 AE revise -> CSR9", orchestrator.decide(q, {"ticket_id": "Q-1001", "decision": "REVISE", "target": "CSR9",
             "note": "Re-check WOS and primary/noncontributory against the blanket AI wording before we go to market.", "approvals": []},
             as_of=AS_OF, now=now, config=cfg, fetch=fetch))

    c = show("COI C-2001 (AI only)", start("coi.json"))
    c = orchestrator.decide(c, {"ticket_id": "C-2001", "decision": "APPROVE", "target": None,
                                "note": "Certificate matches the dec page. Release.", "approvals": ["issue_coi:C-2001/0"]},
                            as_of=AS_OF, now=now, config=cfg)
    show("COI C-2001 AE approve", c)
    dry = orchestrator.release(c, 0, live=False, config=cfg)
    live = orchestrator.release(c, 0, live=True, config=cfg)
    log.append(f"{'  release #0 drafts-only (live=false)':<44} {dry['decision']} {dry.get('code', '')}")
    log.append(f"{'  release #0 live with scoped approval':<44} {live['decision']} ({live['scope_needed']})")

    w = show("COI C-2002 (AI + WOS + PNC, 2M)", start("coi_wos.json"))
    for i in range(4):
        try:
            w = orchestrator.decide(w, {"ticket_id": "C-2002", "decision": "REVISE", "target": "CSR9",
                                        "note": f"Try again ({i + 1})", "approvals": []}, as_of=AS_OF, now=now, config=cfg, fetch=fetch)
        except ValueError as e:
            log.append(f"  revise {i + 1}: refused: {e}")
            break
        if w["status"] == "HALTED":
            break
    show("COI C-2002 after repeated revises", w)

    show("RENEWAL ECBA-26-55120 (auto)", start("renewal.json"))
    show("CLAIM L-3001 (injury)", start("claim.json"))
    show("SERVICE S-4001 (add vehicle)", start("service.json"))

    sweep = renewals.scan([_ticket("quote.json")["account"]], AS_OF)
    log.append(f"CSR6 book sweep: {len(sweep)} renewal(s) open: "
               + ", ".join(f"{r['policy_number']} {r['days_to_expiration']}d{' (notify CSR5)' if r['client_notice_due'] else ''}" for r in sweep))
    chains = {tid: audit.AuditLog(Path(cfg["audit_dir"]) / f"{tid}.jsonl").verify() for tid in runs}
    log.append("audit chains: " + ", ".join(f"{t} {'ok' if v['ok'] else 'BROKEN'} ({v['lines']})" for t, v in chains.items()))
    print("\n".join(log))
    print(f"\npackets: {out}")
    return 0 if all(v["ok"] for v in chains.values()) else 1
