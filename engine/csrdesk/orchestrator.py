"""The run loop: open a ticket, advance it step by step under the stop-limiter, hand it to the AE, and
take the AE's decision back.

    open_ticket(raw)                  schema-gate, mask PII, hash the FEIN; the raw ticket is not kept
    advance(run, as_of, now, ...)     route -> limiter -> worker -> grounding gate on drafts -> audit, until
                                      AE:review (AWAITING_AE) or a halt (HALTED)
    decide(run, decision, ...)        the AE feedback loop: APPROVE | REVISE <CSR> | REJECT | ESCALATE
    refresh(run, raw)                 the client answered: new account data, re-run from intake
    release(run, index, ...)          may a human release draft #index? spine S8 against the AE's scoped approvals

Statuses: OPEN -> AWAITING_AE -> CLOSED_APPROVED | CLOSED_REJECTED | ESCALATED | (REVISE) OPEN;
any step may end in HALTED, which only a human closes.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import (acord, audit, claimsdesk, decpages, exclusions, grounding, intake, lossruns, market, packet,
               pii, policy, renewals, routing, schemas, service, store, thresholds)
from .desk import Ctx, result
from .limiter import check as limiter_check, check_revision

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "data" / "runtime"

WORKERS = {
    "CSR1:intake": intake.run,
    "CSR2:loss_runs": lossruns.run,
    "CSR3:applications": acord.applications,
    "CSR3:coi": acord.coi,
    "CSR4:market": market.run,
    "CSR5:verify": service.verify,
    "CSR5:info_request": service.info_request,
    "CSR5:renewal_notice": service.renewal_notice,
    "CSR5:service": service.service,
    "CSR6:renewal": renewals.run,
    "CSR7:fnol": claimsdesk.run,
    "CSR9:dec_review": decpages.run,
    "CSR10:exclusions": exclusions.run,
}


def open_ticket(raw: dict) -> dict:
    schemas.check_ticket(raw)
    return {"ticket_id": raw["ticket_id"], "type": raw["type"], "received": raw["received"],
            "ticket": pii.masked_ticket(raw), "fein_digest": store.fein_hash(raw["account"].get("fein")),
            "status": "OPEN", "steps": {}, "rules": {}, "trail": [], "skipped": [], "messages": [],
            "client_asks": 0, "ae_revisions": {}, "budget_used": {"calls": 0, "writes": 0}, "halt": None,
            "revision": None, "decisions": [], "approvals": [], "packet": None, "last_asks": None}


def _gate_drafts(res: dict, run: dict) -> None:
    """Spine S5 on every draft: one ungrounded figure discards the draft whole."""
    acct = run["ticket"]["account"]
    base = [acct["insured_name"], json.dumps(acct.get("mailing_address") or {}),
            json.dumps((run["ticket"]["request"] or {}).get("holder_address") or {})]
    for d in res["drafts"]:
        d["gate"] = grounding.ground(d["body"] + "\n" + d["subject"], d["allowed"] + base)


def _record(run: dict, as_of: str, config: dict) -> dict:
    s = store.Store(config.get("db"))
    try:
        snap = packet.build(run, as_of)
        w = s.write("CSR8", fein_digest=run["fein_digest"], masked_account=run["ticket"]["account"],
                    ticket_id=run["ticket_id"], ticket_type=run["type"], status=run["status"],
                    packet={k: snap[k] for k in ("action_items", "steps", "escalations", "halt")})
    finally:
        s.close()
    return result("CSR8", "record", "done", f"Record: account {w['action']}; ticket logged.", findings=w)


def _log(config: dict, run: dict) -> audit.AuditLog:
    return audit.AuditLog(Path(config.get("audit_dir") or RUNTIME / "audit") / f"{run['ticket_id']}.jsonl")


def _halt(run: dict, halt: dict, as_of: str, config: dict, log) -> dict:
    run["halt"], run["status"] = halt, "HALTED"
    if routing.RECORD not in run["steps"]:
        run["steps"][routing.RECORD] = _record(run, as_of, config)
        run["rules"][routing.RECORD] = "halt: CSR 8 records before the human takes over"
    run["packet"] = packet.build(run, as_of)
    log.append({"ticket_id": run["ticket_id"], "event": "halt", **halt})
    return run


def advance(run: dict, *, as_of: str, now, config: dict = None, fetch=None) -> dict:
    config = config or {}
    log = _log(config, run)
    budget = policy.Budget(thresholds.TICKET_BUDGET, run["budget_used"])
    skipped = set(run["skipped"])
    while run["status"] == "OPEN":
        rev = run["revision"]
        ctx = Ctx(ticket=run["ticket"], state=run["steps"], as_of=as_of, budget=budget, config=config, fetch=fetch,
                  note=rev["note"] if rev else None)
        nxt = routing.next_step(ctx, skipped)
        run["skipped"] = sorted(skipped)
        step = nxt["step"]
        if step == routing.REVIEW:
            run["status"] = "AWAITING_AE"
            run["packet"] = packet.build(run, as_of)
            log.append({"ticket_id": run["ticket_id"], "event": "awaiting_ae", "rule": nxt["rule"],
                        "actions": len(run["packet"]["action_items"])})
            break
        halt = limiter_check(run, step, now, Path(config["kill_switch"]) if config.get("kill_switch") else None)
        if halt:
            return _halt(run, halt, as_of, config, log)
        res = _record(run, as_of, config) if step == routing.RECORD else WORKERS[step](ctx)
        _gate_drafts(res, run)
        if rev and step.split(":")[0] == rev["target"]:
            res["ae_note"] = rev["note"]
            run["revision"] = None
        if step == "CSR5:info_request" and res["drafts"]:
            asks = res["findings"].get("asks")
            if asks == run.get("last_asks"):          # dedup fork: the client already has exactly this list
                res.update(drafts=[], status="done", summary="Client: same items already requested; waiting on the client.")
            else:
                run["client_asks"] += 1
                run["last_asks"] = asks
        run["steps"][step], run["rules"][step] = res, nxt["rule"]
        run["trail"].append(step)
        run["messages"].extend(res["messages"])
        run["budget_used"] = dict(budget.used)
        log.append({"ticket_id": run["ticket_id"], "event": "step", "step": step, "rule": nxt["rule"],
                    "status": res["status"], "summary": res["summary"], "saw": audit.digest(run["ticket"]),
                    "produced": audit.digest(res), "budget_used": run["budget_used"],
                    "drafts_discarded": sum(not d["gate"]["ok"] for d in res["drafts"])})
    return run


def decide(run: dict, decision: dict, *, as_of: str, now, config: dict = None, fetch=None,
           updated_raw: dict = None) -> dict:
    schemas.check_ae_decision(decision)
    if decision["ticket_id"] != run["ticket_id"]:
        raise ValueError(f"decision is for {decision['ticket_id']}, ticket is {run['ticket_id']}")
    if run["status"] not in ("AWAITING_AE", "HALTED"):
        raise ValueError(f"ticket is {run['status']}: nothing for the AE to decide")
    config = config or {}
    log = _log(config, run)
    run["decisions"].append(decision)
    run["approvals"] = sorted(set(run["approvals"]) | set(decision.get("approvals") or []))
    log.append({"ticket_id": run["ticket_id"], "event": "ae_decision", "decision": decision["decision"],
                "target": decision.get("target"), "approvals": decision.get("approvals") or []})
    kind = decision["decision"]
    if kind != "REVISE":
        run["status"] = {"APPROVE": "CLOSED_APPROVED", "REJECT": "CLOSED_REJECTED", "ESCALATE": "ESCALATED"}[kind]
        run["packet"] = packet.build(run, as_of)
        return run
    if run["status"] == "HALTED":
        raise ValueError("a halted ticket is worked by hand: APPROVE, REJECT or ESCALATE it")
    target = decision["target"]
    halt = check_revision(run, target)
    if halt:
        return _halt(run, halt, as_of, config, log)
    order = list(run["steps"])
    mine = [i for i, s in enumerate(order) if s.split(":")[0] == target]
    if not mine:
        raise ValueError(f"{target} has no work on this ticket; send the revision to a CSR that does")
    for s in order[mine[0]:]:
        run["steps"].pop(s)
    run["steps"].pop(routing.RECORD, None)
    if updated_raw is not None:
        _replace_ticket(run, updated_raw)
    run["skipped"], run["revision"], run["halt"] = [], {"target": target, "note": decision["note"]}, None
    run["ae_revisions"][target] = run["ae_revisions"].get(target, 0) + 1
    run["status"] = "OPEN"
    return advance(run, as_of=as_of, now=now, config=config, fetch=fetch)


def _replace_ticket(run: dict, raw: dict) -> None:
    schemas.check_ticket(raw)
    if raw["ticket_id"] != run["ticket_id"] or raw["type"] != run["type"]:
        raise ValueError("an update must keep the ticket id and type")
    run["ticket"], run["fein_digest"] = pii.masked_ticket(raw), store.fein_hash(raw["account"].get("fein"))


def refresh(run: dict, raw: dict, *, as_of: str, now, config: dict = None, fetch=None) -> dict:
    """The client sent the missing information: same ticket, new data, re-run from the first step.
    Counters (trail, client asks, revisions, budget) carry over, so refreshes cannot loop forever."""
    if run["status"] not in ("AWAITING_AE", "OPEN"):
        raise ValueError(f"ticket is {run['status']}; only an open ticket takes client updates")
    _replace_ticket(run, raw)
    run["steps"], run["skipped"], run["status"], run["revision"] = {}, [], "OPEN", None
    _log(config or {}, run).append({"ticket_id": run["ticket_id"], "event": "client_update", "saw": audit.digest(run["ticket"])})
    return advance(run, as_of=as_of, now=now, config=config, fetch=fetch)


def release(run: dict, index: int, *, live: bool = False, harness: bool = False, config: dict = None) -> dict:
    """Spine S8 on one draft. `allow` means a human may now release it with its tool; the engine itself
    still sends nothing. live=False (the default, drafts-only) always denies IRREVERSIBLE releases."""
    drafts = packet.all_drafts(run)
    if not 0 <= index < len(drafts):
        raise ValueError(f"no draft #{index}")
    d = drafts[index]
    risk = policy.classify(d["release_tool"])["risk"]
    target = f"{run['ticket_id']}/{index}"
    verdict = policy.decide(d["release_tool"], target, risk, live=live, approvals=run["approvals"],
                            content_ok=d["gate"]["ok"], harness=harness)
    _log(config or {}, run).append({"ticket_id": run["ticket_id"], "event": "release_check", "draft": index,
                                    "tool": d["release_tool"], "scope": f"{d['release_tool']}:{target}", **verdict})
    return {"draft": index, "tool": d["release_tool"], "scope_needed": f"{d['release_tool']}:{target}", "risk": risk, **verdict}


# ── Persistence ───────────────────────────────────────────────────────────────

def ticket_path(ticket_id: str, config: dict = None) -> Path:
    return Path((config or {}).get("tickets_dir") or RUNTIME / "tickets") / f"{ticket_id}.json"


def save(run: dict, config: dict = None) -> Path:
    p = ticket_path(run["ticket_id"], config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")
    return p


def load(ticket_id: str, config: dict = None) -> dict:
    return json.loads(ticket_path(ticket_id, config).read_text(encoding="utf-8"))
