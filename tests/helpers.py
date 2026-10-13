"""Shared fixtures: the sample tickets, an offline stand-in for NHTSA/Census, and an isolated runtime."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

from csrdesk import pii, policy, schemas, thresholds  # noqa: E402
from csrdesk.desk import Ctx  # noqa: E402

AS_OF = "2026-10-12"
NOW = schemas.parse_timestamp("2026-10-12T13:30:00Z")

# Shapes copied from real vPIC / Census responses (2026-10-12), trimmed to the fields the engine reads.
VPIC = {
    "1FTFW1E5": {"ModelYear": "2022", "Make": "FORD", "Model": "F-150", "BodyClass": "Pickup", "VehicleType": "TRUCK",
                 "GVWR": "Class 2F: 7,001 - 8,000 lb (3,175 - 3,629 kg)"},
    "3C6UR5DL": {"ModelYear": "2019", "Make": "RAM", "Model": "2500", "BodyClass": "Pickup", "VehicleType": "TRUCK",
                 "GVWR": "Class 2H: 9,001 - 10,000 lb (4,082 - 4,536 kg)"},
    "1FVACWDT": {"ModelYear": "2017", "Make": "FREIGHTLINER", "Model": "M2", "BodyClass": "Truck", "VehicleType": "TRUCK",
                 "GVWR": "Class 6: 19,501 - 26,000 lb (8,845 - 11,794 kg)"},
}


class FakeNet:
    """Records every URL; answers vPIC by VIN prefix and the geocoder for Riverside Ave only."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            raise OSError("network down")
        if "vpic" in url:
            vin = url.split("DecodeVinValues/")[1].split("?")[0]
            return {"Results": [{"ErrorCode": "0", "ErrorText": "0 - VIN decoded clean", **VPIC.get(vin[:8], {})}]}
        if "Riverside" in url:
            return {"result": {"addressMatches": [{"matchedAddress": "1 RIVERSIDE AVE, LYNDHURST, NJ, 07071",
                                                   "geographies": {"Counties": [{"NAME": "Bergen County", "STATE": "34", "GEOID": "34003"}]}}]}}
        return {"result": {"addressMatches": []}}


def ticket(name: str) -> dict:
    return json.loads((ROOT / "data" / "examples" / name).read_text(encoding="utf-8"))


def ctx(t: dict, state: dict = None, *, as_of: str = AS_OF, config: dict = None, fetch=None, budget=None) -> Ctx:
    schemas.check_ticket(t)
    return Ctx(ticket=pii.masked_ticket(t), state=state or {}, as_of=as_of,
               budget=budget or policy.Budget(thresholds.TICKET_BUDGET), config=config or {}, fetch=fetch or FakeNet())


def isolated_config() -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="csrdesk-test-"))
    return {"db": str(tmp / "agency.db"), "audit_dir": str(tmp / "audit"), "tickets_dir": str(tmp / "tickets"),
            "kill_switch": str(tmp / "STOP"), "_tmp": tmp}


def edit(t: dict, fn) -> dict:
    t = copy.deepcopy(t)
    fn(t)
    return t
