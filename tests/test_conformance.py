"""Spine conformance: every vector in conformance/spine-v1.json against PRODUCTION code.

The bar: every vector passes, zero skips. Beyond that this file
proves the bar means something: the adapter only routes to production functions, and breaking a
production function makes its invariant's vectors fail.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

from csrdesk import conformance, grounding, policy, schemas, scoring  # noqa: E402

VECTOR_FILE = ROOT / "conformance" / "spine-v1.json"
DOC = json.loads(VECTOR_FILE.read_text(encoding="utf-8"))


class EveryVector(unittest.TestCase):
    """One test per vector (test_S5_02, ...), so a failure names the vector and -v lists all of them."""


def _vector_test(v):
    def test(self):
        r = conformance.run_vector(v)
        self.assertEqual(r["status"], "PASS", f"{v['id']} {v['invariant']}: expected {r['expect']}, got {r['got']} ({v.get('note', '')})")
    test.__doc__ = f"{v['invariant']}: {v.get('note') or v['kind']}"
    return test


for _v in DOC["vectors"]:
    setattr(EveryVector, "test_" + _v["id"].replace("-", "_"), _vector_test(_v))


class Bar(unittest.TestCase):
    def test_run_is_green_with_zero_skips(self):
        r = conformance.run()
        self.assertEqual(r["failed"], [])
        self.assertEqual(r["skipped"], [], "zero skips is the bar")
        self.assertEqual(r["vectors"], len(DOC["vectors"]))
        self.assertEqual(r["passed"], r["vectors"])
        self.assertEqual(r["spec_version"], DOC["version"])
        self.assertTrue(r["pin"]["intact"])
        self.assertTrue(r["ok"])
        self.assertEqual(set(r), {"spec_version", "vectors", "passed", "failed", "skipped", "pin", "ok"})

    def test_every_vector_kind_has_an_adapter(self):
        self.assertLessEqual({v["kind"] for v in DOC["vectors"]}, set(conformance.ADAPTERS))

    def test_every_invariant_in_the_spec_is_exercised(self):
        self.assertEqual({v["invariant"] for v in DOC["vectors"]},
                         {"STRICT_SCHEMA", "NO_SELF_GRADING", "EVIDENCE", "DERIVED_AGE", "GROUNDING",
                          "HONEST_DEGRADATION", "CAPS", "SCOPED_APPROVAL", "BUDGET", "FAIL_CLOSED"})

    def test_a_new_vector_kind_without_an_adapter_fails_loudly(self):
        r = conformance.run_vector({"id": "X-01", "invariant": "GROUNDING", "kind": "zz_new_kind",
                                    "input": {}, "expect": {"ok": True}})
        self.assertEqual(r["status"], "FAIL")
        self.assertIn("no adapter", r["got"]["error"])

    def test_production_code_crashing_on_a_vector_is_a_failure_not_a_crash(self):
        v = next(v for v in DOC["vectors"] if v["kind"] == "grounding")
        with mock.patch.object(grounding, "ground", side_effect=RuntimeError("boom")):
            r = conformance.run_vector(v)
        self.assertEqual(r["status"], "FAIL")
        self.assertIn("RuntimeError", r["got"]["error"])

    def test_a_skip_is_reported_with_its_reason_and_still_fails_the_bar(self):
        reason = "SKIP: CAPS - a test"
        with mock.patch.dict(conformance.SKIPS, {"CAPS": reason}):
            r = conformance.run()
        self.assertTrue(r["skipped"])
        self.assertTrue(all(s["why"] == reason for s in r["skipped"]))
        self.assertFalse(r["ok"])

    def test_a_missing_key_is_not_a_null(self):
        self.assertTrue(conformance._matches({"value": None}, {"value": None}))
        self.assertFalse(conformance._matches({"value": None}, {}))

    def test_module_entry_point_exits_zero(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "engine")}
        p = subprocess.run([sys.executable, "-m", "csrdesk.conformance"], capture_output=True, text=True,
                           env=env, cwd=str(ROOT), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout)["ok"])


class Pin(unittest.TestCase):
    def pin_fields(self):
        return dict(line.split("=", 1) for line in (ROOT / "conformance" / "PIN").read_text(encoding="utf-8").split() if "=" in line)

    def test_vendored_file_matches_the_pinned_sha(self):
        self.assertEqual(self.pin_fields()["sha256"], hashlib.sha256(VECTOR_FILE.read_bytes()).hexdigest(),
                         "vendored vectors were edited: re-vendor from spine-spec and re-pin, never edit")

    def test_pin_records_version_and_source(self):
        pin = self.pin_fields()
        self.assertEqual(pin["version"], DOC["version"])
        self.assertRegex(pin["source"], r"^CodeBuffalo0225/spine-spec@[0-9a-f]{7,40}$")
        self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")

    def test_an_edited_vector_file_breaks_the_pin_and_the_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            edited = Path(tmp) / "spine-v1.json"
            edited.write_bytes(VECTOR_FILE.read_bytes() + b"\n")   # one byte: semantically identical, still tampering
            with mock.patch.object(conformance, "VECTORS", edited):
                self.assertFalse(conformance.pin_status()["intact"])
                self.assertFalse(conformance.run()["ok"])

    def test_a_pinned_version_that_disagrees_with_the_file_breaks_the_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            pin = Path(tmp) / "PIN"
            pin.write_text(f"version=0.0.1\nsha256={hashlib.sha256(VECTOR_FILE.read_bytes()).hexdigest()}\nsource=x@abc1234\n")
            with mock.patch.object(conformance, "PIN", pin):
                self.assertFalse(conformance.pin_status()["intact"])


class AdapterRoutesToProduction(unittest.TestCase):
    """Swap each production function for a sentinel: if the adapter re-implemented the rule, it would not notice."""

    def test_grounding(self):
        with mock.patch.object(grounding, "ground", return_value={"ok": False, "ungrounded": ["SENTINEL"]}) as m:
            got = conformance.ADAPTERS["grounding"]({"text": "t", "allowed": ["a"]})
        self.assertEqual(got["ungrounded"], ["SENTINEL"])
        m.assert_called_once_with("t", ["a"])

    def test_classify(self):
        with mock.patch.object(policy, "classify", return_value={"risk": "SENTINEL"}):
            self.assertEqual(conformance.ADAPTERS["classify"]({"tool": "t"}), {"risk": "SENTINEL"})

    def test_approval(self):
        with mock.patch.object(policy, "decide", return_value={"decision": "SENTINEL"}):
            got = conformance.ADAPTERS["approval"]({"tool": "t", "target": "x", "risk": "WRITE", "live": True,
                                                    "approvals": [], "content_ok": True, "harness": False})
        self.assertEqual(got, {"decision": "SENTINEL"})

    def test_budget(self):
        fake = mock.MagicMock()
        fake.return_value.check.return_value = "SENTINEL"
        with mock.patch.object(policy, "Budget", fake):
            got = conformance.ADAPTERS["budget"]({"limits": {"calls": 1}, "calls": [{"risk": "READ", "valid": True}]})
        self.assertEqual(got, {"results": ["SENTINEL"]})
        fake.assert_called_once_with({"calls": 1})

    def test_degrade(self):
        with mock.patch.object(policy, "vendor_readiness", return_value={"status": "SENTINEL", "value": None}):
            self.assertEqual(conformance.ADAPTERS["degrade"]({"credential_present": False})["status"], "SENTINEL")

    def test_cap(self):
        with mock.patch.object(scoring, "admit", return_value=[{"id": "SENTINEL"}]):
            got = conformance.ADAPTERS["cap"]({"items": [], "floor": 0, "cap": 1})
        self.assertEqual(got, {"admitted": ["SENTINEL"]})

    def test_age(self):
        with mock.patch.object(scoring, "age_days", return_value=12345):
            self.assertEqual(conformance.ADAPTERS["age"]({"observed": "2026-01-01", "as_of": "2026-01-02"}), {"age_days": 12345})

    def test_evidence(self):
        with mock.patch.object(schemas, "check_evidence", side_effect=schemas.GateError("SENTINEL", "x")):
            got = conformance.ADAPTERS["evidence"]({"evidence": {"status": "VERIFIED"}, "as_of": "2026-01-01"})
        self.assertEqual(got, {"ok": False, "code": "SENTINEL"})
        with mock.patch.object(schemas, "check_evidence", return_value=None):
            self.assertEqual(conformance.ADAPTERS["evidence"]({"evidence": {}, "as_of": "2026-01-01"}), {"ok": True})

    def test_judgment(self):
        payload = {"entity": "acme.com", "scores": {"d1": 1}, "rationale": "r"}
        with mock.patch.object(schemas, "check_judgment", side_effect=schemas.GateError("SENTINEL", "x")):
            self.assertEqual(conformance.ADAPTERS["judgment"]({"payload": payload}), {"ok": False, "code": "SENTINEL"})
        with mock.patch.object(schemas, "check_judgment", return_value=None):
            self.assertEqual(conformance.ADAPTERS["judgment"]({"payload": payload}), {"ok": True})

    def test_judgment_maps_canonical_dimensions_positionally_and_passes_other_keys_through(self):
        seen = {}
        scores = {"d10": 5, "d2": 4, "d1": 3, "d11": 2, "d12": 1, "d13": 0}
        with mock.patch.object(schemas, "check_judgment", side_effect=lambda p: seen.update(p)):
            conformance.ADAPTERS["judgment"]({"payload": {"entity": "acme.com", "scores": scores,
                                                          "rationale": "r", "zz_extra": 1}})
        dims = schemas.JUDGMENT_DIMS
        self.assertEqual(seen["carrier"], "acme.com")
        # Numeric order d1, d2, d10, d11, d12 (string order would put d10 second), then d13 is a sixth key:
        # no dimension left for it, so it stays under its own name for S1 to reject.
        self.assertEqual([seen["scores"][d] for d in dims], [3, 4, 5, 2, 1])
        self.assertEqual(seen["scores"]["d13"], 0)
        self.assertEqual(seen["zz_extra"], 1)


class VectorsBiteProductionCode(unittest.TestCase):
    """Break a production function and the invariant's vectors must go red. Without this, a green run proves nothing."""

    def assert_breaks(self, target, name, replacement, invariant):
        with mock.patch.object(target, name, replacement):
            r = conformance.run()
        self.assertFalse(r["ok"], f"{name} broken but conformance still ok")
        self.assertIn(invariant, {f["invariant"] for f in r["failed"]}, f"{name} broken: {invariant} did not fail")

    def test_grounding(self):
        self.assert_breaks(grounding, "ground", lambda text, allowed: {"ok": True, "ungrounded": []}, "GROUNDING")

    def test_classification(self):
        self.assert_breaks(policy, "classify", lambda tool: {"risk": "READ"}, "FAIL_CLOSED")

    def test_scoped_approval(self):
        self.assert_breaks(policy, "decide", lambda *a, **k: {"decision": "allow"}, "SCOPED_APPROVAL")

    def test_caps(self):
        self.assert_breaks(scoring, "admit", lambda items, floor, cap: sorted(items, key=lambda i: i["id"])[:cap], "CAPS")

    def test_evidence(self):
        self.assert_breaks(schemas, "check_evidence", lambda ev, as_of, path="$": ev, "EVIDENCE")

    def test_derived_age(self):
        self.assert_breaks(scoring, "age_days", lambda observed, as_of: 0, "DERIVED_AGE")

    def test_honest_degradation(self):
        self.assert_breaks(policy, "vendor_readiness", lambda entries, vendor: {"status": "connected", "value": 0}, "HONEST_DEGRADATION")

    def test_budget(self):
        class Lax:
            def __init__(self, limits):
                pass

            def check(self, risk, valid=True):
                return "OK"
        self.assert_breaks(policy, "Budget", Lax, "BUDGET")

    def test_strict_schema_and_self_grading(self):
        with mock.patch.object(schemas, "check_judgment", lambda payload, path="$": payload):
            r = conformance.run()
        failed = {f["invariant"] for f in r["failed"]}
        self.assertLessEqual({"STRICT_SCHEMA", "NO_SELF_GRADING"}, failed)


if __name__ == "__main__":
    unittest.main()
