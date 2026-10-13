"""Each CSR worker against the sample account: the insurance rules, the if/else inside each desk, and the
honest-degradation paths."""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from helpers import AS_OF, FakeNet, ctx, edit, ticket

from csrdesk import (acord, claimsdesk, decpages, exclusions, intake, lossruns, market, pii, publicdata, renewals,
                     scoring, service, thresholds)


def run(fn, t, state=None, **kw):
    c = ctx(t, state, **kw)
    return fn(c), c


class Intake(unittest.TestCase):
    def test_missing_driver_dob_blocks_auto_only(self):
        r, _ = run(intake.run, ticket("quote.json"))
        self.assertEqual(r["findings"]["blocking_lines"], ["AUTO"])
        self.assertEqual(r["findings"]["ready_lines"], ["GL", "UMBRELLA"])
        self.assertIn("drivers[1].dob", [m["field"] for m in r["findings"]["missing"]])
        self.assertIn("ACORD 129 Vehicle Schedule", r["findings"]["acord_forms"])

    def test_missing_common_field_blocks_every_line(self):
        t = edit(ticket("quote.json"), lambda t: t["account"].update(entity_type=None))
        r, _ = run(intake.run, t)
        self.assertEqual(r["findings"]["ready_lines"], [])

    def test_umbrella_without_underlying_auto_is_blocked(self):
        def f(t):
            t["request"]["lines"] = ["GL", "UMBRELLA"]
            t["account"]["prior_policies"] = [p for p in t["account"]["prior_policies"] if p["line"] != "AUTO"]
        r, _ = run(intake.run, edit(ticket("quote.json"), f))
        self.assertIn("underlying.AUTO", [m["field"] for m in r["findings"]["missing"]])
        self.assertIn("UMBRELLA", r["findings"]["blocking_lines"])


class LossRuns(unittest.TestCase):
    def test_stale_and_current_counted_in_policy_terms(self):
        r, _ = run(lossruns.run, ticket("quote.json"))
        per = r["findings"]["per_line"]
        self.assertEqual(per["GL"]["status"], "stale")            # valued 2026-06-30: 104 days > 90
        self.assertEqual(per["AUTO"]["status"], "current")        # five terms back to 2021-11-20
        self.assertEqual(per["UMBRELLA"]["status"], "new_line")
        self.assertEqual(len(r["drafts"]), 1)                     # only the stale GL carrier gets a request

    def test_loss_ratio_is_none_without_premium_for_every_term(self):
        r, _ = run(lossruns.run, ticket("quote.json"))
        self.assertIsNone(r["findings"]["loss_summary"]["AUTO"]["loss_ratio"])   # one term of premium, five of losses
        self.assertEqual(r["findings"]["loss_summary"]["AUTO"]["incurred"], 31200.0)
        self.assertEqual(len(r["findings"]["large_losses"]), 1)

    def test_loss_ratio_computed_when_premium_covers_window(self):
        def f(t):
            hist = [{"line": "AUTO", "carrier": "Example Casualty (fictional)", "policy_number": f"ECBA-{y}",
                     "effective": f"{y}-11-20", "expiration": f"{y + 1}-11-20", "premium": 20000,
                     "renewal_rate_change_pct": None, "nonrenewal_notice": False, "dec_page": None} for y in range(2021, 2025)]
            t["account"]["prior_policies"] += hist
        r, _ = run(lossruns.run, edit(ticket("quote.json"), f))
        earned = 4 * 20000 + 21600 * 326 / 365            # current term 2025-11-20 -> as of 2026-10-12: 326 of 365 days earned
        self.assertEqual(r["findings"]["loss_summary"]["AUTO"]["loss_ratio"], round(31200 / earned, 3))

    def test_new_venture_orders_nothing(self):
        def f(t):
            t["account"].update(years_in_business=0, prior_policies=[], loss_runs=[])
        r, _ = run(lossruns.run, edit(ticket("quote.json"), f))
        self.assertTrue(all(v["status"] == "new_venture" for v in r["findings"]["per_line"].values()))
        self.assertEqual(r["drafts"], [])


class Verify(unittest.TestCase):
    def test_vins_decode_and_typo_and_straddle_become_client_questions(self):
        net = FakeNet()
        r, _ = run(service.verify, ticket("quote.json"), fetch=net)
        rows = {v["vin"]: v for v in r["findings"]["vehicles"]}
        self.assertEqual(rows["1FTFW1E50NFA12346"]["status"], "confirm_with_client")      # bad check digit
        self.assertFalse(any("1FTFW1E50NFA12346" in u for u in net.calls))                 # never sent
        self.assertEqual(rows["1FTFW1E55NFA12345"]["value"]["size_class"], "light")
        self.assertIsNone(rows["1FVACWDT5HHJ12345"]["value"]["size_class"])                # 19,501-26,000 straddles 20,000
        self.assertEqual(rows["1FVACWDT5HHJ12345"]["value"]["candidates"], ["medium", "heavy"])
        self.assertEqual(len(r["findings"]["client_asks"]), 2)
        self.assertEqual(rows["1FTFW1E55NFA12345"]["evidence"]["status"], "VERIFIED")
        self.assertEqual(r["findings"]["addresses"][0]["value"]["county"], "Bergen County")

    def test_network_down_is_unavailable_not_empty(self):
        r, _ = run(service.verify, ticket("quote.json"), fetch=FakeNet(fail=True), config={"sleep": lambda s: None})
        rows = [v for v in r["findings"]["vehicles"] if v["status"] != "confirm_with_client"]
        self.assertTrue(rows and all(v["status"] == "unavailable" and v["value"] is None for v in rows))
        self.assertEqual(r["findings"]["addresses"][0]["status"], "unavailable")

    def test_vin_check_digit(self):
        self.assertTrue(publicdata.vin_check("1FTFW1E55NFA12345")["ok"])
        self.assertFalse(publicdata.vin_check("1FTFW1E55NFA1234")["ok"])
        self.assertFalse(publicdata.vin_check("1FTFW1E55NFA1234O")["ok"])

    def test_stated_gvw_resolves_the_class(self):
        self.assertEqual(scoring.size_class("Class 6: 19,501 - 26,000 lb", 19800)["size_class"], "medium")


class DecPagesAndExclusions(unittest.TestCase):
    def test_quote_gaps_wos_and_pnc_not_listed(self):
        r, _ = run(decpages.run, ticket("quote.json"))
        kinds = {(g["line"], g.get("requirement")) for g in r["findings"]["gaps"]}
        self.assertIn(("GL", "waiver_of_subrogation"), kinds)
        self.assertIn(("GL", "primary_noncontributory"), kinds)
        self.assertNotIn(("GL", "additional_insured"), kinds)      # CG 20 10 is listed

    def test_limit_shortfall_closed_by_umbrella_is_medium(self):
        def f(t):
            t["account"]["prior_policies"].append({"line": "UMBRELLA", "carrier": "Sample Mutual (fictional)", "policy_number": "SMUM-1",
                                                   "effective": "2025-11-15", "expiration": "2026-11-15", "premium": 3000,
                                                   "renewal_rate_change_pct": None, "nonrenewal_notice": False,
                                                   "dec_page": {"limits": {"each_occurrence": 2000000}}})
        r, _ = run(decpages.run, edit(ticket("coi_wos.json"), f))
        short = next(g for g in r["findings"]["gaps"] if g["kind"] == "limit_shortfall")
        self.assertEqual(short["severity"], "HIGH")                 # umbrella exists but is not on the certificate
        both = edit(ticket("coi_wos.json"), lambda t: (f(t), t["request"]["lines"].append("UMBRELLA")))
        r, _ = run(decpages.run, both)
        short = next(g for g in r["findings"]["gaps"] if g["kind"] == "limit_shortfall")
        self.assertEqual(short["severity"], "MEDIUM")

    def test_missing_dec_page_is_needs_data(self):
        r, _ = run(decpages.run, edit(ticket("coi.json"), lambda t: t["account"]["prior_policies"][0].update(dec_page=None)))
        self.assertEqual(r["status"], "needs_data")

    def test_landscaper_prompts(self):
        r, _ = run(exclusions.run, ticket("quote.json"))
        names = {f["exclusion"]: f for f in r["findings"]["flags"]}
        self.assertEqual(names["snow and ice removal"]["status"], "listed")
        self.assertEqual(names["pesticide / herbicide application (pollution)"]["status"], "listed")
        self.assertNotIn("assault and battery", names)

    def test_residential_coi_with_residential_exclusion_is_critical(self):
        def f(t):
            t["request"]["project_type"] = "residential"
            t["account"]["description_of_operations"] = "General contractor, renovations"
            t["account"]["prior_policies"][0]["dec_page"]["exclusions"].append("Residential construction exclusion")
        r, _ = run(exclusions.run, edit(ticket("coi.json"), f))
        self.assertEqual(r["status"], "escalate")


class Coi(unittest.TestCase):
    def _state(self, t):
        c = ctx(t)
        st = {"CSR9:dec_review": decpages.run(c)}
        st["CSR10:exclusions"] = exclusions.run(ctx(t, st))
        return st

    def test_passes_and_marks_ai_only_where_listed(self):
        t = ticket("coi.json")
        r, _ = run(acord.coi, t, self._state(t))
        self.assertEqual(r["status"], "done")
        cov = r["findings"]["acord25"]["coverages"][0]
        self.assertEqual((cov["addl_insd"], cov["subr_wvd"]), ("Y", "N"))

    def test_wos_and_higher_limit_escalate(self):
        t = ticket("coi_wos.json")
        r, _ = run(acord.coi, t, self._state(t))
        self.assertEqual((r["status"], r["findings"]["gate"]), ("escalate", "requirement_not_supported"))

    def test_no_policy_in_force_escalates_first(self):
        t = ticket("coi.json")
        r, _ = run(acord.coi, t, self._state(t), as_of="2027-01-05")
        self.assertEqual(r["findings"]["gate"], "no_policy_in_force")


class Market(unittest.TestCase):
    def _appetite(self, rows):
        f = Path(tempfile.mkdtemp()) / "appetite.csv"
        with f.open("w", newline="") as fh:
            w = csv.DictWriter(fh, ["carrier", "line", "states", "naics_prefixes", "min_revenue", "max_revenue", "am_best",
                                    "appetite_url", "observed", "notes"])
            w.writeheader()
            w.writerows(rows)
        return str(f)

    def _row(self, carrier, **kw):
        base = {"carrier": carrier, "line": "GL", "states": "NJ|NY", "naics_prefixes": "5617", "min_revenue": "",
                "max_revenue": "5000000", "am_best": "A", "appetite_url": "https://example.com/appetite", "observed": "2026-06-01", "notes": ""}
        return {**base, **kw}

    def _judge(self, carrier, n):
        return {"carrier": carrier, "scores": {d: n for d in ("appetite_fit", "coverage_breadth", "loss_history_fit", "program_fit", "service_fit")},
                "rationale": "test"}

    def test_no_appetite_file_is_needs_key_not_a_list(self):
        r, _ = run(market.run, ticket("quote.json"), config={"appetite": "/nonexistent/appetite.csv"})
        self.assertEqual((r["findings"]["status"], r["findings"]["value"]), ("needs_key", None))

    def test_hard_filters_drop_with_reasons(self):
        path = self._appetite([self._row("Keep"), self._row("WrongState", states="PA"), self._row("LowBest", am_best="B+"),
                               self._row("Stale", observed="2024-01-01"), self._row("TooBig", min_revenue="5000000")])
        kept, dropped = market.filter_markets(market.load_appetite(path), "GL", "NJ", "561730", 1850000, AS_OF)
        self.assertEqual([k["carrier"] for k in kept], ["Keep"])
        self.assertEqual(len(dropped), 4)

    def test_scored_floor_and_cap(self):
        rows = [self._row(f"C{i}") for i in range(7)]
        judgments = [self._judge(f"C{i}", 5 if i < 6 else 2) for i in range(7)]   # C6 totals 40.0 < floor
        r, _ = run(market.run, ticket("coi.json") | {"type": "QUOTE", "request": {"lines": ["GL"], "effective_date": "2026-11-15", "contract_requirements": None}},
                   config={"appetite": self._appetite(rows), "judgments": judgments})
        ranked = r["findings"]["by_line"]["GL"]["ranked"]
        self.assertEqual(len(ranked), thresholds.MARKET_CAP)
        self.assertNotIn("C6", [x["carrier"] for x in ranked])

    def test_self_graded_judgment_file_is_rejected(self):
        import json
        f = Path(tempfile.mkdtemp()) / "j.json"
        f.write_text(json.dumps([{**self._judge("C0", 4), "total": 99}]))
        with self.assertRaises(Exception) as e:
            market.load_judgments(f)
        self.assertEqual(e.exception.code, "SELF_GRADED")


class RenewalsAndClaims(unittest.TestCase):
    def test_45_day_notice_goes_to_csr5(self):
        r, _ = run(renewals.run, ticket("renewal.json"))
        self.assertEqual(r["findings"]["days_to_expiration"], 39)
        self.assertTrue(any(m["to"] == "CSR5" for m in r["messages"]))

    def test_no_notice_at_60_days(self):
        r, _ = run(renewals.run, ticket("renewal.json"), as_of="2026-09-21")
        self.assertEqual(r["findings"]["days_to_expiration"], 60)
        self.assertFalse(any(m["to"] == "CSR5" for m in r["messages"]))

    def test_lapsed_escalates(self):
        r, _ = run(renewals.run, ticket("renewal.json"), as_of="2026-11-25")
        self.assertEqual(r["status"], "escalate")

    def test_scan_is_sorted_and_flags_notice(self):
        rows = renewals.scan([ticket("quote.json")["account"]], AS_OF)
        self.assertEqual([r["policy_number"] for r in rows], ["SMGL-2025-0187", "ECBA-26-55120"])
        self.assertTrue(all(r["client_notice_due"] for r in rows))

    def test_injury_claim_is_urgent_and_never_filed(self):
        r, _ = run(claimsdesk.run, ticket("claim.json"))
        self.assertEqual((r["status"], r["findings"]["urgency"]), ("escalate", "URGENT"))
        self.assertEqual(r["drafts"][0]["release_tool"], "file_claim")
        self.assertEqual({m["to"] for m in r["messages"]}, {"CSR6", "CSR2", "CSR8"})

    def test_late_notice_and_no_policy(self):
        late = edit(ticket("claim.json"), lambda t: t["request"].update(injury=False, estimated_amount=4000, date_of_loss="2026-08-01"))
        r, _ = run(claimsdesk.run, late)
        self.assertEqual(r["findings"]["urgency"], "LATE_NOTICE")
        none = edit(ticket("claim.json"), lambda t: t["request"].update(date_of_loss="2024-01-01"))
        r, _ = run(claimsdesk.run, none)
        self.assertEqual(r["findings"]["urgency"], "COVERAGE_QUESTION")


class Pii(unittest.TestCase):
    def test_masking(self):
        m = pii.masked_ticket(ticket("quote.json"))
        self.assertEqual(m["account"]["fein"], "••-•••6789")
        self.assertTrue(m["account"]["drivers"][0]["dl_number"].endswith("1234"))
        self.assertNotIn("S12345678901234", str(m))
        self.assertEqual(m["account"]["drivers"][0]["dob"], "1984-••-••")


if __name__ == "__main__":
    unittest.main()
