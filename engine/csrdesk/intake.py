"""CSR 1 — quote intake. Is this account complete enough to quote each requested line, and which
ACORD applications does it feed?

Completeness is decided here, in code, from REQUIRED_BY_LINE — not by a model reading the email.
Each gap becomes a client-facing question for CSR 5 and is marked `blocking` (the line cannot go to
market without it) or not. The router reads `blocking_lines`: a blocked line skips CSR 4.
"""
from __future__ import annotations

from .desk import Ctx, message, result

# ACORD applications each line feeds. CSR 3 builds their field sets from the same account record.
ACORD_FORMS = {
    "COMMON": ["ACORD 125 Commercial Insurance Application"],
    "GL": ["ACORD 126 Commercial General Liability Section"],
    "AUTO": ["ACORD 127 Business Auto Section", "ACORD 129 Vehicle Schedule"],
    "WC": ["ACORD 130 Workers Compensation Application"],
    "PROPERTY": ["ACORD 140 Property Section"],
    "UMBRELLA": ["ACORD 131 Umbrella / Excess Section"],
    "BOP": ["carrier BOP application (or ACORD 126 + ACORD 140, as the carrier accepts)"],
    "PROFESSIONAL": ["carrier supplemental application"],
    "CYBER": ["carrier supplemental application"],
    "EPLI": ["carrier supplemental application"],
    "LIQUOR": ["carrier liquor liability supplemental"],
    "INLAND_MARINE": ["carrier equipment schedule / supplemental"],
}

# (field, question for the client, blocking). A field path ending in [] is checked on every item.
COMMON = (
    ("entity_type", "What is the legal entity type (LLC, corporation, partnership, sole proprietor)?", True),
    ("mailing_address", "What is the business mailing address?", True),
    ("description_of_operations", "Please describe exactly what the business does day to day.", True),
    ("contact", "Who is our contact for this quote, with an email or phone?", True),
    ("years_in_business", "How many years has the business been operating?", True),
    ("naics", "", False),           # CSR 1 can classify from the description; not a client question
)
REQUIRED_BY_LINE = {
    "GL": (("annual_revenue", "What were gross annual sales for the last 12 months?", True),
           ("locations", "Please list every business location address.", True)),
    "BOP": (("annual_revenue", "What were gross annual sales for the last 12 months?", True),
            ("locations", "Please list every business location address.", True),
            ("property_schedule", "For each location: building value, contents value, construction type, year built.", True)),
    "AUTO": (("drivers", "Please send every driver's name, date of birth, license number and license state.", True),
             ("drivers[].dl_number", "We are missing a driver's license number.", True),
             ("drivers[].dl_state", "We are missing a driver's license state.", True),
             ("drivers[].dob", "We are missing a driver's date of birth.", True),
             ("vehicles", "Please send the VIN for every vehicle.", True),
             ("vehicles[].garaging_zip", "Where is each vehicle garaged overnight (ZIP code)?", True),
             ("vehicles[].use", "How is each vehicle used (service calls, retail delivery, commercial hauling)?", False)),
    "WC": (("payroll", "What is estimated annual payroll, by job type if you have it?", True),
           ("employees", "How many employees, full and part time?", True),
           ("fein", "What is the business FEIN?", True)),
    "PROPERTY": (("locations", "Please list every business location address.", True),
                 ("property_schedule", "For each location: building value, contents value, construction type, year built.", True)),
    "UMBRELLA": (),
    "PROFESSIONAL": (("annual_revenue", "What were gross annual sales for the last 12 months?", True),),
    "CYBER": (("annual_revenue", "What were gross annual sales for the last 12 months?", True),),
    "EPLI": (("employees", "How many employees, full and part time?", True),),
    "LIQUOR": (("annual_revenue", "What were gross annual sales, and what share is alcohol?", True),),
    "INLAND_MARINE": (("annual_revenue", "What were gross annual sales for the last 12 months?", True),),
}
UNDERLYING_FOR_UMBRELLA = ("GL", "AUTO")


def _empty(v) -> bool:
    return v is None or v == [] or v == ""


def _gaps(account: dict, field: str) -> list:
    """Paths of `field` that are missing. 'drivers[].dl_number' -> ['drivers[1].dl_number', ...]."""
    if "[]." not in field:
        return [field] if _empty(account.get(field)) else []
    coll, sub = field.split("[].")
    return [f"{coll}[{i}].{sub}" for i, item in enumerate(account.get(coll) or []) if _empty(item.get(sub))]


def run(ctx: Ctx) -> dict:
    a, lines = ctx.account, list(ctx.request["lines"])
    missing, seen = [], set()

    def add(line, spec):
        field, ask, blocking = spec
        for path in _gaps(a, field):
            if path in seen:
                continue
            seen.add(path)
            missing.append({"field": path, "line": line, "ask": ask, "blocking": blocking})

    for spec in COMMON:
        add("COMMON", spec)
    for line in lines:
        for spec in REQUIRED_BY_LINE.get(line, ()):
            add(line, spec)

    notes = []
    if "UMBRELLA" in lines:
        have = set(lines) | {p["line"] for p in a.get("prior_policies") or []}
        for under in UNDERLYING_FOR_UMBRELLA:
            if under not in have:
                missing.append({"field": f"underlying.{under}", "line": "UMBRELLA", "blocking": True,
                                "ask": f"An umbrella needs underlying {under}: who writes it today, and can we see the dec page?"})
    new_venture = (a.get("years_in_business") == 0) and not a.get("prior_policies")
    if new_venture:
        notes.append("new venture: no prior coverage, no loss runs expected; carriers will want owner experience")
    if a.get("naics") is None:
        notes.append("NAICS not set: CSR 1 subagent classifies from the description; CSR 4 cannot filter appetite until it is")

    blocking_lines = sorted({m["line"] for m in missing if m["blocking"]})
    if "COMMON" in blocking_lines:
        blocking_lines = sorted(set(blocking_lines) | set(lines))
    forms = list(ACORD_FORMS["COMMON"])
    for line in lines:
        forms += [f for f in ACORD_FORMS.get(line, []) if f not in forms]

    client_asks = [m for m in missing if m["ask"]]
    msgs = []
    if client_asks:
        msgs.append(message("CSR1", "CSR5", "REQUEST", ctx.ticket["ticket_id"], "client information needed",
                            f"{len(client_asks)} item(s) missing for {', '.join(lines)}; please request from the client."))
    msgs.append(message("CSR1", "CSR2", "NOTIFY", ctx.ticket["ticket_id"], "new quote opened",
                        f"Lines {', '.join(lines)}; effective {ctx.request['effective_date']}. Loss runs needed."))
    ready = [l for l in lines if l not in blocking_lines]
    summary = (f"Intake: {len(ready)} of {len(lines)} line(s) ready to market"
               + (f"; blocked: {', '.join(blocking_lines)}" if blocking_lines else "")
               + f"; {len(client_asks)} client question(s).")
    return result("CSR1", "intake", "done" if not missing else "needs_data", summary,
                  findings={"lines": lines, "ready_lines": ready, "blocking_lines": blocking_lines,
                            "missing": missing, "acord_forms": forms, "new_venture": new_venture, "notes": notes},
                  messages=msgs)
