"""The only outside calls the engine makes itself: free, keyless US government data.

    vin_check(vin)                         local ISO 3779 check digit; catches a mistyped VIN before any call
    decode_vins(vins, budget, as_of)       NHTSA vPIC: year, make, model, body, GVWR band  -> vehicle schedule
    geocode(address, budget, as_of)        Census geocoder: matched address, county, FIPS   -> territory, garaging

Every result carries spine-S3 evidence (the exact URL called and the date). Every failure is
honest (spine S6): {"status": "unavailable", "value": None, "error": ...} — never an empty schedule
that reads like "no vehicles", never a guessed county.

Each call is budgeted (spine S9) against the ticket's Budget before it is made. `fetch` is
injectable so tests never touch the network.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Callable

from . import scoring

VPIC = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"
GEOCODER = ("https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress?address={q}"
            "&benchmark=Public_AR_Current&vintage=Current_Current&format=json")
TIMEOUT_S = 20
RETRIES = 2                       # after the first attempt; backoff 1s, 2s
USER_AGENT = "csr-agent-team/0.1 (agency CSR desk; contact via repo owner)"

_TRANSLIT = {**{str(i): i for i in range(10)}, **dict(zip("ABCDEFGH", range(1, 9))),
             **dict(zip("JKLMN", range(1, 6))), "P": 7, "R": 9, **dict(zip("STUVWXYZ", range(2, 10)))}
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)


def vin_check(vin: str) -> dict:
    """{"ok", "reason"}. 17 characters, no I/O/Q, and the 9th character matches the computed check digit."""
    v = (vin or "").strip().upper()
    if len(v) != 17:
        return {"ok": False, "reason": f"{len(v)} characters; a VIN has 17"}
    bad = [c for c in v if c not in _TRANSLIT]
    if bad:
        return {"ok": False, "reason": f"contains {bad[0]!r}; VINs never use I, O or Q"}
    s = sum(_TRANSLIT[c] * w for c, w in zip(v, _WEIGHTS)) % 11
    expect = "X" if s == 10 else str(s)
    if v[8] != expect:
        return {"ok": False, "reason": f"check digit is {v[8]!r}, computed {expect!r}: a character is mistyped"}
    return {"ok": True, "reason": None}


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


def _with_retry(fetch: Callable[[str], dict], url: str, sleep=time.sleep) -> dict:
    last = None
    for attempt in range(RETRIES + 1):
        try:
            return fetch(url)
        except Exception as e:      # network, HTTP or JSON: all retried, then reported
            last = e
            if attempt < RETRIES:
                sleep(2 ** attempt)
    raise last


def decode_vins(vehicles: list, budget, as_of: str, fetch: Callable[[str], dict] = _http_json,
                sleep=time.sleep) -> dict:
    """One decoded row per vehicle. A VIN that fails the local check digit is never sent: it comes back
    `confirm_with_client`. A call refused by the budget or failed by the network comes back
    `unavailable` with the reason; the others still decode."""
    rows = []
    for veh in vehicles:
        vin = veh["vin"].strip().upper()
        chk = vin_check(vin)
        if not chk["ok"]:
            rows.append({"vin": vin, "status": "confirm_with_client", "reason": chk["reason"], "value": None})
            continue
        verdict = budget.check("READ")
        if verdict != "OK":
            rows.append({"vin": vin, "status": "unavailable", "reason": f"ticket budget: {verdict}", "value": None})
            continue
        url = VPIC.format(vin=vin)
        try:
            r = _with_retry(fetch, url, sleep)["Results"][0]
        except Exception as e:
            rows.append({"vin": vin, "status": "unavailable", "reason": f"{type(e).__name__}: {e}", "value": None})
            continue
        codes = {c.strip() for c in str(r.get("ErrorCode", "")).split(",")}
        size = scoring.size_class(r.get("GVWR", ""), veh.get("stated_gvw"))
        rows.append({
            "vin": vin,
            "status": "decoded" if codes <= {"0"} else "decoded_with_warnings",
            "reason": None if codes <= {"0"} else r.get("ErrorText"),
            "value": {"year": r.get("ModelYear") or None, "make": r.get("Make") or None,
                      "model": r.get("Model") or None, "body_class": r.get("BodyClass") or None,
                      "vehicle_type": r.get("VehicleType") or None, "gvwr": r.get("GVWR") or None,
                      "garaging_zip": veh.get("garaging_zip"), "use": veh.get("use"), **size},
            "evidence": {"status": "VERIFIED", "url": url, "observed": as_of},
        })
    return {"status": "ok", "vehicles": rows}


def geocode(address: dict, budget, as_of: str, fetch: Callable[[str], dict] = _http_json,
            sleep=time.sleep) -> dict:
    """Census one-line geocode -> matched address, county, state and county FIPS. No match is
    `no_match` (the address is wrong or new: ask the client), not a guess at the nearest town."""
    one_line = f"{address['street']}, {address['city']}, {address['state']} {address['zip']}"
    verdict = budget.check("READ")
    if verdict != "OK":
        return {"status": "unavailable", "value": None, "error": f"ticket budget: {verdict}", "input": one_line}
    url = GEOCODER.format(q=urllib.parse.quote(one_line))
    try:
        matches = _with_retry(fetch, url, sleep)["result"]["addressMatches"]
    except Exception as e:
        return {"status": "unavailable", "value": None, "error": f"{type(e).__name__}: {e}", "input": one_line}
    if not matches:
        return {"status": "no_match", "value": None, "input": one_line,
                "evidence": {"status": "VERIFIED", "url": url, "observed": as_of}}
    m = matches[0]
    county = (m.get("geographies", {}).get("Counties") or [{}])[0]
    return {"status": "ok", "input": one_line,
            "value": {"matched_address": m.get("matchedAddress"), "county": county.get("NAME"),
                      "state_fips": county.get("STATE"), "county_fips": county.get("GEOID")},
            "evidence": {"status": "VERIFIED", "url": url, "observed": as_of}}
