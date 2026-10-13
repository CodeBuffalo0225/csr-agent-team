"""Routing if/else, the stop-limiter, the AE feedback loop, the single writer and the audit chain."""
from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import timedelta
from pathlib import Path

from helpers import AS_OF, NOW, FakeNet, edit, isolated_config, ticket

from csrdesk import audit, limiter, orchestrator, store, thresholds


def go(name_or_ticket, cfg=None, **kw):
    t = ticket(name_or_ticket) if isinstance(name_or_ticket, str) else name_or_ticket
    cfg = cfg or isolated_config()
    run = orchestrator.advance(orchestrator.open_ticket(t), as_of=kw.get("as_of", AS_OF), now=kw.get("now", NOW),
                               config=cfg, fetch=kw.get("fetch", FakeNet()))
    return run, cfg


class Routing(unittest.TestCase):
    def test_quote_full_path(self):
        run, _ = go("quote.json")
        self.assertEqual(run["status"], "AWAITING_AE")
        self.assertEqual(run["trail"], ["CSR1:intake", "CSR5:verify", "CSR2:loss_runs", "CSR9:dec_review", "CSR10:exclusions",
                                        "CSR3:applications", "CSR4:market", "CSR5:info_request", "CSR8:record"])

    def test_new_venture_skips_dec_review(self):
        t = edit(ticket("quote.json"), lambda t: t["account"].update(years_in_business=0, prior_policies=[], loss_runs=[]))
        run, _ = go(t)
        self.assertNotIn("CSR9:dec_review", run["trail"])

    def test_all_lines_blocked_skips_apps_and_market(self):
        run, _ = go(edit(ticket("quote.json"), lambda t: t["account"].update(entity_type=None)))
        self.assertNotIn("CSR4:market", run["trail"])
        self.assertNotIn("CSR3:applications", run["trail"])
        self.assertIn("CSR5:info_request", run["trail"])

    def test_renewal_at_100_days_runs_only_the_early_rungs(self):
        run, _ = go("renewal.json", as_of="2026-08-12", now=NOW.replace(month=8))
        self.assertEqual(run["trail"], ["CSR6:renewal", "CSR2:loss_runs", "CSR9:dec_review", "CSR8:record"])

    def test_renewal_remarket_fork(self):
        run, _ = go("renewal.json")
        self.assertIn("CSR4:market", run["trail"])                       # +14% >= 10%
        calm = edit(ticket("renewal.json"), lambda t: t["account"]["prior_policies"][1].update(renewal_rate_change_pct=3))
        run, _ = go(calm)
        self.assertNotIn("CSR4:market", run["trail"])
        self.assertIn("CSR5:renewal_notice", run["trail"])

    def test_lapsed_renewal_fast_lane(self):
        run, _ = go("renewal.json", as_of="2026-11-25")      # desk date past expiration; ticket itself is fresh
        self.assertEqual(run["trail"], ["CSR6:renewal", "CSR8:record"])
        self.assertTrue(run["packet"]["escalations"])

    def test_coi_paths(self):
        ok, _ = go("coi.json")
        self.assertEqual(ok["trail"], ["CSR9:dec_review", "CSR10:exclusions", "CSR3:coi", "CSR8:record"])
        self.assertEqual(ok["packet"]["escalations"], [])
        blocked, _ = go("coi_wos.json")
        self.assertEqual(blocked["packet"]["escalations"][0]["step"], "CSR3:coi")

    def test_claim_fast_lane_keeps_essential_checks(self):
        run, _ = go("claim.json")
        self.assertEqual(run["trail"], ["CSR7:fnol", "CSR10:exclusions", "CSR9:dec_review", "CSR8:record"])

    def test_policy_question_goes_to_review_never_answered_by_csr5(self):
        t = edit(ticket("service.json"), lambda t: t["request"].update(change_type="POLICY_QUESTION", vehicle=None,
                                                                         details="Am I covered if a crew member damages a client's car?"))
        run, _ = go(t)
        self.assertEqual(run["trail"], ["CSR5:service", "CSR9:dec_review", "CSR10:exclusions", "CSR8:record"])
        self.assertEqual(run["steps"]["CSR5:service"]["drafts"], [])

    def test_every_step_records_its_rule(self):
        run, _ = go("quote.json")
        self.assertTrue(all(run["rules"][s] for s in run["steps"]))


class Limiter(unittest.TestCase):
    def _run(self, **over):
        base = {"received": "2026-10-12T13:00:00Z", "type": "QUOTE", "trail": [], "messages": [], "client_asks": 0,
                "budget_used": {"calls": 0, "writes": 0}, "ae_revisions": {}}
        return {**base, **over}

    def test_kill_switch_halts_a_live_ticket(self):
        cfg = isolated_config()
        Path(cfg["kill_switch"]).write_text("stop")
        run, _ = go("quote.json", cfg)
        self.assertEqual((run["status"], run["halt"]["code"]), ("HALTED", "KILL_SWITCH"))
        self.assertIn("CSR8:record", run["steps"])                     # recorded before the human takes it

    def test_wall_clock(self):
        run, _ = go("coi.json", now=NOW + timedelta(hours=thresholds.WALL_CLOCK_HOURS["COI"]))
        self.assertEqual(run["halt"]["code"], "WALL_CLOCK")

    def test_max_hops(self):
        h = limiter.check(self._run(trail=["X:y"] * thresholds.MAX_HOPS), "CSR1:intake", NOW, Path("/nonexistent"))
        self.assertEqual(h["code"], "MAX_HOPS")

    def test_step_revisit(self):
        h = limiter.check(self._run(trail=["CSR9:dec_review", "CSR8:record"] * thresholds.MAX_VISITS_PER_STEP), "CSR9:dec_review", NOW, Path("/nonexistent"))
        self.assertEqual(h["code"], "STEP_REVISIT")
        self.assertIsNone(limiter.check(self._run(trail=["CSR8:record"] * 6), "CSR8:record", NOW, Path("/nonexistent")))

    def test_ping_pong(self):
        h = limiter.check(self._run(trail=["CSR2:a", "CSR4:b", "CSR2:c", "CSR4:d", "CSR2:e"]), "CSR4:f", NOW, Path("/nonexistent"))
        self.assertEqual(h["code"], "PING_PONG")
        self.assertIsNone(limiter.check(self._run(trail=["CSR2:a", "CSR4:b", "CSR9:c", "CSR4:d", "CSR2:e"]), "CSR4:f", NOW, Path("/nonexistent")))

    def test_client_ask_cap_and_budget(self):
        h = limiter.check(self._run(client_asks=thresholds.MAX_CLIENT_ASKS), "CSR5:info_request", NOW, Path("/nonexistent"))
        self.assertEqual(h["code"], "CLIENT_ASK_CAP")
        h = limiter.check(self._run(budget_used={"calls": thresholds.TICKET_BUDGET["calls"], "writes": 0}), "CSR1:intake", NOW, Path("/nonexistent"))
        self.assertEqual(h["code"], "BUDGET")

    def test_message_storm(self):
        h = limiter.check(self._run(messages=[{}] * (thresholds.MAX_MESSAGES + 1)), "CSR1:intake", NOW, Path("/nonexistent"))
        self.assertEqual(h["code"], "MESSAGE_STORM")


class AeLoop(unittest.TestCase):
    def _rev(self, target, note="redo"):
        return {"ticket_id": "C-2002", "decision": "REVISE", "target": target, "note": note, "approvals": []}

    def test_revise_reruns_from_target_and_carries_the_note(self):
        run, cfg = go("coi_wos.json")
        run = orchestrator.decide(run, self._rev("CSR9", "check the blanket AI"), as_of=AS_OF, now=NOW, config=cfg, fetch=FakeNet())
        self.assertEqual(run["status"], "AWAITING_AE")
        self.assertEqual(run["steps"]["CSR9:dec_review"]["ae_note"], "check the blanket AI")
        self.assertEqual(run["trail"][-4:], ["CSR9:dec_review", "CSR10:exclusions", "CSR3:coi", "CSR8:record"])

    def test_revision_cap_halts_to_human(self):
        run, cfg = go("coi_wos.json")
        for _ in range(thresholds.MAX_AE_REVISIONS_PER_CSR):
            run = orchestrator.decide(run, self._rev("CSR9"), as_of=AS_OF, now=NOW, config=cfg, fetch=FakeNet())
        run = orchestrator.decide(run, self._rev("CSR9"), as_of=AS_OF, now=NOW, config=cfg, fetch=FakeNet())
        self.assertEqual((run["status"], run["halt"]["code"]), ("HALTED", "AE_REVISION_CAP_CSR"))
        with self.assertRaises(ValueError):
            orchestrator.decide(run, self._rev("CSR10"), as_of=AS_OF, now=NOW, config=cfg)

    def test_revise_to_a_csr_with_no_work_is_refused(self):
        run, cfg = go("coi_wos.json")
        with self.assertRaises(ValueError):
            orchestrator.decide(run, self._rev("CSR7"), as_of=AS_OF, now=NOW, config=cfg)

    def test_client_reply_refresh_and_ask_dedup(self):
        run, cfg = go("quote.json")
        self.assertEqual(run["client_asks"], 1)
        run = orchestrator.refresh(run, ticket("quote_client_reply.json"), as_of=AS_OF, now=NOW, config=cfg, fetch=FakeNet())
        self.assertEqual(run["client_asks"], 2)
        self.assertEqual(run["steps"]["CSR1:intake"]["findings"]["blocking_lines"], [])
        run = orchestrator.decide(run, {**self._rev("CSR9"), "ticket_id": "Q-1001"}, as_of=AS_OF, now=NOW, config=cfg, fetch=FakeNet())
        self.assertEqual(run["client_asks"], 2)                               # same list: not asked again

    def test_release_needs_live_and_exact_scope(self):
        run, cfg = go("coi.json")
        run = orchestrator.decide(run, {"ticket_id": "C-2001", "decision": "APPROVE", "target": None, "note": "ok",
                                        "approvals": ["issue_coi:C-2001/0"]}, as_of=AS_OF, now=NOW, config=cfg)
        self.assertEqual(orchestrator.release(run, 0, config=cfg)["code"], "DRY_RUN")
        self.assertEqual(orchestrator.release(run, 0, live=True, config=cfg)["decision"], "allow")
        run["approvals"] = ["issue_coi:C-2001/1"]
        self.assertEqual(orchestrator.release(run, 0, live=True, config=cfg)["code"], "NO_APPROVAL")

    def test_ungrounded_draft_is_discarded_and_blocks_release(self):
        run, cfg = go("coi.json")
        d = run["steps"]["CSR3:coi"]["drafts"][0]
        d["body"] += " Limits were raised to $5,000,000 last week."
        from csrdesk import grounding
        d["gate"] = grounding.ground(d["body"], d["allowed"])
        self.assertFalse(d["gate"]["ok"])
        run["approvals"] = ["issue_coi:C-2001/0"]
        self.assertEqual(orchestrator.release(run, 0, live=True, config=cfg)["code"], "CONTENT_BLOCKED")


class WriterAndAudit(unittest.TestCase):
    def test_only_csr8_writes_and_dedups_on_fein(self):
        cfg = isolated_config()
        s = store.Store(cfg["db"])
        acct = ticket("quote.json")["account"]
        with self.assertRaises(PermissionError):
            s.write("CSR3", fein_digest=None, masked_account=acct, ticket_id="x", ticket_type="QUOTE", status="OPEN", packet={})
        a = s.write("CSR8", fein_digest=store.fein_hash("12-3456789"), masked_account=acct, ticket_id="t1", ticket_type="QUOTE", status="OPEN", packet={})
        b = s.write("CSR8", fein_digest=store.fein_hash("123456789"), masked_account={**acct, "insured_name": "Meadowlands Lawn and Snow"},
                    ticket_id="t2", ticket_type="COI", status="OPEN", packet={})
        self.assertEqual(a["account_key"], b["account_key"])
        self.assertIn("fein", b["action"])
        with self.assertRaises(sqlite3.DatabaseError):
            s.db.execute("DELETE FROM events")
        s.close()

    def test_no_raw_pii_in_state_db_or_audit(self):
        run, cfg = go("quote.json")
        blobs = json.dumps(run) + Path(cfg["audit_dir"], "Q-1001.jsonl").read_text() + Path(cfg["db"]).read_bytes().decode("latin-1")
        for raw in ("S12345678901234", "E98765432109876", "12-3456789", "123456789", "1984-03-09"):
            self.assertNotIn(raw, blobs)

    def test_audit_chain_detects_tampering(self):
        run, cfg = go("quote.json")
        log = audit.AuditLog(Path(cfg["audit_dir"]) / "Q-1001.jsonl")
        self.assertTrue(log.verify()["ok"])
        lines = log.path.read_text().splitlines()
        rec = json.loads(lines[2])
        rec["status"] = "done"
        lines[2] = json.dumps(rec, sort_keys=True)
        log.path.write_text("\n".join(lines) + "\n")
        self.assertEqual(log.verify()["broken_at"], 2)

    def test_messages_never_move_control(self):
        # every line blocked: CSR 2 still NOTIFYs CSR 4 about the large loss, but only the router runs CSR 4 — and it doesn't
        run, _ = go(edit(ticket("quote.json"), lambda t: t["account"].update(entity_type=None)))
        self.assertTrue(any(m["to"] == "CSR4" for m in run["messages"]))
        self.assertNotIn("CSR4:market", run["trail"])
        self.assertEqual(run["trail"], [s for s in run["steps"]])


if __name__ == "__main__":
    unittest.main()
