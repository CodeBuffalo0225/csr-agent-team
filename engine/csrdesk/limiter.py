"""The stop-limiter. Checked before every step and before every AE send-back.

A halt is not a failure and never a silent stop: the ticket goes to a human (the AE) with everything
done so far and the reason it stopped. What the limiter prevents is the expensive kind of failure that
does not crash — two agents asking each other for the same thing forever, a fourth email to a client
who has not answered three, an AE and a CSR revising the same COI all afternoon.

    check(run, step, now)        -> None | {"code", "why"}     before a step runs
    check_revision(run, target)  -> None | {"code", "why"}     before an AE REVISE is accepted

Codes, in the order checked (cheapest and most absolute first):
  KILL_SWITCH      data/runtime/STOP exists: every ticket halts
  WALL_CLOCK       ticket older than WALL_CLOCK_HOURS[type]: a human takes it
  MAX_HOPS         more than MAX_HOPS steps
  STEP_REVISIT     one step (agent:task) about to run more than MAX_VISITS_PER_STEP times
  PING_PONG        the last steps alternate between the same two agents MAX_PINGPONG+1 times
  MESSAGE_STORM    more than MAX_MESSAGES bus messages
  CLIENT_ASK_CAP   a client info request beyond MAX_CLIENT_ASKS (the AE calls instead)
  BUDGET           spine S9: the ticket's outside-call budget is spent (checked inside publicdata calls too)
  AE_REVISION_CAP_CSR / AE_REVISION_CAP_TOTAL   send-backs to one CSR / across the ticket
"""
from __future__ import annotations

from pathlib import Path

from . import schemas, thresholds

ROOT = Path(__file__).resolve().parents[2]
EXEMPT_FROM_REVISIT = ("CSR8", "AE")


def _agent(step: str) -> str:
    return step.split(":", 1)[0]


def check(run: dict, step: str, now, kill_switch: Path = None) -> dict:
    ks = kill_switch or (ROOT / thresholds.KILL_SWITCH)
    if ks.exists():
        return {"code": "KILL_SWITCH", "why": f"{ks} exists: all tickets halted by the agency"}
    opened = schemas.parse_timestamp(run["received"])
    age_h = (now - opened).total_seconds() / 3600
    cap_h = thresholds.WALL_CLOCK_HOURS[run["type"]]
    if age_h > cap_h:
        return {"code": "WALL_CLOCK", "why": f"{run['type']} ticket is {age_h:.1f}h old (limit {cap_h}h)"}
    trail = run["trail"]
    if len(trail) >= thresholds.MAX_HOPS:
        return {"code": "MAX_HOPS", "why": f"{len(trail)} steps taken (limit {thresholds.MAX_HOPS})"}
    agent = _agent(step)
    if agent not in EXEMPT_FROM_REVISIT:
        visits = trail.count(step)
        if visits >= thresholds.MAX_VISITS_PER_STEP:
            return {"code": "STEP_REVISIT", "why": f"{step} has run {visits} times (limit {thresholds.MAX_VISITS_PER_STEP})"}
    tail = [_agent(s) for s in trail[-(2 * thresholds.MAX_PINGPONG + 1):]] + [agent]
    if len(tail) == 2 * (thresholds.MAX_PINGPONG + 1) and len(set(tail)) == 2 and all(tail[i] != tail[i + 1] for i in range(len(tail) - 1)):
        return {"code": "PING_PONG", "why": f"{tail[0]} and {tail[1]} have handed back and forth {thresholds.MAX_PINGPONG + 1} times"}
    if len(run["messages"]) > thresholds.MAX_MESSAGES:
        return {"code": "MESSAGE_STORM", "why": f"{len(run['messages'])} bus messages (limit {thresholds.MAX_MESSAGES})"}
    if step == "CSR5:info_request" and run["client_asks"] >= thresholds.MAX_CLIENT_ASKS:
        return {"code": "CLIENT_ASK_CAP", "why": f"client already asked {run['client_asks']} times; the AE calls instead"}
    used, lim = run["budget_used"], thresholds.TICKET_BUDGET
    if any(used.get(k, 0) >= lim[k] for k in lim):
        return {"code": "BUDGET", "why": f"outside-call budget spent ({used} of {lim})"}
    return None


def check_revision(run: dict, target: str) -> dict:
    per = run["ae_revisions"].get(target, 0)
    if per >= thresholds.MAX_AE_REVISIONS_PER_CSR:
        return {"code": "AE_REVISION_CAP_CSR", "why": f"{target} already revised {per} times: take it offline with the CSR"}
    total = sum(run["ae_revisions"].values())
    if total >= thresholds.MAX_AE_REVISIONS_TOTAL:
        return {"code": "AE_REVISION_CAP_TOTAL", "why": f"{total} revisions on this ticket: it needs a conversation, not another loop"}
    return None
