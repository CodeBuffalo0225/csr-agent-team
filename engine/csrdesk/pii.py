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


def masked_account(account: dict) -> dict:
    """A deep copy safe to hand to an agent, a log or the AE packet."""
    a = copy.deepcopy(account)
    a["fein"] = mask_fein(a.get("fein"))
    for d in a.get("drivers") or []:
        d["dl_number"] = mask_tail(d.get("dl_number"))
        d["dob"] = mask_dob(d.get("dob"))
    return a


def masked_ticket(ticket: dict) -> dict:
    t = copy.deepcopy(ticket)
    t["account"] = masked_account(t["account"])
    drv = (t.get("request") or {}).get("driver")
    if drv:
        drv["dl_number"] = mask_tail(drv.get("dl_number"))
        drv["dob"] = mask_dob(drv.get("dob"))
    return t
