"""The operations layer: CSR 11 policy checking, state notice rules, appetite validation, AMS export, replay."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from helpers import AS_OF, NOW, ROOT, FakeNet, ctx, edit, isolated_config, ticket

from csrdesk import ams, market, orchestrator, policycheck, renewals, replay, staterules, store


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="csrdesk-ops-"))


class PolicyCheck(unittest.TestCase):
    def test_every_planted_mismatch_is_found(self):
        r = policycheck.run(ctx(ticket("policy_check.json")))
        fields = {m["field"] for m in r["findings"]["mismatches"]}
        self.assertEqual(fields, {"limit.personal_advertising_injury", "deductible.bodily_injury_property_damage",
                                  "form", "exclusion", "premium"})
        self.assertEqual(r["status"], "escalate")

    def test_a_matching_policy_is_clean(self):
        def f(t):
            pol = t["account"]["prior_policies"][-1]
            pr = t["request"]["proposal"]
            pol["premium"] = pr["premium"]
            pol["dec_page"].update(limits=dict(pr["limits"]), deductibles=dict(pr["deductibles"]),
                                   forms=list(pr["forms"]), exclusions=list(pr["exclusions"]))
        r = policycheck.run(ctx(edit(ticket("policy_check.json"), f)))
        self.assertEqual((r["status"], r["findings"]["mismatches"]), ("done", []))

    def test_better_for_client_is_low_and_no_dec_is_needs_data(self):
        r = policycheck.run(ctx(edit(ticket("policy_check.json"),
                                     lambda t: t["request"]["proposal"]["limits"].update(each_occurrence=500000))))
        low = [m for m in r["findings"]["mismatches"] if m["field"] == "limit.each_occurrence"]
        self.assertEqual(low[0]["severity"], "LOW")
        r = policycheck.run(ctx(edit(ticket("policy_check.json"), lambda t: t["account"]["prior_policies"][-1].update(dec_page=None))))
        self.assertEqual(r["status"], "needs_data")

    def test_routes_and_escalates(self):
        run = orchestrator.advance(orchestrator.open_ticket(ticket("policy_check.json")), as_of=AS_OF, now=NOW,
                                   config=isolated_config(), fetch=FakeNet())
        self.assertEqual(run["trail"], ["CSR11:policy_check", "CSR10:exclusions", "CSR8:record"])
        self.assertEqual(run["packet"]["escalations"][0]["step"], "CSR11:policy_check")


class StateRules(unittest.TestCase):
    def _rules(self, rows):
        f = tmpdir() / "state_rules.csv"
        with f.open("w", newline="") as fh:
            w = csv.DictWriter(fh, ["state", "line", "nonrenewal_notice_days", "cancel_nonpay_notice_days",
                                    "cancel_other_notice_days", "source_url", "verified", "notes"])
            w.writeheader()
            w.writerows(rows)
        return str(f)

    def test_no_rules_is_needs_data_and_shows_in_the_packet(self):
        run = orchestrator.advance(orchestrator.open_ticket(ticket("renewal.json")), as_of=AS_OF, now=NOW,
                                   config={**isolated_config(), "state_rules": str(tmpdir() / "none.csv")}, fetch=FakeNet())
        rule = run["steps"]["CSR6:renewal"]["findings"]["state_notice_rule"]
        self.assertEqual((rule["status"], rule["value"]), ("needs_data", None))
        self.assertTrue(any(a.startswith("Data gap") for a in run["packet"]["action_items"]))

    def test_verified_line_row_beats_star_and_unverified_rows_are_ignored(self):
        path = self._rules([
            {"state": "NJ", "line": "*", "nonrenewal_notice_days": "30", "source_url": "https://example.gov/a", "verified": "2026-09-01"},
            {"state": "NJ", "line": "AUTO", "nonrenewal_notice_days": "60", "source_url": "https://example.gov/b", "verified": "2026-09-01"},
            {"state": "NY", "line": "*", "nonrenewal_notice_days": "45", "source_url": "", "verified": ""},
        ])
        rows = staterules.load(path)
        self.assertEqual(staterules.rule_for("NJ", "AUTO", AS_OF, rows)["value"]["nonrenewal_notice_days"], 60)
        self.assertEqual(staterules.rule_for("NJ", "GL", AS_OF, rows)["value"]["nonrenewal_notice_days"], 30)
        self.assertEqual(staterules.rule_for("NY", "GL", AS_OF, rows)["status"], "needs_data")
        self.assertEqual(len(staterules.validate(rows, AS_OF)), 1)
        c = ctx(ticket("renewal.json"), config={"state_rules": path})
        self.assertIn("non-renewal notice 60 days", renewals.run(c)["summary"])

    def test_stale_rule_is_flagged(self):
        rows = staterules.load(self._rules([{"state": "NJ", "line": "*", "nonrenewal_notice_days": "30",
                                             "source_url": "https://example.gov/a", "verified": "2024-01-01"}]))
        self.assertEqual(staterules.rule_for("NJ", "GL", AS_OF, rows)["status"], "stale")


class Appetite(unittest.TestCase):
    def test_validator(self):
        f = tmpdir() / "appetite.csv"
        with f.open("w", newline="") as fh:
            w = csv.DictWriter(fh, market.APPETITE_COLUMNS)
            w.writeheader()
            w.writerow({"carrier": "Good", "line": "GL", "states": "NJ|NY", "naics_prefixes": "5617", "am_best": "A",
                        "appetite_url": "https://example.com/g", "observed": "2026-06-01", "confirmed_by": "Test Confirmer"})
            w.writerow({"carrier": "Bad", "line": "GLX", "states": "Jersey", "naics_prefixes": "", "min_revenue": "lots",
                        "am_best": "AAA", "appetite_url": "", "observed": ""})
        r = market.validate_appetite(str(f), AS_OF)
        self.assertFalse(r["ok"])
        self.assertTrue(all("row 3" in e for e in r["errors"]))
        self.assertGreaterEqual(len(r["errors"]), 5)

    def test_template_is_empty_not_ok(self):
        r = market.validate_appetite(str(ROOT / "data" / "appetite.example.csv"), AS_OF)
        self.assertEqual((r["ok"], r["status"]), (False, "empty"))


class AmsAndReplay(unittest.TestCase):
    def test_connector_is_needs_key_until_config_token_and_a_completed_call(self):
        import os
        from unittest import mock
        cfgp = tmpdir() / "ams.json"
        self.assertEqual(ams.connector_status(config_path=str(cfgp))["status"], "needs_key")
        cfgp.write_text(json.dumps({"system": "hawksoft", "base_url": "https://x.example.invalid", "token_env": "T_HS",
                                    "paths": {"note": "/n"}}))
        with mock.patch.dict(os.environ, {"T_HS": "t"}):
            self.assertEqual(ams.connector_status(config_path=str(cfgp))["status"], "needs_key")      # no completed call yet
            done = [{"event": "call_ok", "vendor": "ams", "ts": "t"}]
            self.assertEqual(ams.connector_status(done, config_path=str(cfgp))["status"], "connected")

    def test_export_is_masked(self):
        cfg = isolated_config()
        orchestrator.advance(orchestrator.open_ticket(ticket("quote.json")), as_of=AS_OF, now=NOW, config=cfg, fetch=FakeNet())
        s = store.Store(cfg["db"])
        out = tmpdir()
        r = ams.export(s, out)
        s.close()
        self.assertEqual((r["accounts"], r["tickets"]), (1, 1))
        blob = "".join(p.read_text() for p in out.iterdir())
        self.assertIn("••-•••6789", blob)
        self.assertNotIn("12-3456789", blob)

    def test_sample_replay_agrees(self):
        r = replay.run_dir(ROOT / "data" / "replay")
        self.assertEqual((r["disagree"], r["checks_passed"]), (0, r["checks"]))

    def test_replay_reports_a_disagreement(self):
        case = json.loads((ROOT / "data" / "replay" / "05_claim.json").read_text())
        case["expected"] = {"claim_urgency": "ROUTINE"}
        r = replay.run_case(case)
        self.assertFalse(r["agree"])
        self.assertEqual(r["misses"][0]["got"], "URGENT")

    def test_case_without_expected_is_refused(self):
        with self.assertRaises(ValueError):
            replay.run_case(ticket("claim.json"))


if __name__ == "__main__":
    unittest.main()
