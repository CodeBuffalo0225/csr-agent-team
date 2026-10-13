"""Plug-in deployment: a fresh agency home, the readiness checklist, the agency's identity on drafts, the
human-only confirmations, and the plugin's fail-closed guard."""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import AS_OF, NOW, ROOT, FakeNet, isolated_config, ticket

from csrdesk import agency, guard, markets, orchestrator, paths, staterules

PROFILE = {"agency_name": "SAMPLE Bergen Commercial Agency (fictional)", "dba": None,
           "address": {"street": "10 Example St", "city": "Hackensack", "state": "NJ", "zip": "07601"},
           "phone": "201-555-0100", "email": "service@example.com", "website": None,
           "producer": {"name": "Pat Example", "npn": "12345678", "license_state": "NJ"},
           "states_written": ["NJ", "NY"], "lines_written": ["GL", "AUTO"], "ams_system": "none",
           "compliance_owner": "Lee Compliance", "service_hours": None}


class Home:
    """An isolated agency home for one test."""

    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="csr-agency-"))
        self.env = mock.patch.dict(os.environ, {paths.ENV: str(self.dir)})
        self.env.start()
        return self.dir

    def __exit__(self, *a):
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


def onboard(h: Path, replay_cases: int = agency.REPLAY_TARGET):
    (h / "agency" / "agency.json").write_text(json.dumps(PROFILE))
    with (h / "agency" / "markets.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, markets.MARKET_COLUMNS)
        w.writeheader()
        w.writerow({"market": "Admitted Co (fictional)", "kind": "carrier", "admitted": "Y", "appointed": "Y", "lines": "GL|AUTO", "states": "NJ|NY"})
    from csrdesk import market
    with (h / "appetite.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, market.APPETITE_COLUMNS)
        w.writeheader()
        w.writerow({"carrier": "Admitted Co (fictional)", "line": "GL", "states": "NJ", "naics_prefixes": "5617", "am_best": "A",
                    "appetite_url": "https://example.com/a", "observed": "2026-09-01", "confirmed_by": "Ops Manager"})
    for st in ("NJ", "NY"):
        staterules.confirm(st, "Lee Compliance")
    src = json.loads((ROOT / "data" / "replay" / "05_claim.json").read_text())
    for i in range(replay_cases):
        (h / "replay" / f"case_{i:02d}.json").write_text(json.dumps(dict(src, ticket_id=f"L-R{i:02d}")))


class FreshAgency(unittest.TestCase):
    def test_init_creates_the_home_and_never_overwrites(self):
        with Home() as h:
            r = agency.init()
            self.assertIn("agency/agency.json", r["created"])
            (h / "agency" / "agency.json").write_text("{\"kept\": true}")
            r2 = agency.init()
            self.assertIn("agency/agency.json", r2["kept"])
            self.assertEqual(json.loads((h / "agency" / "agency.json").read_text()), {"kept": True})
            self.assertFalse(json.loads((h / "runtime" / "desk.json").read_text())["live"])

    def test_fresh_home_is_not_ready_and_says_what_is_left(self):
        with Home():
            agency.init()
            r = agency.readiness(AS_OF)
            self.assertEqual(r["verdict"], "NOT READY")
            todo = [i["step"] for i in r["items"] if i["status"] == "todo"]
            self.assertTrue(any(s.startswith("1 agency profile") for s in todo))
            self.assertTrue(all(i["fix"] for i in r["items"] if i["status"] == "todo"))

    def test_onboarded_agency_is_ready(self):
        with Home() as h:
            agency.init()
            onboard(h)
            r = agency.readiness(AS_OF)
            self.assertEqual(r["verdict"], "READY", [i for i in r["items"] if i["status"] == "todo"])

    def test_unconfirmed_rules_or_short_replay_block_ready(self):
        with Home() as h:
            agency.init()
            onboard(h, replay_cases=5)
            r = agency.readiness(AS_OF)
            self.assertEqual(r["verdict"], "NOT READY")
            self.assertEqual([i["step"] for i in r["items"] if i["status"] == "todo"], ["6 replay closed tickets"])

    def test_profile_validation(self):
        self.assertEqual(agency.validate_profile(PROFILE), [])
        bad = dict(PROFILE, producer={"name": "", "npn": "ABC", "license_state": "Jersey"}, states_written=[], ams_system="other")
        self.assertGreaterEqual(len(agency.validate_profile(bad)), 4)


class AgencyIdentity(unittest.TestCase):
    def test_signature_and_producer_block(self):
        with Home() as h:
            agency.init()
            (h / "agency" / "agency.json").write_text(json.dumps(PROFILE))
            run = orchestrator.advance(orchestrator.open_ticket(ticket("quote.json")), as_of=AS_OF, now=NOW,
                                       config=isolated_config(), fetch=FakeNet())
            email = run["steps"]["CSR5:info_request"]["drafts"][0]
            self.assertIn("Pat Example\nSAMPLE Bergen Commercial Agency (fictional)", email["body"])
            self.assertTrue(email["gate"]["ok"], email["gate"])
            coi = orchestrator.advance(orchestrator.open_ticket(ticket("coi.json")), as_of=AS_OF, now=NOW,
                                       config=isolated_config(), fetch=FakeNet())
            block = coi["steps"]["CSR3:coi"]["findings"]["acord25"]["producer"]
            self.assertEqual(block["contact"], "Pat Example")
            self.assertTrue(coi["steps"]["CSR3:coi"]["drafts"][0]["gate"]["ok"])

    def test_rules_confirm_writes_the_agencys_copy_only(self):
        with Home() as h:
            shipped = (ROOT / "data" / "state_rules.csv").read_text()
            staterules.confirm("NJ", "Lee Compliance")
            self.assertEqual((ROOT / "data" / "state_rules.csv").read_text(), shipped)
            self.assertEqual(staterules.rule_for("NJ", "GL", AS_OF)["status"], "ok")
            self.assertEqual(staterules.rule_for("NY", "GL", AS_OF)["status"], "unconfirmed")
            self.assertTrue((h / "state_rules.csv").exists())


class Guard(unittest.TestCase):
    def ev(self, tool, **inp):
        return {"tool_name": tool, "tool_input": inp}

    def test_connectors(self):
        self.assertEqual(guard.decide(self.ev("mcp__6f85f0d0-f594__send_message"))["decision"], "ask")
        self.assertEqual(guard.decide(self.ev("mcp__x__get_thread"))["decision"], "allow")
        self.assertEqual(guard.decide(self.ev("mcp__x__applied_epic_create_activity"), live=False)["decision"], "ask")
        self.assertEqual(guard.decide(self.ev("mcp__x__applied_epic_create_activity"), live=True)["decision"], "allow")
        self.assertEqual(guard.decide(self.ev("mcp__x__bind_policy"), live=True)["decision"], "ask")

    def test_human_only_files_and_commands(self):
        for path in ("/a/home/runtime/desk.json", "/a/home/appetite.csv", "/a/home/state_rules.csv", "/a/home/runtime/audit/Q.jsonl"):
            self.assertEqual(guard.decide(self.ev("Write", file_path=path))["decision"], "deny", path)
        self.assertIsNone(guard.decide(self.ev("Write", file_path="/a/home/agency/markets.csv")))
        self.assertEqual(guard.decide(self.ev("Bash", command='bin/csr setup confirm "X" --by me'))["decision"], "deny")
        self.assertEqual(guard.decide(self.ev("Bash", command="bin/csr rules confirm NJ --by me"))["decision"], "deny")
        self.assertEqual(guard.decide(self.ev("Bash", command="echo '{\"live\":true}' > csr-desk/runtime/desk.json"))["decision"], "deny")
        self.assertIsNone(guard.decide(self.ev("Bash", command="cat csr-desk/appetite.csv")))
        self.assertIsNone(guard.decide(self.ev("Bash", command="bin/csr readiness")))

    def test_hook_script_fails_closed(self):
        hook = ROOT / "engine" / "hook.py"
        bad = subprocess.run([sys.executable, str(hook)], input="not json", capture_output=True, text=True, timeout=30)
        out = json.loads(bad.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        ok = subprocess.run([sys.executable, str(hook)], input=json.dumps(self.ev("mcp__x__file_claim")), capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(ok.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_plugin_manifest_and_layout(self):
        m = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(m["name"], "csr-desk")
        self.assertTrue((ROOT / "hooks" / "hooks.json").exists())
        names = {p.stem for p in (ROOT / "agents").glob("*.md")}
        self.assertEqual(len([n for n in names if n.startswith("csr-")]), 11)
        self.assertTrue({"csr", "csr-setup", "csr-readiness"} <= {p.stem for p in (ROOT / "commands").glob("*.md")})


if __name__ == "__main__":
    unittest.main()
