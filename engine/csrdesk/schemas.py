"""Schema gate: every record passes here before an agent or the router uses it.

Strict by construction (spine S1): an undeclared key is rejected, not stripped. Self-grading is
rejected first (spine S2): an agent supplies judgment and prose, the engine supplies arithmetic, so a
payload carrying a total, tier, score, rank, priority or age is the model doing the engine's job.
Evidence is spine S3: VERIFIED means a URL and a date.

    check_evidence(ev, as_of)        {status, url, observed}                                      S3
    check_judgment(payload)          CSR 4's market-fit judgment {carrier, scores{5 dims}, rationale}  S1 S2
    check_ticket(ticket)             a unit of work: QUOTE | RENEWAL | COI | SERVICE | CLAIM
    check_ae_decision(decision)      the AE's feedback-loop verdict
    check_message(msg)               one message on the inter-CSR bus

Client data arrives partial; that is normal. So account fields are optional here and typed when
present. Whether the account is *complete enough for a line* is CSR 1's judgment in intake.py, made
in code from REQUIRED_BY_LINE, not here.

Codes: TYPE SELF_GRADED UNKNOWN_KEY MISSING BAD_ENUM BAD_STATUS MISSING_URL MISSING_DATE FUTURE_DATED
BAD_DATE BAD_URL BAD_EMAIL OUT_OF_RANGE TOO_LONG.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, NoReturn

EVIDENCE_STATUSES = ("VERIFIED", "INFERENCE", "HYPOTHESIS")
# CSR 4's half of market scoring. scoring.py owns the weights and the arithmetic.
JUDGMENT_DIMS = ("appetite_fit", "coverage_breadth", "loss_history_fit", "program_fit", "service_fit")
JUDGMENT_KEYS = ("carrier", "scores", "rationale")
# Keys only the engine may produce. A model payload carrying one is self-grading (S2).
COMPUTED_KEYS = ("total", "tier", "score", "rank", "priority", "age_days")

AGENTS = ("CSR1", "CSR2", "CSR3", "CSR4", "CSR5", "CSR6", "CSR7", "CSR8", "CSR9", "CSR10", "CSR11", "AE")
TICKET_TYPES = ("QUOTE", "RENEWAL", "COI", "SERVICE", "CLAIM", "POLICY_CHECK")
LINES = ("GL", "AUTO", "WC", "PROPERTY", "UMBRELLA", "BOP", "PROFESSIONAL", "CYBER", "INLAND_MARINE",
         "LIQUOR", "EPLI")
ENTITY_TYPES = ("INDIVIDUAL", "PARTNERSHIP", "LLC", "CORPORATION", "S_CORPORATION", "NONPROFIT", "JOINT_VENTURE",
                "TRUST", "OTHER")
VEHICLE_USES = ("service", "retail", "commercial", "pleasure")
CHANGE_TYPES = ("ADD_DRIVER", "REMOVE_DRIVER", "ADD_VEHICLE", "REMOVE_VEHICLE", "ADD_LOCATION", "LIMIT_CHANGE",
                "NAME_CHANGE", "ADDRESS_CHANGE", "BILLING", "POLICY_QUESTION")
PROJECT_TYPES = ("residential", "commercial", "public", "other")
DECISIONS = ("APPROVE", "REVISE", "REJECT", "ESCALATE")
MESSAGE_KINDS = ("NOTIFY", "REQUEST", "REPLY")

ADDRESS_KEYS = ("street", "city", "state", "zip")
CONTACT_KEYS = ("name", "email", "phone")
DRIVER_KEYS = ("name", "dob", "dl_number", "dl_state", "years_licensed", "mvr_consent")
VEHICLE_KEYS = ("vin", "garaging_zip", "use", "stated_gvw")
PROPERTY_KEYS = ("location_index", "building_value", "contents_value", "construction", "year_built")
DEC_KEYS = ("limits", "deductibles", "forms", "endorsements", "exclusions")
POLICY_KEYS = ("line", "carrier", "policy_number", "effective", "expiration", "premium", "renewal_rate_change_pct",
               "nonrenewal_notice", "dec_page")
LOSS_RUN_KEYS = ("line", "carrier", "valued_as_of", "period_start", "period_end", "claims")
LOSS_KEYS = ("date_of_loss", "status", "paid", "reserved", "description")
ACCOUNT_KEYS = ("insured_name", "dba", "fein", "entity_type", "naics", "description_of_operations",
                "years_in_business", "mailing_address", "locations", "annual_revenue", "payroll", "employees",
                "contact", "drivers", "vehicles", "property_schedule", "prior_policies", "loss_runs")
REQUIREMENT_KEYS = ("limits", "additional_insured", "waiver_of_subrogation", "primary_noncontributory")
TICKET_KEYS = ("ticket_id", "type", "received", "account", "request")
PROPOSAL_KEYS = ("line", "carrier", "effective", "expiration", "premium", "limits", "deductibles", "forms", "endorsements",
                 "exclusions")
REQUEST_KEYS = {
    "QUOTE": ("lines", "effective_date", "contract_requirements"),
    "RENEWAL": ("policy_number",),
    "COI": ("holder_name", "holder_address", "lines", "description", "project_type", "requirements"),
    "SERVICE": ("change_type", "details", "driver", "vehicle"),
    "CLAIM": ("line", "date_of_loss", "description", "injury", "estimated_amount", "reported_by"),
    "POLICY_CHECK": ("policy_number", "proposal"),
}
REQUEST_REQUIRED = {
    "QUOTE": ("lines", "effective_date"),
    "RENEWAL": ("policy_number",),
    "COI": ("holder_name", "holder_address", "lines", "requirements"),
    "SERVICE": ("change_type", "details"),
    "CLAIM": ("line", "date_of_loss", "description", "injury"),
    "POLICY_CHECK": ("policy_number", "proposal"),
}
DECISION_KEYS = ("ticket_id", "decision", "target", "note", "approvals")
MESSAGE_KEYS = ("sender", "to", "kind", "ticket_id", "subject", "body")

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL = re.compile(r"^https?://[^\s/?#]+\S*$", re.I)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")   # strict: 3.11+ fromisoformat also takes 20260918 and 2026-W38-5
STATE = re.compile(r"^[A-Z]{2}$")
ZIP = re.compile(r"^\d{5}(?:-\d{4})?$")
_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
LIMIT_KEY = re.compile(r"^[A-Z_]+\.[a-z_]+$")    # "GL.each_occurrence"


class GateError(ValueError):
    """A record failed the schema gate. `code` is stable; tests, adapters and gate output key on it."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _fail(code: str, path: str, message: str) -> NoReturn:
    raise GateError(code, f"{path}: {message}")


# ── Dates ─────────────────────────────────────────────────────────────────────

def parse_date(value: Any, path: str = "$") -> dt.date:
    if not isinstance(value, str) or not ISO_DATE.match(value):
        _fail("BAD_DATE", path, f"expected YYYY-MM-DD, got {value!r}")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        _fail("BAD_DATE", path, f"expected YYYY-MM-DD, got {value!r}")


def as_date(value: Any, path: str = "as_of") -> dt.date:
    """A date, a datetime (its date) or a YYYY-MM-DD string."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return parse_date(value, path)


def parse_timestamp(value: Any, path: str = "$") -> dt.datetime:
    """ISO 8601 with a time ('2026-10-01T15:00:00+00:00', 'Z' accepted). Python 3.9's fromisoformat
    takes neither 'Z' nor odd fractions, so both are normalised first. A naive time is read as UTC."""
    m = _TIMESTAMP.match(value) if isinstance(value, str) else None
    if not m:
        _fail("BAD_DATE", path, f"expected an ISO timestamp like 2026-10-01T15:00:00+00:00, got {value!r}")
    zone = "+00:00" if m.group(2) in ("Z", None) else m.group(2)
    try:
        return dt.datetime.fromisoformat(m.group(1) + zone)
    except ValueError:
        _fail("BAD_DATE", path, f"not a real timestamp: {value!r}")


# ── Field helpers ─────────────────────────────────────────────────────────────

def _keys(obj: Any, path: str, allowed: tuple, required: tuple = ()) -> None:
    if not isinstance(obj, dict):
        _fail("TYPE", path, f"expected an object, got {type(obj).__name__}")
    graded = [k for k in obj if k in COMPUTED_KEYS]
    if graded:
        _fail("SELF_GRADED", f"{path}.{graded[0]}", "engine-computed field supplied by the model")
    unknown = [k for k in obj if k not in allowed]
    if unknown:
        _fail("UNKNOWN_KEY", f"{path}.{unknown[0]}", "key is not declared in the schema")
    missing = [k for k in required if k not in obj]
    if missing:
        _fail("MISSING", f"{path}.{missing[0]}", "required field absent")


def _str(obj: dict, key: str, path: str, *, nullable: bool = False, max_len: int = 2000) -> None:
    v = obj.get(key)
    if v is None and nullable:
        return
    if not isinstance(v, str) or not v.strip():
        _fail("TYPE", f"{path}.{key}", "expected a non-empty string" + (" or null" if nullable else ""))
    if len(v) > max_len:
        _fail("TOO_LONG", f"{path}.{key}", f"{len(v)} chars exceeds {max_len}")


def _enum(obj: dict, key: str, path: str, values: tuple, *, nullable: bool = False) -> None:
    if obj.get(key) is None and nullable:
        return
    if obj.get(key) not in values:
        _fail("BAD_ENUM", f"{path}.{key}", f"{obj.get(key)!r} not in {list(values)}")


def _num(obj: dict, key: str, path: str, *, nullable: bool = True, integer: bool = False, lo: float = 0) -> None:
    v = obj.get(key)
    if v is None and nullable:
        return
    ok = isinstance(v, int) if integer else isinstance(v, (int, float))
    if isinstance(v, bool) or not ok:
        _fail("TYPE", f"{path}.{key}", f"expected {'an integer' if integer else 'a number'}, got {v!r}")
    if v < lo:
        _fail("OUT_OF_RANGE", f"{path}.{key}", f"{v!r} is below {lo}")


def _bool(obj: dict, key: str, path: str, *, nullable: bool = False) -> None:
    v = obj.get(key)
    if v is None and nullable:
        return
    if not isinstance(v, bool):
        _fail("TYPE", f"{path}.{key}", f"expected true or false, got {v!r}")


def _date(obj: dict, key: str, path: str, *, nullable: bool = True) -> None:
    v = obj.get(key)
    if v is None and nullable:
        return
    parse_date(v, f"{path}.{key}")


def _list(obj: dict, key: str, path: str, *, min_items: int = 0, nullable: bool = True) -> list:
    v = obj.get(key)
    if v is None and nullable:
        return []
    if not isinstance(v, list):
        _fail("TYPE", f"{path}.{key}", f"expected a list, got {type(v).__name__}")
    if len(v) < min_items:
        _fail("MISSING", f"{path}.{key}", f"needs at least {min_items} item" + ("s" if min_items != 1 else ""))
    return v


def _str_list(obj: dict, key: str, path: str, *, min_items: int = 0, max_len: int = 200,
              values: tuple = ()) -> list:
    items = _list(obj, key, path, min_items=min_items)
    for i, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            _fail("TYPE", f"{path}.{key}[{i}]", "expected a non-empty string")
        if len(item) > max_len:
            _fail("TOO_LONG", f"{path}.{key}[{i}]", f"{len(item)} chars exceeds {max_len}")
        if values and item not in values:
            _fail("BAD_ENUM", f"{path}.{key}[{i}]", f"{item!r} not in {list(values)}")
    return items


def _amounts(obj: dict, key: str, path: str, *, keyed: bool = False) -> None:
    """{name: number >= 0}. With keyed=True names are "LINE.limit_name" (contract requirements)."""
    v = obj.get(key)
    if v is None:
        return
    if not isinstance(v, dict):
        _fail("TYPE", f"{path}.{key}", "expected an object of amounts")
    for name, amount in v.items():
        if keyed and not LIMIT_KEY.match(name):
            _fail("TYPE", f"{path}.{key}.{name}", 'expected "LINE.limit_name", e.g. "GL.each_occurrence"')
        if keyed and name.split(".")[0] not in LINES:
            _fail("BAD_ENUM", f"{path}.{key}.{name}", f"line not in {list(LINES)}")
        _num(v, name, f"{path}.{key}", nullable=False)


def _address(obj: dict, key: str, path: str, *, nullable: bool = True) -> None:
    a = obj.get(key)
    if a is None and nullable:
        return
    _addr(a, f"{path}.{key}")


def _addr(a: Any, p: str) -> None:
    _keys(a, p, ADDRESS_KEYS, ADDRESS_KEYS)
    _str(a, "street", p, max_len=200)
    _str(a, "city", p, max_len=100)
    if not isinstance(a["state"], str) or not STATE.match(a["state"]):
        _fail("TYPE", f"{p}.state", f"expected a two-letter state, got {a['state']!r}")
    if not isinstance(a["zip"], str) or not ZIP.match(a["zip"]):
        _fail("TYPE", f"{p}.zip", f"expected a ZIP code string, got {a['zip']!r}")


# ── Evidence (spine S3) ───────────────────────────────────────────────────────

def check_evidence(ev: Any, as_of: Any, path: str = "$") -> dict:
    """VERIFIED needs an http(s) URL and an observed date that is not after as_of.
    INFERENCE and HYPOTHESIS need neither. `as_of` is a date or a YYYY-MM-DD string."""
    as_of = as_date(as_of)
    _keys(ev, path, ("status", "url", "observed"), ("status",))
    status = ev["status"]
    if status not in EVIDENCE_STATUSES:
        _fail("BAD_STATUS", f"{path}.status", f"{status!r} not in {list(EVIDENCE_STATUSES)}")
    url, observed = ev.get("url"), ev.get("observed")
    if url is not None and (not isinstance(url, str) or not URL.match(url)):
        if status == "VERIFIED":
            _fail("MISSING_URL", f"{path}.url", "VERIFIED needs an http(s) source URL")
        _fail("BAD_URL", f"{path}.url", f"not an http(s) URL: {url!r}")
    if status == "VERIFIED" and not url:
        _fail("MISSING_URL", f"{path}.url", "VERIFIED needs an http(s) source URL")
    if status == "VERIFIED" and not observed:
        _fail("MISSING_DATE", f"{path}.observed", "VERIFIED needs an observed date")
    if observed is not None and parse_date(observed, f"{path}.observed") > as_of:
        _fail("FUTURE_DATED", f"{path}.observed", f"{observed} is after as_of {as_of.isoformat()}")
    return ev


# ── Market-fit judgment (CSR 4's half of carrier scoring) ─────────────────────

def check_judgment(payload: Any, path: str = "$") -> dict:
    """{"carrier": str, "scores": {<the five JUDGMENT_DIMS>: int 0-5}, "rationale": str}.
    No total, tier, score, rank, priority or age_days anywhere in it (S2); no other key (S1)."""
    _keys(payload, path, JUDGMENT_KEYS, JUDGMENT_KEYS)
    _str(payload, "carrier", path, max_len=120)
    _str(payload, "rationale", path, max_len=600)
    scores, sp = payload["scores"], f"{path}.scores"
    _keys(scores, sp, JUDGMENT_DIMS, JUDGMENT_DIMS)
    for dim in JUDGMENT_DIMS:
        s = scores[dim]
        if isinstance(s, bool) or not isinstance(s, int) or not 0 <= s <= 5:
            _fail("OUT_OF_RANGE", f"{sp}.{dim}", f"expected an integer 0-5, got {s!r}")
    return payload


# ── Account and ticket ────────────────────────────────────────────────────────

def _check_dec(d: Any, path: str) -> None:
    _keys(d, path, DEC_KEYS, ("limits",))
    _amounts(d, "limits", path)
    _amounts(d, "deductibles", path)
    for k in ("forms", "endorsements", "exclusions"):
        _str_list(d, k, path)


def _check_policy(p: Any, path: str) -> None:
    _keys(p, path, POLICY_KEYS, ("line", "carrier", "policy_number", "effective", "expiration"))
    _enum(p, "line", path, LINES)
    _str(p, "carrier", path, max_len=120)
    _str(p, "policy_number", path, max_len=60)
    _date(p, "effective", path, nullable=False)
    _date(p, "expiration", path, nullable=False)
    if parse_date(p["expiration"]) <= parse_date(p["effective"]):
        _fail("OUT_OF_RANGE", f"{path}.expiration", "expiration must be after effective")
    _num(p, "premium", path)
    _num(p, "renewal_rate_change_pct", path, lo=-100)
    _bool(p, "nonrenewal_notice", path, nullable=True)
    if p.get("dec_page") is not None:
        _check_dec(p["dec_page"], f"{path}.dec_page")


def _check_loss_run(r: Any, path: str) -> None:
    _keys(r, path, LOSS_RUN_KEYS, LOSS_RUN_KEYS)
    _enum(r, "line", path, LINES)
    _str(r, "carrier", path, max_len=120)
    for k in ("valued_as_of", "period_start", "period_end"):
        _date(r, k, path, nullable=False)
    for i, c in enumerate(_list(r, "claims", path, nullable=False)):
        cp = f"{path}.claims[{i}]"
        _keys(c, cp, LOSS_KEYS, LOSS_KEYS)
        _date(c, "date_of_loss", cp, nullable=False)
        _enum(c, "status", cp, ("OPEN", "CLOSED"))
        _num(c, "paid", cp, nullable=False)
        _num(c, "reserved", cp, nullable=False)
        _str(c, "description", cp, max_len=400)


def _check_driver(d: Any, path: str) -> None:
    _keys(d, path, DRIVER_KEYS, ("name",))
    _str(d, "name", path, max_len=120)
    _date(d, "dob", path)
    _str(d, "dl_number", path, nullable=True, max_len=30)
    if d.get("dl_state") is not None and (not isinstance(d["dl_state"], str) or not STATE.match(d["dl_state"])):
        _fail("TYPE", f"{path}.dl_state", f"expected a two-letter state, got {d['dl_state']!r}")
    _num(d, "years_licensed", path, integer=True)
    _bool(d, "mvr_consent", path, nullable=True)


def _check_vehicle(v: Any, path: str) -> None:
    _keys(v, path, VEHICLE_KEYS, ("vin",))
    _str(v, "vin", path, max_len=17)
    if v.get("garaging_zip") is not None and (not isinstance(v["garaging_zip"], str) or not ZIP.match(v["garaging_zip"])):
        _fail("TYPE", f"{path}.garaging_zip", f"expected a ZIP code string, got {v['garaging_zip']!r}")
    _enum(v, "use", path, VEHICLE_USES, nullable=True)
    _num(v, "stated_gvw", path, integer=True)


def check_account(a: Any, path: str = "$.account") -> dict:
    _keys(a, path, ACCOUNT_KEYS, ("insured_name",))
    _str(a, "insured_name", path, max_len=200)
    _str(a, "dba", path, nullable=True, max_len=200)
    _str(a, "fein", path, nullable=True, max_len=12)
    _enum(a, "entity_type", path, ENTITY_TYPES, nullable=True)
    _str(a, "naics", path, nullable=True, max_len=6)
    _str(a, "description_of_operations", path, nullable=True, max_len=1000)
    _num(a, "years_in_business", path, integer=True)
    _address(a, "mailing_address", path)
    for i, loc in enumerate(_list(a, "locations", path)):
        _addr(loc, f"{path}.locations[{i}]")
    _num(a, "annual_revenue", path)
    _num(a, "payroll", path)
    _num(a, "employees", path, integer=True)
    if a.get("contact") is not None:
        c, cp = a["contact"], f"{path}.contact"
        _keys(c, cp, CONTACT_KEYS, ("name",))
        _str(c, "name", cp, max_len=120)
        if c.get("email") is not None and (not isinstance(c["email"], str) or not EMAIL.match(c["email"])):
            _fail("BAD_EMAIL", f"{cp}.email", f"expected an email address, got {c['email']!r}")
        _str(c, "phone", cp, nullable=True, max_len=30)
    for i, d in enumerate(_list(a, "drivers", path)):
        _check_driver(d, f"{path}.drivers[{i}]")
    for i, v in enumerate(_list(a, "vehicles", path)):
        _check_vehicle(v, f"{path}.vehicles[{i}]")
    for i, s in enumerate(_list(a, "property_schedule", path)):
        sp = f"{path}.property_schedule[{i}]"
        _keys(s, sp, PROPERTY_KEYS, ("location_index",))
        _num(s, "location_index", sp, integer=True, nullable=False)
        _num(s, "building_value", sp)
        _num(s, "contents_value", sp)
        _str(s, "construction", sp, nullable=True, max_len=60)
        _num(s, "year_built", sp, integer=True)
    for i, p in enumerate(_list(a, "prior_policies", path)):
        _check_policy(p, f"{path}.prior_policies[{i}]")
    for i, r in enumerate(_list(a, "loss_runs", path)):
        _check_loss_run(r, f"{path}.loss_runs[{i}]")
    return a


def _check_requirements(r: Any, path: str, *, nullable: bool) -> None:
    if r is None and nullable:
        return
    _keys(r, path, REQUIREMENT_KEYS)
    _amounts(r, "limits", path, keyed=True)
    for k in ("additional_insured", "waiver_of_subrogation", "primary_noncontributory"):
        _bool(r, k, path, nullable=True)


def check_ticket(t: Any, path: str = "$") -> dict:
    _keys(t, path, TICKET_KEYS, TICKET_KEYS)
    _str(t, "ticket_id", path, max_len=60)
    _enum(t, "type", path, TICKET_TYPES)
    parse_timestamp(t["received"], f"{path}.received")
    check_account(t["account"], f"{path}.account")
    kind, r, rp = t["type"], t["request"], f"{path}.request"
    _keys(r, rp, REQUEST_KEYS[kind], REQUEST_REQUIRED[kind])
    if kind == "QUOTE":
        _str_list(r, "lines", rp, min_items=1, values=LINES)
        _date(r, "effective_date", rp, nullable=False)
        _check_requirements(r.get("contract_requirements"), f"{rp}.contract_requirements", nullable=True)
    elif kind == "RENEWAL":
        _str(r, "policy_number", rp, max_len=60)
    elif kind == "COI":
        _str(r, "holder_name", rp, max_len=200)
        _address(r, "holder_address", rp, nullable=False)
        _str_list(r, "lines", rp, min_items=1, values=LINES)
        _str(r, "description", rp, nullable=True, max_len=600)
        _enum(r, "project_type", rp, PROJECT_TYPES, nullable=True)
        _check_requirements(r["requirements"], f"{rp}.requirements", nullable=False)
    elif kind == "SERVICE":
        _enum(r, "change_type", rp, CHANGE_TYPES)
        _str(r, "details", rp, max_len=1000)
        if r.get("driver") is not None:
            _check_driver(r["driver"], f"{rp}.driver")
        if r.get("vehicle") is not None:
            _check_vehicle(r["vehicle"], f"{rp}.vehicle")
    elif kind == "CLAIM":
        _enum(r, "line", rp, LINES)
        _date(r, "date_of_loss", rp, nullable=False)
        _str(r, "description", rp, max_len=2000)
        _bool(r, "injury", rp)
        _num(r, "estimated_amount", rp)
        _str(r, "reported_by", rp, nullable=True, max_len=120)
    elif kind == "POLICY_CHECK":
        _str(r, "policy_number", rp, max_len=60)
        pr, pp = r["proposal"], f"{rp}.proposal"
        _keys(pr, pp, PROPOSAL_KEYS, ("line", "carrier", "effective", "expiration", "limits"))
        _enum(pr, "line", pp, LINES)
        _str(pr, "carrier", pp, max_len=120)
        _date(pr, "effective", pp, nullable=False)
        _date(pr, "expiration", pp, nullable=False)
        _num(pr, "premium", pp)
        _amounts(pr, "limits", pp)
        _amounts(pr, "deductibles", pp)
        for k in ("forms", "endorsements", "exclusions"):
            _str_list(pr, k, pp)
    return t


# ── AE feedback and the bus ───────────────────────────────────────────────────

def check_ae_decision(d: Any, path: str = "$") -> dict:
    """APPROVE | REVISE (target = the CSR that redoes the work) | REJECT | ESCALATE. `approvals` are
    scopes "<tool>:<target>" the AE grants for this ticket's IRREVERSIBLE acts (spine S8)."""
    _keys(d, path, DECISION_KEYS, ("ticket_id", "decision", "note"))
    _str(d, "ticket_id", path, max_len=60)
    _enum(d, "decision", path, DECISIONS)
    _str(d, "note", path, max_len=1000)
    if d["decision"] == "REVISE":
        _enum(d, "target", path, tuple(a for a in AGENTS if a != "AE"))
    elif d.get("target") is not None:
        _fail("OUT_OF_RANGE", f"{path}.target", "only a REVISE names a target CSR")
    for i, scope in enumerate(_str_list(d, "approvals", path)):
        if ":" not in scope:
            _fail("TYPE", f"{path}.approvals[{i}]", 'expected "<tool>:<target>"')
    return d


def check_message(m: Any, path: str = "$") -> dict:
    """Any CSR may message any CSR (the mesh). A message informs; it never moves control — only the
    router hands a ticket to the next agent (routing.py)."""
    _keys(m, path, MESSAGE_KEYS, MESSAGE_KEYS)
    _enum(m, "sender", path, AGENTS)
    _enum(m, "to", path, AGENTS)
    if m["sender"] == m["to"]:
        _fail("OUT_OF_RANGE", f"{path}.to", "an agent does not message itself")
    _enum(m, "kind", path, MESSAGE_KINDS)
    _str(m, "ticket_id", path, max_len=60)
    _str(m, "subject", path, max_len=200)
    _str(m, "body", path, max_len=2000)
    return m
