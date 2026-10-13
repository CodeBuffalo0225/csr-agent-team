"""One test per defect the 2026-10-13 independent review confirmed, built from the reviewer's failure scenario."""
from __future__ import annotations

import json
import unittest
from datetime import timedelta
from pathlib import Path

from helpers import AS_OF, NOW, FakeNet, ctx, edit, isolated_config, ticket

from csrdesk import (acord, audit, claimsdesk, decpages, exclusions, lossruns, market, orchestrator, pii, policy,
                     renewals, schemas, scoring, service, store)


def go(t, cfg=None, now=NOW):
    cfg = cfg or isolated_config()
    return orchestrator.advance(orchestrator.open_ticket(t), as_of=AS_OF, now=now, config=cfg, fetch=FakeNet()), cfg


def coi_state(t):
    st = {"CSR9:dec_review": decpages.run(ctx(t))}
    st["CSR10:exclusions"] = exclusions.run(ctx(t, st))
    return st


class R1CoiDescription(unittest.TestCase):
    def test_coverage_language_in_the_requested_description_escalates(self):
        t = edit(ticket("coi.json"), lambda t: t["request"].update(
            description="Holder is additional insured on a primary and noncontributory basis with waiver of subrogation; umbrella limit $5,000,000"))
        r = acord.coi(ctx(t, coi_state(t)))
        self.assertEqual((r["status"], r["findings"]["gate"]), ("escalate", "description_asserts_coverage"))

    def test_a_plain_project_description_still_passes(self):
        t = ticket("coi.json")
        self.assertEqual(acord.coi(ctx(t, coi_state(t)))["findings"]["gate"], "passed")


class R2Approvals(unittest.TestCase):
    def test_approvals_only_on_approve_and_only_for_real_drafts(self):
        run, cfg = go(ticket("coi.json"))
        with self.assertRaises(ValueError):
            orchestrator.decide(run, {"ticket_id": "C-2001", "decision": "REVISE", "target": "CSR9", "note": "x",
                                      "approvals": ["issue_coi:C-2001/0"]}, as_of=AS_OF, now=NOW, config=cfg)
        with self.assertRaises(ValueError):
            orchestrator.decide(run, {"ticket_id": "C-2001", "decision": "APPROVE", "target": None, "note": "x",
                                      "approvals": ["issue_coi:C-2001/7"]}, as_of=AS_OF, now=NOW, config=cfg)

    def test_rejected_ticket_never_releases(self):
        run, cfg = go(ticket("coi.json"))
        run = orchestrator.decide(run, {"ticket_id": "C-2001", "decision": "REJECT", "target": None, "note": "no",
                                        "approvals": []}, as_of=AS_OF, now=NOW, config=cfg)
        self.assertEqual(orchestrator.release(run, 0, live=True, config=cfg)["code"], "NOT_APPROVED")


class R3Classifier(unittest.TestCase):
    def test_status_writes_and_certificates_are_not_reads(self):
        for tool in ("set_submission_status", "apply_endorsement_status", "approve_binder_status", "send_status_update",
                     "create_certificate", "create_coi"):
            self.assertEqual(policy.classify(tool)["risk"], "IRREVERSIBLE", tool)
        self.assertNotEqual(policy.classify("update_claim_status")["risk"], "READ")
        self.assertEqual(policy.classify("get_claim_status")["risk"], "READ")


class R4WallClock(unittest.TestCase):
    def test_ae_and_client_wait_does_not_count_against_the_desk(self):
        run, cfg = go(ticket("coi_wos.json"))
        later = NOW + timedelta(hours=5)
        run = orchestrator.decide(run, {"ticket_id": "C-2002", "decision": "REVISE", "target": "CSR9", "note": "again",
                                        "approvals": []}, as_of=AS_OF, now=later, config=cfg, fetch=FakeNet())
        self.assertEqual(run["status"], "AWAITING_AE")
        q, cfg = go(ticket("quote.json"))
        q = orchestrator.refresh(q, ticket("quote_client_reply.json"), as_of=AS_OF, now=NOW + timedelta(days=4), config=cfg, fetch=FakeNet())
        self.assertEqual(q["status"], "AWAITING_AE")


class R5Residential(unittest.TestCase):
    def test_project_type_triggers_the_check_whatever_the_operation_says(self):
        def f(t):
            t["request"]["project_type"] = "residential"
            t["account"]["description_of_operations"] = "Kitchen and bath remodeling and handyman services"
            t["account"]["prior_policies"][0]["dec_page"]["exclusions"].append("Residential construction exclusion")
        t = edit(ticket("coi.json"), f)
        r = exclusions.run(ctx(t))
        self.assertEqual(r["status"], "escalate")


class R6LossRuns(unittest.TestCase):
    def test_established_business_without_priors_is_asked_not_new_venture(self):
        t = edit(ticket("quote.json"), lambda t: t["account"].update(years_in_business=12, prior_policies=[], loss_runs=[]))
        r = lossruns.run(ctx(t))
        self.assertTrue(all(v["status"] == "prior_unknown" for v in r["findings"]["per_line"].values()))
        self.assertTrue(any("Who insured the business" in a for a in service.collect_asks(ctx(t, {"CSR2:loss_runs": r}))))

    def test_fresh_old_carrier_run_does_not_hide_a_stale_current_one(self):
        def f(t):
            runs = t["account"]["loss_runs"]
            gl = next(r for r in runs if r["line"] == "GL")
            gl.update(valued_as_of="2025-01-31", period_start="2024-11-15", period_end="2025-01-31")
            runs.append({"line": "GL", "carrier": "Old Carrier (fictional)", "valued_as_of": "2026-09-30",
                         "period_start": "2021-11-15", "period_end": "2024-11-15", "claims": []})
        r = lossruns.run(ctx(edit(ticket("quote.json"), f)))
        self.assertEqual(r["findings"]["per_line"]["GL"]["status"], "stale")

    def test_old_claims_stay_out_of_the_window_loss_ratio(self):
        def f(t):
            t["account"]["loss_runs"][0]["claims"].append({"date_of_loss": "2017-03-01", "status": "CLOSED", "paid": 40000,
                                                            "reserved": 0, "description": "old loss"})
        r = lossruns.run(ctx(edit(ticket("quote.json"), f)))
        self.assertEqual(r["findings"]["loss_summary"]["GL"]["incurred"], 20600.0)


class R7Asks(unittest.TestCase):
    def test_unchanged_list_at_the_cap_does_not_halt_and_keeps_its_draft(self):
        run, cfg = go(ticket("quote.json"))
        run["client_asks"] = 3
        run = orchestrator.decide(run, {"ticket_id": "Q-1001", "decision": "REVISE", "target": "CSR2", "note": "x",
                                        "approvals": []}, as_of=AS_OF, now=NOW, config=cfg, fetch=FakeNet())
        self.assertEqual(run["status"], "AWAITING_AE")
        self.assertTrue(run["steps"]["CSR5:info_request"]["drafts"])

    def test_revise_with_new_data_reruns_intake(self):
        run, cfg = go(ticket("quote.json"))
        run = orchestrator.decide(run, {"ticket_id": "Q-1001", "decision": "REVISE", "target": "CSR9", "note": "dob in",
                                        "approvals": []}, as_of=AS_OF, now=NOW, config=cfg, fetch=FakeNet(),
                                  updated_raw=ticket("quote_client_reply.json"))
        self.assertEqual(run["steps"]["CSR1:intake"]["findings"]["blocking_lines"], [])


class R8CoiCoverageHoles(unittest.TestCase):
    def test_requirement_on_a_line_not_on_the_certificate_is_a_gap(self):
        t = edit(ticket("coi.json"), lambda t: t["request"]["requirements"]["limits"].update({"AUTO.combined_single_limit": 5000000}))
        self.assertEqual(acord.coi(ctx(t, coi_state(t)))["findings"]["gate"], "requirement_not_supported")

    def test_ai_required_on_an_uncheckable_line_is_a_gap(self):
        def f(t):
            t["request"]["lines"] = ["UMBRELLA"]
            t["request"]["requirements"]["limits"] = {}
            t["account"]["prior_policies"].append({"line": "UMBRELLA", "carrier": "Sample Mutual (fictional)", "policy_number": "U-1",
                                                   "effective": "2025-11-15", "expiration": "2026-11-15", "premium": 3000,
                                                   "renewal_rate_change_pct": None, "nonrenewal_notice": False,
                                                   "dec_page": {"limits": {"each_occurrence": 2000000}}})
        t = edit(ticket("coi.json"), f)
        self.assertEqual(acord.coi(ctx(t, coi_state(t)))["status"], "escalate")


class R9AcordMapping(unittest.TestCase):
    def test_managers_or_lessors_form_is_not_ai_for_a_service_contract(self):
        def f(t):
            dec = t["account"]["prior_policies"][0]["dec_page"]
            dec["forms"] = ["CG 00 01 Commercial General Liability Coverage Form", "CG 20 11 Additional Insured - Managers or Lessors of Premises"]
            dec["endorsements"] = []
        t = edit(ticket("coi.json"), f)
        self.assertEqual(acord.coi(ctx(t, coi_state(t)))["status"], "escalate")

    def test_scheduled_form_alone_needs_the_ae(self):
        t = edit(ticket("coi.json"), lambda t: t["account"]["prior_policies"][0]["dec_page"].update(endorsements=[]))
        st = decpages.endorsement_status(t["account"]["prior_policies"][0]["dec_page"], "additional_insured")
        self.assertEqual(st["status"], "scheduled_verify")
        self.assertEqual(acord.coi(ctx(t, coi_state(t)))["status"], "escalate")

    def test_property_is_not_an_acord_25(self):
        def f(t):
            t["request"]["lines"] = ["PROPERTY"]
            t["request"]["requirements"]["limits"] = {}
            t["request"]["requirements"]["additional_insured"] = False
        t = edit(ticket("coi.json"), f)
        self.assertEqual(acord.coi(ctx(t))["findings"]["gate"], "property_needs_evidence_form")


class R10DocsVsCode(unittest.TestCase):
    def test_db_status_follows_the_decision(self):
        run, cfg = go(ticket("coi.json"))
        run = orchestrator.decide(run, {"ticket_id": "C-2001", "decision": "REJECT", "target": None, "note": "no",
                                        "approvals": []}, as_of=AS_OF, now=NOW, config=cfg)
        s = store.Store(cfg["db"])
        self.assertEqual(s.db.execute("SELECT status FROM tickets WHERE ticket_id='C-2001'").fetchone()[0], "CLOSED_REJECTED")
        s.close()

    def test_record_writes_count_against_the_budget(self):
        run, _ = go(ticket("coi.json"))
        self.assertGreaterEqual(run["budget_used"]["writes"], 1)

    def test_truncation_is_detected_against_the_run_head(self):
        run, cfg = go(ticket("quote.json"))
        log = audit.AuditLog(Path(cfg["audit_dir"]) / "Q-1001.jsonl")
        self.assertTrue(log.verify(expected=run["audit_head"])["ok"])
        lines = log.path.read_text().splitlines()
        log.path.write_text("\n".join(lines[:-3]) + "\n")
        self.assertTrue(log.verify()["ok"])                                     # the chain alone cannot see it
        self.assertFalse(log.verify(expected=run["audit_head"])["ok"])          # the head can


class R11Pii(unittest.TestCase):
    def test_fein_digest_is_keyed(self):
        import hashlib
        self.assertNotEqual(store.fein_hash("123456789"), hashlib.sha256(b"123456789").hexdigest())

    def test_free_text_and_errors_do_not_leak(self):
        t = edit(ticket("claim.json"), lambda t: t["request"].update(
            description="Driver S12345678901234, SSN 123-45-6789, FEIN 12-3456789 slipped"))
        m = pii.masked_ticket(t)
        for raw in ("S12345678901234", "123-45-6789", "12-3456789"):
            self.assertNotIn(raw, json.dumps(m))
        with self.assertRaises(schemas.GateError) as e:
            schemas.parse_date("03/09/1984", "$.account.drivers[0].dob")
        self.assertNotIn("1984", str(e.exception))

    def test_different_feins_with_similar_names_stay_separate(self):
        s = store.Store(isolated_config()["db"])
        acct = ticket("quote.json")["account"]
        a = s.write("CSR8", fein_digest=store.fein_hash("111111111"), masked_account={**acct, "insured_name": "Acme Roofing LLC"},
                    ticket_id="a", ticket_type="QUOTE", status="OPEN", packet={})
        b = s.write("CSR8", fein_digest=store.fein_hash("222222222"), masked_account={**acct, "insured_name": "Acme Roofing Inc"},
                    ticket_id="b", ticket_type="QUOTE", status="OPEN", packet={})
        self.assertNotEqual(a["account_key"], b["account_key"])
        s.close()


class R12to15Cluster(unittest.TestCase):
    def test_acord_127_partial_when_a_driver_is_incomplete_and_masked_fields_flagged(self):
        from csrdesk import intake
        t = ticket("quote.json")
        st = {"CSR1:intake": intake.run(ctx(t))}
        forms = acord.applications(ctx(t, st))["findings"]["forms"]
        self.assertEqual(forms["ACORD 127 Business Auto Section"]["status"], "partial")
        self.assertIn("fein", forms["ACORD 125 Commercial Insurance Application"]["fill_from_source"])

    def test_limit_change_without_a_line_asks_which_policy(self):
        t = edit(ticket("service.json"), lambda t: t["request"].update(change_type="LIMIT_CHANGE", vehicle=None,
                                                                         details="Increase auto liability CSL to $2,000,000"))
        r = service.service(ctx(t))
        self.assertEqual(r["status"], "needs_data")
        t2 = edit(t, lambda t: t["request"].update(line="AUTO"))
        self.assertEqual(service.service(ctx(t2))["findings"]["policy_number"], "ECBA-26-55120")

    def test_renewal_wording_and_renewal_on_file(self):
        def f(t):
            t["account"]["prior_policies"].append(dict(t["account"]["prior_policies"][1], policy_number="ECBA-27-1",
                                                       effective="2026-11-20", expiration="2027-11-20"))
        r = renewals.run(ctx(edit(ticket("renewal.json"), f), as_of="2026-11-10"))
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["findings"]["renewal_on_file"], "ECBA-27-1")

    def test_gvw_cross_check_and_open_band(self):
        self.assertEqual(scoring.size_class("Class 6: 19,501 - 26,000 lb", 9000)["basis"], "stated_gvw_conflicts_with_nhtsa")
        self.assertEqual(scoring.size_class("Class 8: 33,001 lb and above (14,969 kg and above)")["candidates"], ["heavy", "extra-heavy"])

    def test_market_unknown_revenue_blank_prefix_and_bad_row(self):
        rows = [{"carrier": "Banded", "line": "GL", "states": "NJ", "naics_prefixes": "5617", "min_revenue": "", "max_revenue": "5000000",
                 "am_best": "A", "appetite_url": "https://example.com", "observed": "2026-06-01", "notes": ""},
                {"carrier": "Blank", "line": "GL", "states": "NJ", "naics_prefixes": "238|", "min_revenue": "", "max_revenue": "",
                 "am_best": "A", "appetite_url": "https://example.com", "observed": "2026-06-01", "notes": ""},
                {"carrier": "Future", "line": "GL", "states": "NJ", "naics_prefixes": "*", "min_revenue": "", "max_revenue": "",
                 "am_best": "A", "appetite_url": "https://example.com", "observed": "2027-01-01", "notes": ""}]
        kept, dropped = market.filter_markets(rows, "GL", "NJ", "561730", None, AS_OF)
        self.assertEqual(kept, [])
        self.assertEqual({d["carrier"] for d in dropped}, {"Banded", "Blank", "Future"})

    def test_claims_made_future_date_and_unknown_estimate(self):
        fut = edit(ticket("claim.json"), lambda t: t["request"].update(date_of_loss="2026-12-01"))
        self.assertEqual(claimsdesk.run(ctx(fut))["findings"]["urgency"], "BAD_DATE")
        unk = edit(ticket("claim.json"), lambda t: t["request"].update(injury=False, estimated_amount=None))
        self.assertFalse(claimsdesk.run(ctx(unk))["findings"]["estimate_known"])
        cm = edit(ticket("claim.json"), lambda t: (t["account"]["prior_policies"][0]["dec_page"]["forms"].append("CG 00 02 Claims-Made"),
                                                    t["request"].update(injury=False, estimated_amount=1000)))
        self.assertTrue(claimsdesk.run(ctx(cm))["findings"]["claims_made"])

    def test_live_is_the_agencys_file_not_a_flag(self):
        cfg = isolated_config()
        self.assertFalse(orchestrator.agency_live({"desk_config": str(cfg["_tmp"] / "desk.json")}))
        (cfg["_tmp"] / "desk.json").write_text('{"live": true}')
        self.assertTrue(orchestrator.agency_live({"desk_config": str(cfg["_tmp"] / "desk.json")}))


if __name__ == "__main__":
    unittest.main()
