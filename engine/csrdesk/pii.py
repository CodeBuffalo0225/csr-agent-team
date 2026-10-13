"""What an agent, a log line or an AE packet is allowed to see of a person.

Driver's license numbers are DPPA-protected personal information; FEINs and dates of birth are
GLBA nonpublic personal information once an agency holds them. The raw values are needed exactly
twice: on the carrier application and on an MVR order, both IRREVERSIBLE acts the AE approves. So
everything else — agent context, the audit log, the AE packet, the database row CSR 8 writes — gets
the masked form. Masking happens once, at intake, and the masked copy is what flows.
"""
from __future__ import annotations

import copy
import re


def mask_tail(value: str, keep: int = 4) -> str:
    """'D1234567890' -> '•••••••7890'. None stays None (an absent value is not a masked one)."""
    if value is None:
        return None
    s = re.sub(r"\s", "", str(value))
    if len(s) <= keep:
        return "•" * len(s)
    return "•" * (len(s) - keep) + s[-keep:]


def mask_fein(fein: str) -> str:
    if fein is None:
        return None
    digits = re.sub(r"\D", "", fein)
    return f"••-•••{digits[-4:]}" if len(digits) == 9 else mask_tail(fein)


def mask_dob(dob: str) -> str:
    """Year only: enough for an underwriter's age band, not enough to identify a person."""
    return None if dob is None else f"{dob[:4]}-••-••"


# Patterns masked inside free text (descriptions, details, notes): SSNs, FEINs, and license-shaped tokens
# (a letter followed by 7+ digits, or 9+ digits). Free text is where a client pastes the number anyway.
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_FEIN = re.compile(r"\b\d{2}-\d{7}\b")
_LONG_ID = re.compile(r"\b(?:[A-Za-z]\d{7,}|\d{9,})\b")


def scrub_text(text):
    if not isinstance(text, str):
        return text
    text = _SSN.sub(lambda m: "•••-••-" + m.group(0)[-4:], text)
    text = _FEIN.sub(lambda m: "••-•••" + m.group(0)[-4:], text)
    return _LONG_ID.sub(lambda m: mask_tail(m.group(0)), text)


FREE_TEXT = ("details", "description")


def masked_account(account: dict) -> dict:
    """A deep copy safe to hand to an agent, a log or the AE packet."""
    a = copy.deepcopy(account)
    a["fein"] = mask_fein(a.get("fein"))
    a["description_of_operations"] = scrub_text(a.get("description_of_operations"))
    for d in a.get("drivers") or []:
        d["dl_number"] = mask_tail(d.get("dl_number"))
        d["dob"] = mask_dob(d.get("dob"))
    return a


def masked_ticket(ticket: dict) -> dict:
    t = copy.deepcopy(ticket)
    t["account"] = masked_account(t["account"])
    for k in FREE_TEXT:
        if k in (t.get("request") or {}):
            t["request"][k] = scrub_text(t["request"][k])
    for c in [c for r in t["account"].get("loss_runs") or [] for c in r["claims"]]:
        c["description"] = scrub_text(c["description"])
    drv = (t.get("request") or {}).get("driver")
    if drv:
        drv["dl_number"] = mask_tail(drv.get("dl_number"))
        drv["dob"] = mask_dob(drv.get("dob"))
    return t
