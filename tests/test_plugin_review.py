"""Regression tests for the plug-in layer review (2026-10-13): each built from the reviewer's bypass or failure."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import AS_OF, NOW, FakeNet, isolated_config, ticket

from csrdesk import agency, guard, humans, orchestrator, paths, policy


def ev(tool, **inp):
    return {"tool_name": tool, "tool_input": inp}


def denied(tool, **inp):
    d = guard.decide(ev(tool, **inp), live=True)
    return bool(d) and d["decision"] == "deny"


class R1HumanOnlyCommands(unittest.TestCase):
    def test_reordered_quoted_and_python_forms_are_refused(self):
        for cmd in ("bin/csr rules --by Bob confirm NJ", "bin/csr setup --by Bob confirm 'Acme'",
                    "bin/csr setup 'confirm' Acme --by Bob", "${CLAUDE_PLUGIN_ROOT}/bin/csr live on",
                    'python3 -c "from csrdesk import markets; markets.confirm(\'X\',\'me\')"', "bin/csr unlock"):
            self.assertTrue(denied("Bash", command=cmd), cmd)

    def test_confirm_needs_a_terminal_even_when_the_hook_is_bypassed(self):
        with self.assertRaises(PermissionError):
            humans.attest("Confirm NJ", "NJ", "Bob", isatty=lambda: False)
        with self.assertRaises(PermissionError):
            humans.attest("Confirm NJ", "NJ", "Bob", isatty=lambda: True, prompt=lambda _: "nj?")
        self.assertEqual(humans.attest("Confirm NJ", "NJ", "Bob", isatty=lambda: True, prompt=lambda _: "NJ"), "Bob")

    def test_reading_docs_about_confirm_is_fine(self):
        self.assertIsNone(guard.decide(ev("Bash", command="grep -n 'rules confirm' docs/DEPLOYMENT.md")))


class R2to4PathEvasion(unittest.TestCase):
    def test_shell_evasions(self):
        for cmd in ("cd H/runtime && echo '{\"live\":true}' > desk.json", "f=desk; echo x > runtime/$f.json",
                    "echo 1 > runtime/desk.j*", "ln -s ../runtime r", "ls\nrm H/runtime/STOP", "rm -rf H/runtime/audit",
                    "rm -rf H/runtime", "mv runtime runtime.bak", "cat H/runtime/pepper", "chflags nouchg H/appetite.csv"):
            self.assertTrue(denied("Bash", command=cmd), cmd)

    def test_file_tool_path_tricks(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "home" / "runtime").mkdir(parents=True)
        os.symlink(tmp / "home" / "runtime", tmp / "r")
        for p in ("H/runtime/./desk.json", "H/runtime//desk.json", "H/Runtime/Desk.json", str(tmp / "r" / "desk.json"),
                  "H/runtime/tickets/Q-1.json", "H/agency/ams.json", "H/agency/appetite.proposed.csv"):
            self.assertTrue(denied("Write", file_path=p), p)
        shutil.rmtree(tmp)


class R5to6Trust(unittest.TestCase):
    def test_other_command_tools_and_mcp_arguments(self):
        self.assertTrue(denied("Monitor", command="echo 1 > runtime/desk.json"))
        self.assertTrue(denied("PowerShell", command="Remove-Item runtime\\STOP"))
        self.assertTrue(denied("mcp__fs__write_file", path="H/runtime/desk.json", content="{}"))
        d = guard.decide(ev("mcp__fs__move_file", source="a.txt", destination="b.txt"), live=True)
        self.assertEqual(d["decision"], "ask")
        self.assertIn("a.txt", d["reason"])
        self.assertTrue(denied("Read", file_path="H/runtime/pepper"))

    def test_hook_matches_every_tool(self):
        cfg = json.loads((paths.ROOT / "hooks" / "hooks.json").read_text())
        entry = cfg["hooks"]["PreToolUse"][0]
        self.assertEqual(entry["matcher"], ".*")
        self.assertIn("exit 2", entry["hooks"][0]["command"])         # fails closed when Python cannot start


class R8to9Homes(unittest.TestCase):
    def test_init_refuses_the_plugin_directory(self):
        with self.assertRaises(ValueError):
            agency.init(str(paths.ROOT / "data"))
        with self.assertRaises(ValueError):
            agency.init(str(paths.ROOT / "anything"))

    def test_installed_plugin_without_a_home_refuses_to_guess(self):
        env = {k: v for k, v in os.environ.items() if k != paths.ENV}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(paths, "installed", return_value=True), \
                mock.patch.object(paths.Path, "cwd", return_value=Path(tempfile.mkdtemp())):
            with self.assertRaises(paths.HomeNotSet):
                paths.home()
            self.assertFalse(orchestrator.agency_live())               # and the guard still answers (not live -> ask)
            self.assertEqual(guard.decide(ev("mcp__x__applied_epic_create_activity"))["decision"], "ask")

    def test_malformed_profiles_do_not_crash(self):
        self.assertTrue(agency.validate_profile([]))
        self.assertTrue(agency.validate_profile({"producer": "x", "address": {}}))
        self.assertTrue(agency.validate_profile({"producer": {}, "address": "x"}))


class R12KillSwitchAndSelftest(unittest.TestCase):
    def test_kill_switch_stops_release(self):
        run = orchestrator.advance(orchestrator.open_ticket(ticket("coi.json")), as_of=AS_OF, now=NOW,
                                   config=(cfg := isolated_config()), fetch=FakeNet())
        run = orchestrator.decide(run, {"ticket_id": "C-2001", "decision": "APPROVE", "target": None, "note": "ok",
                                        "approvals": ["issue_coi:C-2001/0"]}, as_of=AS_OF, now=NOW, config=cfg)
        Path(cfg["kill_switch"]).write_text("stop")
        self.assertEqual(orchestrator.release(run, 0, live=True, config=cfg)["code"], "KILL_SWITCH")

    def test_hook_selftest_runs_the_real_hook(self):
        with mock.patch.dict(os.environ, {paths.ENV: tempfile.mkdtemp()}):
            self.assertTrue(agency.hook_selftest()["ok"])

    def test_classifier_no_longer_lets_read_words_launder_actions(self):
        for tool in ("read_and_archive", "sign_document_view", "dispatch_list"):
            self.assertNotEqual(policy.classify(tool)["risk"], "READ", tool)
        self.assertEqual(policy.classify("search_threads")["risk"], "READ")


if __name__ == "__main__":
    unittest.main()
