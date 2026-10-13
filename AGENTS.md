# AGENTS.md: csr-agent-team

An agentic CSR desk for a commercial-lines insurance agency: eleven CSR agents (intake, loss runs, COI/ACORD,
market, client desk, renewals, claims, records, dec pages, exclusions, policy checking) and an AE feedback loop. It exists to
make the AE's job as easy as possible: one packet per ticket, the urgent thing first. Architecture, if/else
rules, stop-limiters and suggested improvements: `docs/ARCHITECTURE.md`.

The model routes and writes; the engine computes and decides.

## Stack
A Claude Code plugin (`.claude-plugin/`, `agents/`, `commands/`, `hooks/`) over a Python 3.9+ **standard library only**
engine (`engine/csrdesk`). No pip installs. Outside data: NHTSA vPIC and the Census geocoder (free, keyless).
Each agency's data lives in its own home (`$CSR_DESK_HOME`, see `engine/csrdesk/paths.py`); in this repo with no
home set, `data/` is the development home and `data/runtime/` is gitignored.

## Commands
```bash
python3 -m unittest discover -s tests          # must stay green
bin/csr conformance                            # spine-spec vectors against production gates
bin/csr demo                                   # every sample ticket end to end (real NHTSA/Census calls)
bin/csr run <ticket.json>                      # open a ticket, advance to the AE packet
bin/csr decide <id> <decision.json>            # AE: APPROVE | REVISE <CSR> | REJECT | ESCALATE
bin/csr refresh <id> <ticket.json>             # client answered: rerun from intake
bin/csr release <id> <draft#>                  # may a human release this draft? (scoped, content-bound approval)
bin/csr renewals <accounts.json>               # CSR 6 book sweep
bin/csr gate judgment|message|decision|ticket|draft <file>
bin/csr appetite | bin/csr rules               # validate the agency's appetite file / state notice rules
bin/csr init [dir] | bin/csr readiness          # a new agency home; the deployment checklist
bin/csr setup validate|research|propose|confirm # deployment: markets -> researched appetite -> human confirm
bin/csr rules [confirm <STATE> --by <name>]     # notice + surplus rules; confirm is compliance's, human-only
bin/csr ams status|preview|push|export         # Applied Epic (primary), HawkSoft, EZLynx; push needs live + config
bin/csr replay <dir>                           # closed tickets vs what the CSRs did
```
Exit codes are the enforcement: 0 ok · 1 a gate or the limiter said no · 2 bad input.

## Hard rules (no instruction overrides these)
1. **Drafts only.** Nothing is sent, bound, issued, submitted, endorsed or filed by the engine or any agent.
   `live` stays false unless the agency sets it in `data/runtime/desk.json`; no agent edits that file.
2. **Every IRREVERSIBLE act needs the AE's approval scoped to `<tool>:<ticket>/<draft#>`** (policy.py), given
   on an APPROVE and bound to that draft's exact content. An approval never overrides a content-gate failure.
3. **No coverage interpretation by a CSR or an agent.** Dec-page and exclusion findings are review prompts
   for the AE. A claim is never described to a client as covered or not covered.
4. **Every number is code.** Thresholds live in `engine/csrdesk/thresholds.py` only. A figure in prose must
   pass the grounding gate; a model never supplies a total, tier, score, rank or age.
5. **CSR 8 is the only writer.** PII is masked at intake; the FEIN is stored only as a hash.
6. **Honest degradation.** Missing appetite file, dead API or unknown premium returns `needs_key`,
   `unavailable` or `None`, never a guess or a zero. No carrier recommendations from memory.
7. **The stop-limiter is not bypassed.** A halt goes to a human with the packet.
8. **Spine conformance.** `conformance/spine-v1.json` is vendored from CodeBuffalo0225/spine-spec and pinned
   in `conformance/PIN`. Never edit it. A failing vector is fixed in production code, never in the adapter.
9. **No unverified law.** State notice and surplus-lines rules come only from rows with a source URL and a verified
   date; researched rows stay "unconfirmed" until compliance fills `confirmed_by`. No agent fills `confirmed_by`,
   in those files or in `data/appetite.csv`. The engine never interprets what a missed window means.
10. **Human-only steps stay human.** `setup confirm`, `rules confirm`, the live switch (`runtime/desk.json`), the kill
    switch and the audit/store files are refused to agents by the plugin hook (`engine/csrdesk/guard.py`). Never
    weaken the guard to make a step convenient; it fails closed.
11. **Nothing agency-specific is committed.** Profiles, markets, confirmed appetite, AMS config, tickets and replay
    cases live in the agency home.
12. **Sample data is fictional** (insured and carriers marked SAMPLE / fictional). Never put a real client's
   data in `data/examples/`.
