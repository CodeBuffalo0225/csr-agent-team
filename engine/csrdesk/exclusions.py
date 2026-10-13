"""CSR 10 — exclusions. Which exclusions matter for *this* operation, project or loss, and are they
on the forms?

Two inputs, both deterministic: the operation (description of operations / NAICS keywords) and, on a
claim, the loss description. Each hit is a REVIEW PROMPT for the AE: "this class usually carries an
X exclusion; it is / is not listed on the dec page". It is never a coverage determination, and on a
claim it is never told to the client as a denial — only the carrier's adjuster decides coverage.

The table is a starting checklist an agency extends; the CSR 10 subagent reads the actual forms.
"""
from __future__ import annotations

import re

from .desk import Ctx, active_policy, message, policy_by_number, result, ticket_lines

# (operation pattern, exclusion name, phrases/forms that evidence it on the dec page, why it matters)
BY_OPERATION = (
    (r"landscap|lawn|tree|snow", "pesticide / herbicide application (pollution)",
     ("CG 21 49", "total pollution", "pesticide", "herbicide"),
     "chemical application is a pollution exposure; a total pollution exclusion removes it"),
    (r"landscap|lawn|snow", "snow and ice removal",
     ("snow", "ice removal"), "many landscaping forms exclude or sublimit snow plowing; slip-and-fall is the frequent claim"),
    (r"tree", "tree trimming / removal height limitation",
     ("height", "tree trimming", "tree removal"), "height and tree-felling limitations are common on landscaper programs"),
    (r"contract|construct|roof|plumb|electric|hvac|carpent|paint|landscap|masonry|concrete",
     "work performed by subcontractors", ("CG 22 94", "CG 22 95", "subcontractor"),
     "removes the subcontractor exception to the your-work exclusion"),
    (r"contract|construct|roof|plumb|electric|hvac|carpent|paint",
     "residential construction / tract housing", ("residential", "tract housing", "habitational"),
     "residential exclusions are carrier-specific and void coverage for home work"),
    (r"contract|construct|roof|masonry|concrete", "action over / injury to employees of contractors",
     ("action over", "injury to employee", "contractor's employee"), "removes coverage for third-party-over actions in NY/NJ"),
    (r"roof", "roofing height / hot work", ("height", "hot work", "torch"), "height limitations and torch-down exclusions"),
    (r"contract|construct|engineer|design|architect", "contractors professional liability",
     ("CG 22 79", "professional"), "design or specification work is excluded; may need a professional policy"),
    (r"contract|construct|landscap|plumb|electric|hvac", "designated operations / classification limitation",
     ("CG 21 44", "designated", "classification limitation"), "coverage only for the listed operations; any other work is uncovered"),
    (r"restaurant|bar|tavern|pub|nightclub|brew|liquor|catering", "assault and battery",
     ("assault", "battery"), "the most common bar/restaurant exclusion; often needs a separate sublimit"),
    (r"restaurant|bar|tavern|pub|nightclub|brew|liquor|catering", "liquor liability",
     ("liquor",), "the GL liquor exclusion applies when the insured is in the business of serving alcohol"),
    (r"daycare|child|school|camp|youth|church|nonprofit", "abuse and molestation",
     ("abuse", "molestation"), "often excluded outright; a separate coverage part is needed"),
    (r".*", "access or disclosure of confidential or personal data", ("CG 21 06", "data", "confidential"),
     "GL generally does not respond to a data breach; cyber is a separate line"),
    (r".*", "communicable disease", ("CG 21 32", "communicable disease"), "common on GL since 2020; matters for food, care and events"),
)

# (loss pattern, exclusion name, why). Claim-side prompts: matched on the loss description.
BY_LOSS = (
    (r"pollut|spill|chemical|pesticide|herbicide|fuel|leak", "pollution", "pollution exclusions are near-universal on GL and auto"),
    (r"flood|surface water|storm surge", "flood", "flood is excluded on standard property forms"),
    (r"mold|fung", "fungi / mold", "mold is usually excluded or sublimited"),
    (r"assault|fight|punch|battery", "assault and battery", "check for an assault and battery exclusion or sublimit"),
    (r"employee .*injur|worker .*injur|injured on the job", "employer's liability / workers comp", "an employee injury belongs on WC, not GL"),
    (r"faulty work|defective work|redo|re-do", "your work / your product", "repairing the insured's own faulty work is not covered; resulting damage may be"),
    (r"intentional|on purpose", "expected or intended injury", "intentional acts are excluded"),
    (r"wear and tear|deteriorat|gradual", "wear and tear / gradual deterioration", "property forms exclude gradual loss"),
)


def _listed(pol: dict) -> list:
    dec = (pol or {}).get("dec_page") or {}
    return [re.sub(r"\s+", " ", x).lower() for x in (dec.get("exclusions") or []) + (dec.get("forms") or [])
            + (dec.get("endorsements") or [])]


def run(ctx: Ctx) -> dict:
    t, a, tid = ctx.ticket, ctx.account, ctx.ticket["ticket_id"]
    ops = " ".join(filter(None, [a.get("description_of_operations"), t["request"].get("description")])).lower()
    if t["type"] == "RENEWAL":
        pol = policy_by_number(a, t["request"]["policy_number"])
        policies = {pol["line"]: pol} if pol else {}
    else:
        on = t["request"].get("date_of_loss") or ctx.as_of
        policies = {line: active_policy(a, line, on) for line in ticket_lines(t)}
    listed = [x for p in policies.values() for x in _listed(p)]
    has_forms = any((p or {}).get("dec_page") for p in policies.values())

    flags = []
    if t["type"] != "CLAIM":
        for pat, name, evidence, why in BY_OPERATION:
            if pat != r".*" and not re.search(pat, ops):
                continue
            found = next((x for x in listed for e in evidence if e.lower() in x), None)
            flags.append({"exclusion": name, "trigger": "operation",
                          "status": "listed" if found else ("not_listed" if has_forms else "no_forms_to_check"),
                          "matched": found, "why": why,
                          "severity": "HIGH" if found and pat != r".*" else ("MEDIUM" if pat != r".*" else "LOW")})
        if t["type"] == "COI" and t["request"].get("project_type") == "residential":
            res = next((f for f in flags if f["exclusion"].startswith("residential")), None)
            if res and res["status"] == "listed":
                res["severity"] = "CRITICAL"
                res["why"] = "certificate is for residential work and a residential exclusion is listed: do not issue without AE"
    else:
        desc = t["request"]["description"].lower()
        for pat, name, why in BY_LOSS:
            if re.search(pat, desc):
                found = next((x for x in listed if name.split(" ")[0] in x), None)
                flags.append({"exclusion": name, "trigger": "loss_description",
                              "status": "listed" if found else ("not_listed" if has_forms else "no_forms_to_check"),
                              "matched": found, "why": why, "severity": "HIGH"})

    msgs = []
    serious = [f for f in flags if f["severity"] in ("HIGH", "CRITICAL")]
    if serious:
        msgs.append(message("CSR10", "CSR4" if t["type"] in ("QUOTE", "RENEWAL") else "CSR3" if t["type"] == "COI" else "CSR7",
                            "NOTIFY", tid, "exclusions to address",
                            "; ".join(f"{f['exclusion']} ({f['status']})" for f in serious)))
    critical = [f for f in flags if f["severity"] == "CRITICAL"]
    summary = (f"Exclusions: {len(flags)} review prompt(s), {len(serious)} high"
               + (f", {len(critical)} critical" if critical else "") + ".")
    return result("CSR10", "exclusions", "escalate" if critical else "done", summary,
                  findings={"flags": flags, "forms_available": has_forms}, messages=msgs,
                  escalate=critical[0]["why"] if critical else None)
