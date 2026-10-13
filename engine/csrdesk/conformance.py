"""spine-spec adapter. Routes each vector kind to the PRODUCTION gate.

Nothing in this file re-implements a rule. If a vector fails, the fix belongs in grounding.py,
schemas.py, scoring.py or policy.py, never here (AGENTS.md hard rule 8). The adapter only
translates the canonical input into a call to the real gate and the result back into the canonical
output.

The measured bar is every vector in conformance/spine-v1.json passing, with zero skips. SKIPS
exists so that a skip, if one is ever allowed, carries its written reason and is reported; it still
turns `ok` False.

    python3 -m csrdesk.conformance      prints the result as JSON, exit 0 when ok, 1 when not
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from . import grounding, policy, schemas, scoring

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "conformance" / "spine-v1.json"
PIN = ROOT / "conformance" / "PIN"
CANON = ("entity", "scores", "rationale")
SKIPS: dict = {}  # "<invariant>": "SKIP: <invariant> - <why>". Empty: every invariant is exercised.


def _judgment(inp: dict) -> dict:
    """Canonical {entity, scores{d1..dn}, rationale} -> CSR 4's {carrier, scores{dims}, rationale}.
    dN maps positionally onto JUDGMENT_DIMS; dimensions a canonical payload does not name are scored 0
    (the production gate requires all five). Any other key is passed through for S1 and S2 to judge."""
    p = inp["payload"]
    dims = schemas.JUDGMENT_DIMS
    named = sorted(p["scores"].items(), key=lambda kv: int(kv[0][1:]))
    scores = {d: 0 for d in dims}
    for i, (name, value) in enumerate(named):
        scores[dims[i] if i < len(dims) else name] = value
    real = {"carrier": p["entity"], "scores": scores, "rationale": p["rationale"]}
    real.update({k: v for k, v in p.items() if k not in CANON})
    try:
        schemas.check_judgment(real)
        return {"ok": True}
    except schemas.GateError as e:
        return {"ok": False, "code": e.code}


def _evidence(inp: dict) -> dict:
    try:
        schemas.check_evidence(inp["evidence"], inp["as_of"])
        return {"ok": True}
    except schemas.GateError as e:
        return {"ok": False, "code": e.code}


def _budget(inp: dict) -> dict:
    b = policy.Budget(inp["limits"])
    return {"results": [b.check(c["risk"], c["valid"]) for c in inp["calls"]]}


def _approval(inp: dict) -> dict:
    return policy.decide(inp["tool"], inp["target"], inp["risk"], live=inp["live"],
                         approvals=inp["approvals"], content_ok=inp["content_ok"], harness=inp["harness"])


def _degrade(inp: dict) -> dict:
    # No completed call in the log means the vendor has no evidence of a working credential.
    # With a credential, the same gate is shown the completed call that proves it.
    done = [{"event": "call_ok", "vendor": "ams", "ts": "2026-10-12T00:00:00+00:00"}]
    return policy.vendor_readiness(done if inp["credential_present"] else [], "ams")


ADAPTERS = {
    "judgment": _judgment,
    "evidence": _evidence,
    "age": lambda i: {"age_days": scoring.age_days(i["observed"], i["as_of"])},
    "grounding": lambda i: grounding.ground(i["text"], i["allowed"]),
    "degrade": _degrade,
    "cap": lambda i: {"admitted": [x["id"] for x in scoring.admit(i["items"], i["floor"], i["cap"])]},
    "approval": _approval,
    "budget": _budget,
    "classify": lambda i: {"risk": policy.classify(i["tool"])["risk"]},
}


def _matches(expect: dict, got: dict) -> bool:
    """Every expected key must be present in the output and equal. A missing key is not a null."""
    return all(k in got and got[k] == v for k, v in expect.items())


def run_vector(v: dict) -> dict:
    if v["invariant"] in SKIPS:
        return {"id": v["id"], "invariant": v["invariant"], "status": "SKIP", "why": SKIPS[v["invariant"]]}
    adapter = ADAPTERS.get(v["kind"])
    if adapter is None:
        got = {"error": f"no adapter for vector kind {v['kind']!r}"}
    else:
        try:
            got = adapter(v["input"])
        except Exception as e:  # production code crashing on a vector is a failure, reported, not a crash here
            got = {"error": f"{type(e).__name__}: {e}"}
    return {"id": v["id"], "invariant": v["invariant"], "status": "PASS" if _matches(v["expect"], got) else "FAIL",
            "expect": v["expect"], "got": got}


def _pin_fields() -> dict:
    return dict(line.split("=", 1) for line in PIN.read_text(encoding="utf-8").split() if "=" in line)


def pin_status() -> dict:
    """Is the vendored vector file the one that was pinned? `upstream` is informational: the version
    found in a local spine-spec checkout, so a newer spec shows up as drift to re-pin on purpose."""
    raw = VECTORS.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    pin = _pin_fields()
    doc_version = json.loads(raw.decode("utf-8")).get("version")
    out = {"version": pin.get("version"), "source": pin.get("source"), "pinned_sha": pin.get("sha256"),
           "actual_sha": sha, "intact": pin.get("sha256") == sha and pin.get("version") == doc_version,
           "upstream": None}
    upstream = Path(os.environ.get("SPINE_SPEC_DIR", Path.home() / "Projects" / "spine-spec"))
    if (upstream / "VERSION").exists():
        out["upstream"] = (upstream / "VERSION").read_text(encoding="utf-8").strip()
    return out


def run() -> dict:
    doc = json.loads(VECTORS.read_text(encoding="utf-8"))
    results = [run_vector(v) for v in doc["vectors"]]
    failed = [r for r in results if r["status"] == "FAIL"]
    skipped = [r for r in results if r["status"] == "SKIP"]
    pin = pin_status()
    return {"spec_version": doc["version"], "vectors": len(results),
            "passed": sum(r["status"] == "PASS" for r in results), "failed": failed,
            "skipped": skipped, "pin": pin, "ok": not failed and not skipped and pin["intact"]}


def main() -> int:
    result = run()
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
