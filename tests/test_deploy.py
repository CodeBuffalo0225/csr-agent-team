"""Deployment features: market onboarding (carriers + wholesalers), the admitted-first fork, researched state
rules with confirmation, and the Applied Epic / HawkSoft / EZLynx adapters."""
from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import AS_OF, NOW, FakeNet, ctx, isolated_config, ticket

from csrdesk import ams, market, markets, orchestrator, staterules


def tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="csrdesk-deploy-"))


def write_csv(path: Path, cols, rows) -> str:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, list(cols))
        w.writeheader()
        w.writerows(rows)
    return str(path)


MARKETS = [
    {"market": "Admitted Co (fictional)", "kind": "carrier", "admitted": "Y", "appointed": "Y", "lines": "GL|AUTO", "states": "NJ|NY"},
    {"market": "Not Appointed Co (fictional)", "kind": "carrier", "admitted": "Y", "appointed": "N", "lines": "*", "states": "*"},
    {"market": "Wholesale Partner (fictional)", "kind": "wholesaler", "admitted": "N", "appointed": "Y", "lines": "*", "states": "NJ"},
]


def appetite_row(carrier, line="GL", confirmed="Ops Manager"):
    return {"carrier": carrier, "line": line, "states": "NJ", "naics_prefixes": "5617", "max_revenue": "5000000", "am_best": "A",
            "appetite_url": "https://example.com/appetite", "observed": "2026-09-01", "confirmed_by": confirmed}


class Onboarding(unittest.TestCase):
    def test_validate_and_research_only_appointed(self):
        self.assertEqual(markets.validate_markets(MARKETS), [])
        bad = [{"market": "X", "kind": "insurer", "admitted": "maybe", "appointed": "Y", "lines": "GLX", "states": "Jersey"}]
        self.assertGreaterEqual(len(markets.validate_markets(bad)), 4)
        names = [t["market"] for t in markets.research_tasks(MARKETS)]
        self.assertEqual(names, ["Admitted Co (fictional)", "Wholesale Partner (fictional)"])

    def test_propose_then_confirm(self):
        d = tmp()
        prop, app = d / "proposed.csv", d / "appetite.csv"
        r = markets.propose([appetite_row("Admitted Co (fictional)", confirmed=""),
                             appetite_row("Not Appointed Co (fictional)", confirmed=""),
                             appetite_row("Admitted Co (fictional)", confirmed="the subagent")], MARKETS, AS_OF, proposed_path=prop)
        self.assertEqual((r["accepted"], r["rejected"]), (1, 2))
        self.assertIsNone(market.load_appetite(str(app)))                          # proposed is not live
        c = markets.confirm("Admitted Co (fictional)", "Ops Manager", proposed_path=prop, appetite_path=app,
                            markets_rows=MARKETS, as_of=AS_OF, attest=lambda action, expected, by=None: by or "Test Person")
        self.assertEqual(c["confirmed_rows"], 1)
        self.assertEqual(market.load_appetite(str(app))[0]["confirmed_by"], "Ops Manager")
        with self.assertRaises(ValueError):                         # nothing left to confirm
            markets.confirm("Admitted Co (fictional)", "x", proposed_path=prop, appetite_path=app, markets_rows=MARKETS, attest=lambda action, expected, by=None: by or "Test Person")
        with self.assertRaises(PermissionError):                    # no terminal: an agent cannot confirm
            markets.propose([appetite_row("Admitted Co (fictional)", confirmed="")], MARKETS, AS_OF, proposed_path=prop)
            markets.confirm("Admitted Co (fictional)", "Ops", proposed_path=prop, appetite_path=app, markets_rows=MARKETS, as_of=AS_OF)


class AdmittedFirst(unittest.TestCase):
    def _cfg(self, appetite_rows):
        d = tmp()
        return {"markets": write_csv(d / "markets.csv", markets.MARKET_COLUMNS, MARKETS),
                "appetite": write_csv(d / "appetite.csv", market.APPETITE_COLUMNS, appetite_rows)}

    def _quote_ctx(self, cfg):
        t = ticket("coi.json") | {"type": "QUOTE", "request": {"lines": ["GL"], "effective_date": "2026-11-15", "contract_requirements": None}}
        return ctx(t, config=cfg)

    def test_unappointed_and_unconfirmed_rows_are_dropped(self):
        cfg = self._cfg([appetite_row("Admitted Co (fictional)"), appetite_row("Not Appointed Co (fictional)"),
                         appetite_row("Admitted Co (fictional)", confirmed="")])
        r = market.run(self._quote_ctx(cfg))
        self.assertEqual(r["findings"]["by_line"]["GL"]["eligible"], ["Admitted Co (fictional)"])
        whys = {d["why"] for d in r["findings"]["dropped"]}
        self.assertIn("agency is not appointed with this market", whys)
        self.assertTrue(any("not confirmed" in w for w in whys))
        self.assertEqual(r["findings"]["wholesale"], {})

    def test_no_admitted_market_opens_the_wholesale_path_with_diligent_effort(self):
        cfg = self._cfg([appetite_row("Not Appointed Co (fictional)")])
        r = market.run(self._quote_ctx(cfg))
        w = r["findings"]["wholesale"]["GL"]
        self.assertEqual(w["wholesalers"], ["Wholesale Partner (fictional)"])
        self.assertEqual(w["diligent_effort"]["value"]["declinations_required"], 3)
        self.assertIn("declinations from authorized insurers first", r["summary"])


class ResearchedRules(unittest.TestCase):
    def test_shipped_nj_ny_rules_are_cited_and_unconfirmed(self):
        nj = staterules.rule_for("NJ", "GL", AS_OF)
        ny = staterules.rule_for("NY", "UMBRELLA", AS_OF)
        self.assertEqual((nj["status"], nj["value"]["nonrenewal_notice_days"], nj["value"]["cancel_nonpay_notice_days"]), ("unconfirmed", 30, 10))
        self.assertEqual((ny["value"]["nonrenewal_notice_days"], staterules.rule_for("NY", "GL", AS_OF)["value"]["nonrenewal_notice_days"]), (30, 60))
        self.assertTrue(nj["evidence"]["url"].startswith("https://"))
        self.assertEqual(staterules.rule_for("NJ", "WC", AS_OF)["status"], "needs_data")   # WC rules differ; not loaded
        self.assertEqual(staterules.surplus_rule("NY", AS_OF)["value"]["declinations_required"], 3)
        self.assertEqual(staterules.surplus_rule("PA", AS_OF)["value"]["declinations_required"], 3)
        self.assertEqual(staterules.surplus_rule("CT", AS_OF)["status"], "needs_data")   # CT Public Act 26-69 change unconfirmed: not loaded
        self.assertEqual(staterules.rule_for("PA", "GL", AS_OF)["status"], "needs_data")  # statute text not verifiable: not loaded
        ct = staterules.rule_for("CT", "PROFESSIONAL", AS_OF)["value"]
        self.assertEqual((ct["nonrenewal_notice_days"], ct["cancel_nonpay_notice_days"]), (90, 10))

    def test_unconfirmed_rule_shows_as_confirm_item_and_window_fact(self):
        run = orchestrator.advance(orchestrator.open_ticket(ticket("renewal.json")), as_of="2026-10-25", now=NOW,
                                   config=isolated_config(), fetch=FakeNet())
        self.assertTrue(any(a.startswith("Confirm (CSR6:renewal)") for a in run["packet"]["action_items"]))
        self.assertIn("non-renewal notice 30 days", run["steps"]["CSR6:renewal"]["summary"])
        self.assertIn("26 days left and no non-renewal notice on file", run["steps"]["CSR6:renewal"]["summary"])

    def test_confirmed_rule_is_ok_and_a_typed_name_is_not(self):
        d = tmp()
        with mock.patch.dict(os.environ, {"CSR_DESK_HOME": str(d)}):
            rows = staterules.load()
            for r in rows:
                r["confirmed_by"] = "Compliance"                    # typing a name into the CSV confirms nothing
            self.assertEqual(staterules.rule_for("NJ", "GL", AS_OF, rows)["status"], "unconfirmed")
            staterules.confirm("NJ", "Lee Compliance", attest=lambda action, expected, by=None: by or "Test Person")
            self.assertEqual(staterules.rule_for("NJ", "GL", AS_OF)["status"], "ok")
            with open(d / "state_rules.csv", "w") as f:              # the agency changes a NJ row: the sign-off lapses
                w = csv.DictWriter(f, list(rows[0].keys()))
                w.writeheader()
                w.writerow(dict(rows[0], nonrenewal_notice_days="45", confirmed_by=""))
            r = staterules.rule_for("NJ", rows[0]["line"], AS_OF)
            self.assertEqual(r["status"], "unconfirmed")
            self.assertIn("changed since compliance confirmed", r["why"])


class AmsAdapters(unittest.TestCase):
    def _run(self):
        return orchestrator.advance(orchestrator.open_ticket(ticket("coi.json")), as_of=AS_OF, now=NOW,
                                    config=isolated_config(), fetch=FakeNet())

    def test_preview_per_system_is_masked(self):
        run = self._run()
        for system in ams.SYSTEMS:
            p = ams.preview(run, system)
            blob = json.dumps(p)
            self.assertIn("SAMPLE Meadowlands Lawn & Snow LLC", blob)
            self.assertNotIn("12-3456789", blob)
        self.assertIn("activity", ams.preview(run, "applied_epic"))
        self.assertIn("log note", ams.preview(run, "hawksoft"))

    def test_status_names_what_is_missing(self):
        st = ams.connector_status(config_path=str(tmp() / "none.json"))
        self.assertEqual(st["status"], "needs_key")
        self.assertIn("Applied Epic (primary)", st["supported"].values())

    def test_push_needs_config_token_and_live(self):
        d = tmp()
        cfgp = d / "ams.json"
        cfgp.write_text(json.dumps({"system": "applied_epic", "base_url": "https://ams.example.invalid", "token_env": "CSR_DESK_TEST_EPIC",
                                    "paths": {"note": "/configured/by/agency"}}))
        run = self._run()
        self.assertEqual(ams.push(run, live=True, config_path=str(cfgp))["status"], "needs_key")   # token env absent
        sent = []
        with mock.patch.dict(os.environ, {"CSR_DESK_TEST_EPIC": "t", "CSR_DESK_HOME": str(d)}):
            r = ams.push(run, live=False, config_path=str(cfgp), transport=lambda u, p, t: sent.append(u) or {"http_status": 200})
            self.assertEqual((r["results"][0]["code"], sent), ("DRY_RUN", []))                   # not live: nothing sent
            r = ams.push(run, live=True, config_path=str(cfgp), transport=lambda u, p, t: sent.append(u) or {"http_status": 200})
            self.assertEqual(r["status"], "pushed")
            self.assertEqual(sent, ["https://ams.example.invalid/configured/by/agency"])
            self.assertEqual(ams.push(run, live=True, config_path=str(cfgp), transport=lambda u, p, t: sent.append(u))["status"],
                             "already_pushed")                     # idempotent: the same note is not pushed twice
            self.assertEqual(ams.connector_status(config_path=str(cfgp))["status"], "connected")
            (d / "runtime" / "STOP").write_text("x")
            self.assertEqual(ams.push(run, live=True, config_path=str(cfgp))["code"], "KILL_SWITCH")

    def test_config_cannot_aim_at_other_secrets_or_plain_http(self):
        cfgp = tmp() / "ams.json"
        cfgp.write_text(json.dumps({"system": "hawksoft", "base_url": "http://evil.example", "token_env": "ANTHROPIC_API_KEY",
                                    "paths": {"note": "/n"}}))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret"}):
            miss = ams.connector_status(config_path=str(cfgp))["missing"]
        self.assertTrue(any("https" in m for m in miss) and any("CSR_DESK_" in m for m in miss))


if __name__ == "__main__":
    unittest.main()
