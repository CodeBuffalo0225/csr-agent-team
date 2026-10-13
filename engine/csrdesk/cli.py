"""bin/csr — the desk from the command line. Exit codes are the enforcement: 0 ok · 1 a gate or the
stop-limiter said no · 2 bad input.

    bin/csr run <ticket.json> [--as-of D] [--now TS] [--json]     open a ticket and advance it to the AE
    bin/csr decide <ticket_id> <decision.json> [--update t.json]  AE feedback: APPROVE | REVISE | REJECT | ESCALATE
    bin/csr refresh <ticket_id> <ticket.json>                     the client answered; re-run from intake
    bin/csr release <ticket_id> <draft#>                          may a human release this draft? (spine S8; live = data/runtime/desk.json)
    bin/csr packet <ticket_id> [--json]                           the AE packet
    bin/csr renewals <accounts.json> [--as-of D]                  CSR 6 book sweep
    bin/csr vin <VIN> [<VIN> ...]                                 check digit + NHTSA decode
    bin/csr gate judgment|message|decision|ticket <file.json>     schema gate on one record
    bin/csr gate draft <body.txt> --allowed <figure> ...          grounding gate on prose
    bin/csr classify <tool_name>                                  READ | WRITE | IRREVERSIBLE
    bin/csr audit <ticket_id>                                     verify the hash chain
    bin/csr conformance                                           spine-spec vectors against production gates
    bin/csr demo                                                  every example ticket end to end (isolated runtime)
    bin/csr appetite [file]                                       validate the agency appetite file (default data/appetite.csv)
    bin/csr rules [file]                                          validate notice + surplus lines rules; count unconfirmed rows
    bin/csr setup validate|research                               deployment: check markets.csv; research tasks per appointed market
    bin/csr setup propose <rows.csv> | confirm <market> --by <n>  proposed appetite rows; a person confirms one market's rows
    bin/csr ams status|preview <id> [--system s]|push <id>|export <dir>   Applied Epic (primary), HawkSoft, EZLynx
    bin/csr replay <dir> [--live]                                 closed tickets vs what the CSRs did: agreement report
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import ams, audit, conformance, grounding, market, markets, orchestrator, packet, policy, publicdata, renewals, replay, schemas, staterules, store

ROOT = Path(__file__).resolve().parents[2]


def _now(s):
    return schemas.parse_timestamp(s) if s else dt.datetime.now(dt.timezone.utc)


def _today(s):
    return s or dt.date.today().isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _config(args) -> dict:
    cfg = {}
    if getattr(args, "appetite", None):
        cfg["appetite"] = args.appetite
    if getattr(args, "judgments", None):
        cfg["judgments"] = market.load_judgments(args.judgments)
    return cfg


def _emit(run, as_json):
    p = run["packet"]
    print(json.dumps(p, indent=2, default=str) if as_json else packet.to_markdown(p))
    return 0 if run["status"] in ("AWAITING_AE", "CLOSED_APPROVED", "CLOSED_REJECTED", "ESCALATED") else 1


def cmd_run(a):
    cfg = _config(a)
    run = orchestrator.advance(orchestrator.open_ticket(_read(a.ticket)), as_of=_today(a.as_of), now=_now(a.now), config=cfg)
    orchestrator.save(run)
    return _emit(run, a.json)


def cmd_decide(a):
    run = orchestrator.load(a.ticket_id)
    run = orchestrator.decide(run, _read(a.decision), as_of=_today(a.as_of), now=_now(a.now), config=_config(a),
                              updated_raw=_read(a.update) if a.update else None)
    orchestrator.save(run)
    return _emit(run, a.json)


def cmd_refresh(a):
    run = orchestrator.refresh(orchestrator.load(a.ticket_id), _read(a.ticket), as_of=_today(a.as_of), now=_now(a.now),
                               config=_config(a))
    orchestrator.save(run)
    return _emit(run, a.json)


def cmd_release(a):
    run = orchestrator.load(a.ticket_id)
    v = orchestrator.release(run, a.index, live=orchestrator.agency_live())
    orchestrator.save(run)
    print(json.dumps(v, indent=2))
    return 0 if v["decision"] == "allow" else 1


def cmd_packet(a):
    return _emit(orchestrator.load(a.ticket_id), a.json)


def cmd_renewals(a):
    data = _read(a.accounts)
    accounts = data if isinstance(data, list) else [data.get("account", data)]
    print(json.dumps(renewals.scan(accounts, _today(a.as_of)), indent=2))
    return 0


def cmd_vin(a):
    b = policy.Budget({"calls": 20})
    print(json.dumps(publicdata.decode_vins([{"vin": v} for v in a.vins], b, _today(None)), indent=2))
    return 0


def cmd_gate(a):
    if a.kind == "draft":
        r = grounding.ground(Path(a.file).read_text(encoding="utf-8"), a.allowed or [])
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    check = {"judgment": schemas.check_judgment, "message": schemas.check_message,
             "decision": schemas.check_ae_decision, "ticket": schemas.check_ticket}[a.kind]
    try:
        check(_read(a.file))
    except schemas.GateError as e:
        print(json.dumps({"ok": False, "code": e.code, "detail": e.detail}, indent=2))
        return 1
    print(json.dumps({"ok": True}))
    return 0


def cmd_classify(a):
    print(json.dumps(policy.classify(a.tool), indent=2))
    return 0


def cmd_audit(a):
    head = orchestrator.load(a.ticket_id).get("audit_head") if orchestrator.ticket_path(a.ticket_id).exists() else None
    r = audit.AuditLog(orchestrator.RUNTIME / "audit" / f"{a.ticket_id}.jsonl").verify(expected=head)
    print(json.dumps(r))
    return 0 if r["ok"] else 1


def cmd_conformance(_a):
    return conformance.main()


def cmd_appetite(a):
    r = market.validate_appetite(a.file, _today(None))
    print(json.dumps(r, indent=2))
    return 0 if r["ok"] else 1


def cmd_rules(a):
    rows = staterules.load(a.file)
    errors = staterules.validate(rows, _today(None))
    status = "needs_data" if not rows else ("ok" if not errors else "invalid")
    surplus = staterules.load(None, staterules.SURPLUS_DEFAULT)
    unconfirmed = sorted({f"{r['state']}" for r in rows + surplus if not (r.get("confirmed_by") or "").strip()})
    print(json.dumps({"status": status, "rows": len(rows), "surplus_rows": len(surplus), "errors": errors,
                      "unconfirmed_states": unconfirmed,
                      "confirm": "compliance sets confirmed_by on each row after checking it against its source_url" if unconfirmed else None},
                     indent=2))
    return 0 if status == "ok" else 1


def cmd_setup(a):
    rows = markets.load_markets(a.markets)
    if a.action in ("validate", "research"):
        if rows is None:
            print(json.dumps({"status": "needs_data", "fix": "copy data/agency/markets.example.csv to data/agency/markets.csv and list "
                                                            "your appointed carriers and wholesalers/MGAs"}, indent=2))
            return 1
        errors = markets.validate_markets(rows)
        if a.action == "validate" or errors:
            print(json.dumps({"status": "ok" if not errors else "invalid", "markets": len(rows),
                              "appointed": len(markets.appointed(rows)), "errors": errors}, indent=2))
            return 0 if not errors else 1
        print(json.dumps(markets.research_tasks(rows), indent=2))
        return 0
    if a.action == "propose":
        if rows is None or not a.arg:
            raise ValueError("setup propose needs data/agency/markets.csv and a CSV of proposed rows")
        with open(a.arg, newline="", encoding="utf-8") as f:
            import csv as _csv
            r = markets.propose(list(_csv.DictReader(f)), rows, _today(None))
        print(json.dumps(r, indent=2))
        return 0 if not r["rejected"] else 1
    if a.action == "confirm":
        if not a.arg:
            raise ValueError("setup confirm needs a market name")
        print(json.dumps(markets.confirm(a.arg, a.by), indent=2))
        return 0
    raise ValueError(a.action)


def cmd_ams(a):
    if a.action == "status":
        print(json.dumps(ams.connector_status(), indent=2))
        return 0
    if a.action in ("preview", "push"):
        if not a.out:
            raise ValueError(f"ams {a.action} needs a ticket id")
        run = orchestrator.load(a.out)
        if a.action == "preview":
            print(json.dumps(ams.preview(run, a.system), indent=2, ensure_ascii=False))
            return 0
        r = ams.push(run, live=orchestrator.agency_live())
        print(json.dumps(r, indent=2))
        return 0 if r["status"] == "pushed" else 1
    if not a.out:
        raise ValueError("ams export needs an output directory")
    s = store.Store()
    try:
        print(json.dumps(ams.export(s, a.out), indent=2))
    finally:
        s.close()
    return 0


def cmd_replay(a):
    r = replay.run_dir(a.dir, live=a.live)
    print(json.dumps(r if a.json else {k: v for k, v in r.items() if k != "results"} |
                     {"disagreements": [x for x in r["results"] if not x["agree"]]}, indent=2))
    return 0 if r["disagree"] == 0 else 1


def cmd_demo(a):
    from . import demo
    return demo.main(fetch=None, out_dir=Path(a.out) if a.out else None)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="csr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, appetite=True):
        sp.add_argument("--as-of", help="YYYY-MM-DD the desk treats as today (default: today)")
        sp.add_argument("--now", help="ISO timestamp for the wall-clock limiter (default: now)")
        sp.add_argument("--json", action="store_true")
        if appetite:
            sp.add_argument("--appetite", help="agency appetite CSV (default data/appetite.csv)")
            sp.add_argument("--judgments", help="CSR 4 market judgments JSON (schema-gated)")

    s = sub.add_parser("run"); s.add_argument("ticket"); common(s); s.set_defaults(fn=cmd_run)
    s = sub.add_parser("decide"); s.add_argument("ticket_id"); s.add_argument("decision"); s.add_argument("--update"); common(s); s.set_defaults(fn=cmd_decide)
    s = sub.add_parser("refresh"); s.add_argument("ticket_id"); s.add_argument("ticket"); common(s); s.set_defaults(fn=cmd_refresh)
    s = sub.add_parser("release"); s.add_argument("ticket_id"); s.add_argument("index", type=int); s.set_defaults(fn=cmd_release)
    s = sub.add_parser("packet"); s.add_argument("ticket_id"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_packet)
    s = sub.add_parser("renewals"); s.add_argument("accounts"); s.add_argument("--as-of"); s.set_defaults(fn=cmd_renewals)
    s = sub.add_parser("vin"); s.add_argument("vins", nargs="+"); s.set_defaults(fn=cmd_vin)
    s = sub.add_parser("gate"); s.add_argument("kind", choices=["judgment", "message", "decision", "ticket", "draft"]); s.add_argument("file")
    s.add_argument("--allowed", nargs="*"); s.set_defaults(fn=cmd_gate)
    s = sub.add_parser("classify"); s.add_argument("tool"); s.set_defaults(fn=cmd_classify)
    s = sub.add_parser("audit"); s.add_argument("ticket_id"); s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("conformance"); s.set_defaults(fn=cmd_conformance)
    s = sub.add_parser("demo"); s.add_argument("--out"); s.set_defaults(fn=cmd_demo)
    s = sub.add_parser("appetite"); s.add_argument("file", nargs="?", default="data/appetite.csv"); s.set_defaults(fn=cmd_appetite)
    s = sub.add_parser("rules"); s.add_argument("file", nargs="?", default=None); s.set_defaults(fn=cmd_rules)
    s = sub.add_parser("ams"); s.add_argument("action", choices=["status", "preview", "push", "export"]); s.add_argument("out", nargs="?")
    s.add_argument("--system", default="applied_epic", choices=sorted(ams.SYSTEMS)); s.set_defaults(fn=cmd_ams)
    s = sub.add_parser("setup"); s.add_argument("action", choices=["validate", "research", "propose", "confirm"]); s.add_argument("arg", nargs="?")
    s.add_argument("--by", help="the person at the agency confirming"); s.add_argument("--markets", help="markets.csv path"); s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("replay"); s.add_argument("dir"); s.add_argument("--live", action="store_true"); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_replay)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except schemas.GateError as e:
        print(json.dumps({"ok": False, "code": e.code, "detail": e.detail}), file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
