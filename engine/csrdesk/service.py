"""CSR 5 — the client-facing desk: names, licenses, vehicles, prior policies, and every message the
client receives.

Tasks
  verify          decode every VIN (NHTSA, after the local check digit) and geocode the mailing address
                  and locations (Census). Real calls, budgeted, with evidence. A VIN that fails the check
                  digit, a GVW band that straddles a size class, or an address with no match becomes a
                  client question, not a guess.
  info_request    one client email collecting everything every other CSR asked for. Counts against
                  MAX_CLIENT_ASKS; the limiter stops a fourth ask and the AE calls instead.
  renewal_notice  CSR 6 notified CSR 5 that a policy is within RENEWAL_NOTICE_DAYS: the client notice and
                  updated-exposure request.
  service         a SERVICE ticket, by change type (if/else below).

Every client-facing draft carries its `allowed` figures for the grounding gate. Nothing is sent.
"""
from __future__ import annotations

import time

from . import publicdata, scoring, thresholds
from .desk import Ctx, active_policy, draft, message, policy_by_number, result

COVERAGE_CHANGES = ("ADD_DRIVER", "REMOVE_DRIVER", "ADD_VEHICLE", "REMOVE_VEHICLE", "ADD_LOCATION", "LIMIT_CHANGE")


def verify(ctx: Ctx) -> dict:
    a, tid = ctx.account, ctx.ticket["ticket_id"]
    fetch = ctx.fetch or publicdata._http_json
    sleep = ctx.config.get("sleep", time.sleep)          # retry backoff; tests inject a no-op
    service_change = ctx.ticket["type"] == "SERVICE"
    # a service change verifies only what it adds; a quote verifies the whole schedule and every address
    vehicles = [ctx.request["vehicle"]] if service_change and ctx.request.get("vehicle") else list(a.get("vehicles") or [])
    decoded = publicdata.decode_vins(vehicles, ctx.budget, ctx.as_of, fetch=fetch, sleep=sleep)["vehicles"] if vehicles else []
    places = []
    addresses = [] if service_change else (
        [("mailing", a.get("mailing_address"))] + [(f"location {i + 1}", l) for i, l in enumerate(a.get("locations") or [])])
    for label, addr in addresses:
        if addr and not any(p["input_addr"] == addr for p in places):
            g = publicdata.geocode(addr, ctx.budget, ctx.as_of, fetch=fetch, sleep=sleep)
            places.append({"label": label, "input_addr": addr, **g})
    asks = []
    for v in decoded:
        if v["status"] == "confirm_with_client":
            asks.append(f"Please re-check VIN {v['vin']}: {v['reason']}.")
        elif v["value"] and v["value"]["size_class"] is None and v["value"]["basis"] == "band_straddles_boundary":
            asks.append(f"For the {v['value']['year']} {v['value']['make']} {v['value']['model']} (VIN ending {v['vin'][-6:]}), "
                        f"what GVW is printed on the driver's door-jamb sticker?")
    for p in places:
        if p["status"] == "no_match":
            asks.append(f"We could not verify the {p['label']} address ({p['input']}); please confirm it.")
    unavailable = [v["vin"] for v in decoded if v["status"] == "unavailable"] + [p["label"] for p in places if p["status"] == "unavailable"]
    msgs = []
    if decoded:
        classes = sorted({v["value"]["size_class"] for v in decoded if v["value"] and v["value"]["size_class"]})
        msgs.append(message("CSR5", "CSR3", "NOTIFY", tid, "vehicle schedule decoded",
                            f"{sum(1 for v in decoded if v['value'])} of {len(decoded)} VIN(s) decoded; size classes: {', '.join(classes) or 'unresolved'}."))
    status = "needs_data" if asks else ("blocked" if unavailable else "done")
    summary = (f"Verify: {sum(1 for v in decoded if v['value'])}/{len(decoded)} VIN(s) decoded, "
               f"{sum(p['status'] == 'ok' for p in places)}/{len(places)} address(es) matched"
               + (f"; {len(asks)} to confirm with client" if asks else "") + (f"; unavailable: {', '.join(unavailable)}" if unavailable else "") + ".")
    return result("CSR5", "verify", status, summary,
                  findings={"vehicles": decoded, "addresses": places, "client_asks": asks, "unavailable": unavailable},
                  messages=msgs)


def collect_asks(ctx: Ctx) -> list:
    """Everything any CSR needs from the client, de-duplicated, in desk order."""
    asks = []
    for m in ctx.out("CSR1:intake").get("missing", []):
        if m["ask"]:
            asks.append(m["ask"])
    asks += ctx.out("CSR5:verify").get("client_asks", [])
    per = ctx.out("CSR2:loss_runs").get("per_line") or {}
    unknown = [l for l, v in per.items() if v["status"] == "prior_unknown"]
    if unknown:
        asks.append(f"Who insured the business for {', '.join(unknown)} over the last five years (carrier and policy number)?")
    if any(v["status"] in ("stale", "missing", "short_history") for v in per.values()):
        asks.append("Please sign the attached letter of authorization so your prior carriers can release loss runs to us.")
    if ctx.out("CSR9:dec_review").get("missing_dec_pages"):
        asks.append("Please send the declarations page for: " + "; ".join(ctx.out("CSR9:dec_review")["missing_dec_pages"]) + ".")
    seen, out = set(), []
    for x in asks:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def info_request(ctx: Ctx) -> dict:
    a = ctx.account
    asks = collect_asks(ctx)
    if not asks:
        return result("CSR5", "info_request", "done", "Client: nothing outstanding to request.")
    contact = (a.get("contact") or {})
    to = contact.get("email") or contact.get("phone") or "client (no contact on file)"
    name = (contact.get("name") or "there").split(" ")[0]
    purpose = {"QUOTE": "your quote", "RENEWAL": "your renewal", "SERVICE": "your policy change",
               "POLICY_CHECK": "your new policy", "COI": "your certificate", "CLAIM": "your claim"}[ctx.ticket["type"]]
    body = (f"Hi {name},\n\nTo finish {purpose} we need a few items:\n" + "\n".join(f"- {x}" for x in asks)
            + "\n\nReply here or call us with any questions.\n")
    allowed = [a["insured_name"]] + asks
    return result("CSR5", "info_request", "needs_data", f"Client: {len(asks)} item(s) requested.",
                  findings={"asks": asks, "to": to},
                  drafts=[draft("email", to, f"Information needed for {purpose} - {a['insured_name']}", body, "send_email", allowed)])


def renewal_notice(ctx: Ctx) -> dict:
    a, r = ctx.account, ctx.request
    p = policy_by_number(a, r["policy_number"])
    left = scoring.days_until(p["expiration"], ctx.as_of)
    contact = a.get("contact") or {}
    to = contact.get("email") or contact.get("phone") or "client (no contact on file)"
    body = (f"Your {p['line']} policy {p['policy_number']} with {p['carrier']} expires on {p['expiration']}, "
            f"{left} days from today. To prepare your renewal, please confirm or update: annual revenue, payroll, "
            f"employee count, locations, vehicles and drivers, and any claims or changes in operations this year.")
    return result("CSR5", "renewal_notice", "done", f"Renewal notice drafted: {p['policy_number']} expires in {left} days.",
                  findings={"policy_number": p["policy_number"], "days_to_expiration": left},
                  drafts=[draft("email", to, f"Your upcoming renewal - {a['insured_name']}", body, "send_email",
                                [p["policy_number"], p["expiration"], f"{left} days"])])


def service(ctx: Ctx) -> dict:
    """if/else on change type:
        coverage change   -> endorsement request draft; IRREVERSIBLE at the carrier, AE approves
          ADD_DRIVER      -> also needs license, DOB and signed MVR consent before anything is submitted
          ADD_VEHICLE     -> the VIN goes through verify first
        POLICY_QUESTION   -> never answered here: CSR 9 + CSR 10 review, the AE answers (E&O)
        NAME_CHANGE       -> a new legal entity is a new insured: AE
        ADDRESS / BILLING -> CSR 5 drafts the confirmation
    """
    r, a, tid = ctx.request, ctx.account, ctx.ticket["ticket_id"]
    kind = r["change_type"]
    if kind == "POLICY_QUESTION":
        return result("CSR5", "service", "done", "Service: coverage question routed to CSR 9/10 and the AE; CSR 5 does not interpret coverage.",
                      findings={"route": "coverage_review", "change_type": kind},
                      messages=[message("CSR5", "CSR9", "REQUEST", tid, "coverage question", r["details"])])
    if kind == "NAME_CHANGE":
        return result("CSR5", "service", "escalate", "Service: name change; if the legal entity changed this is a new insured.",
                      findings={"route": "ae", "change_type": kind},
                      escalate="name change: confirm whether the FEIN/legal entity changed (new insured, new applications)")
    if kind in ("ADDRESS_CHANGE", "BILLING"):
        to = (a.get("contact") or {}).get("email") or "client"
        return result("CSR5", "service", "done", f"Service: {kind.lower().replace('_', ' ')} confirmation drafted.",
                      findings={"route": "csr5", "change_type": kind},
                      drafts=[draft("email", to, f"Confirming your request - {a['insured_name']}",
                                    f"We received your request: {r['details']} We will confirm once the carrier processes it.",
                                    "send_email", [r["details"], a["insured_name"]])])
    missing = []
    if kind == "ADD_DRIVER":
        drv = r.get("driver") or {}
        for f, label in (("dl_number", "license number"), ("dl_state", "license state"), ("dob", "date of birth")):
            if not drv.get(f):
                missing.append(f"the new driver's {label}")
        if drv.get("mvr_consent") is not True:
            missing.append("the driver's signed consent to run a motor vehicle report")
    if kind == "ADD_VEHICLE":
        missing += ctx.out("CSR5:verify").get("client_asks") or []      # a straddling GVW or a bad VIN blocks rating
        veh = ctx.out("CSR5:verify").get("vehicles") or []
        new = next((v for v in veh if r.get("vehicle") and v["vin"] == r["vehicle"]["vin"].upper()), None)
        if not r.get("vehicle"):
            missing.append("the VIN of the vehicle to add")
        elif new is None or new["status"] in ("confirm_with_client", "unavailable"):
            missing.append(f"a valid VIN ({(new or {}).get('reason') or 'not decoded'})")
    if missing:
        to = (a.get("contact") or {}).get("email") or "client"
        body = "To process your change we need:\n" + "\n".join(f"- {m}" for m in missing)
        return result("CSR5", "service", "needs_data", f"Service: {kind} waiting on {len(missing)} item(s).",
                      findings={"route": "client", "change_type": kind, "missing": missing},
                      drafts=[draft("email", to, f"Information needed for your policy change - {a['insured_name']}",
                                    body, "send_email", missing)])
    line = r.get("line") or ("AUTO" if "DRIVER" in kind or "VEHICLE" in kind else None)
    if line is None:
        return result("CSR5", "service", "needs_data", f"Service: {kind} does not say which policy to change.",
                      findings={"route": "client", "change_type": kind,
                                "missing": ["which policy (line of coverage) this change applies to"]})
    target = active_policy(a, line, ctx.as_of)
    if target is None:
        return result("CSR5", "service", "escalate", f"Service: no {line} policy in force to endorse.",
                      findings={"route": "ae", "change_type": kind}, escalate=f"{kind} requested with no {line} policy in force")
    body = (f"Endorsement request, {target['carrier']} policy {target['policy_number']}: {kind.replace('_', ' ').lower()}. "
            f"Details: {r['details']}")
    return result("CSR5", "service", "done", f"Service: endorsement request drafted on {target['policy_number']}; AE approves submission.",
                  findings={"route": "endorsement", "change_type": kind, "policy_number": target["policy_number"]},
                  drafts=[draft("endorsement_request", target["carrier"], f"Endorsement request - {target['policy_number']}",
                                body, "submit_endorsement", [target["policy_number"], r["details"]])])
