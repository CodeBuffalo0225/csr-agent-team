"""The AE packet: everything the AE needs to decide, on one page, in the order they should act.

Order of action items: escalations (act now) > halts (a human takes the ticket) > drafts awaiting
release > client information outstanding > the rest. The AE should never have to open ten CSR notes to
find the one thing that is on fire.
"""
from __future__ import annotations

from . import policy, thresholds


def all_drafts(run: dict) -> list:
    out = []
    for step, res in run["steps"].items():
        for d in res.get("drafts", []):
            out.append({"index": len(out), "step": step, **d,
                        "risk": policy.classify(d["release_tool"])["risk"]})
    return out


def build(run: dict, as_of: str) -> dict:
    steps = [{"step": s, "rule": run["rules"].get(s), "status": r["status"], "summary": r["summary"],
              "ae_note": r.get("ae_note")} for s, r in run["steps"].items()]
    escalations = [{"step": s, "why": r["escalate"]} for s, r in run["steps"].items() if r.get("escalate")]
    drafts = all_drafts(run)
    outstanding = [s["summary"] for s in steps if s["status"] == "needs_data"]
    blocked = [s["summary"] for s in steps if s["status"] == "blocked"]
    actions = []
    for e in escalations:
        actions.append(f"ESCALATION ({e['step']}): {e['why']}")
    if run.get("halt"):
        actions.append(f"HALTED ({run['halt']['code']}): {run['halt']['why']}. Work this ticket by hand.")
    for d in drafts:
        if d["gate"]["ok"]:
            actions.append(f"Release? draft #{d['index']} {d['kind']} to {d['to']} ({d['release_tool']}, {d['risk']})")
        else:
            actions.append(f"Draft #{d['index']} DISCARDED by grounding gate: ungrounded {d['gate']['ungrounded']}")
    for o in outstanding:
        actions.append(f"Outstanding: {o}")
    for step, r in run["steps"].items():
        rule = (r.get("findings") or {}).get("state_notice_rule")
        if rule and rule["status"] == "unconfirmed":
            actions.append(f"Confirm ({step}): {rule['why']}")
        elif rule and rule["status"] != "ok":
            actions.append(f"Data gap ({step}): {rule['why']}")
    for b in blocked:
        actions.append(f"Blocked: {b}")
    acct = run["ticket"]["account"]
    return {
        "ticket_id": run["ticket_id"], "type": run["type"], "insured": acct["insured_name"], "as_of": as_of,
        "status": run["status"], "halt": run.get("halt"), "escalations": escalations,
        "action_items": actions, "steps": steps, "drafts": drafts,
        "messages": [f"{m['sender']} -> {m['to']} [{m['kind']}] {m['subject']}" for m in run["messages"]],
        "findings": {s: r["findings"] for s, r in run["steps"].items()},
        "limits": {"hops": f"{len(run['trail'])}/{thresholds.MAX_HOPS}", "client_asks": f"{run['client_asks']}/{thresholds.MAX_CLIENT_ASKS}",
                   "ae_revisions": run["ae_revisions"], "budget_used": run["budget_used"], "budget": thresholds.TICKET_BUDGET},
    }


def to_markdown(p: dict) -> str:
    out = [f"# AE packet · {p['ticket_id']} · {p['type']} · {p['insured']}",
           f"status **{p['status']}** · as of {p['as_of']} · hops {p['limits']['hops']} · client asks {p['limits']['client_asks']}"
           f" · outside calls {p['limits']['budget_used'].get('calls', 0)}/{p['limits']['budget']['calls']}", "",
           "## Act on", *([f"{i + 1}. {a}" for i, a in enumerate(p["action_items"])] or ["Nothing outstanding: approve or reject."]), "",
           "## What each CSR did", "| step | status | summary | routed by |", "|---|---|---|---|"]
    for s in p["steps"]:
        out.append(f"| {s['step']} | {s['status']} | {s['summary']} | {s['rule'] or ''} |")
    if p["drafts"]:
        out += ["", "## Drafts (nothing has been sent)"]
        for d in p["drafts"]:
            out += [f"### #{d['index']} {d['kind']} → {d['to']} · release with `{d['release_tool']}` ({d['risk']})"
                    + ("" if d["gate"]["ok"] else " · **DISCARDED**"), f"**{d['subject']}**", "", "```", d["body"], "```"]
    if p["messages"]:
        out += ["", "## Bus", *[f"- {m}" for m in p["messages"]]]
    return "\n".join(out) + "\n"
