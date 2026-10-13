# Plugging the desk into an agency

The desk is a Claude Code plugin. Any commercial-lines agency installs the same plugin, and everything that
belongs to that agency (its profile, markets, appetite, confirmed rules, tickets, audit trail) lives in its own
**agency home** outside the plugin. One install can serve one agency or several; nothing agency-specific is ever
committed here.

## 0. Install

```text
/plugin marketplace add CodeBuffalo0225/csr-agent-team
/plugin install csr-desk@csr-desk
```

The repository is private, so the installing account needs read access. The plugin brings:
- **11 CSR agents** and the AE briefer.
- **Commands:** `/csr` (work a ticket), `/csr-setup` (guided onboarding) and `/csr-readiness`.
- **A safety hook.** It asks a person before any irreversible connector call: send, bind, issue a certificate,
  submit, file a claim, pay, or pull an MVR. It also asks before AMS writes while the desk isn't live, refuses
  agent edits to the human-only files, and fails closed.

**Prerequisite:** Python 3.9+ on PATH as `python3`, on every workstation; no packages beyond that. The hook is a
Python script. If it can't start, the hook blocks every tool call (exit 2) rather than letting tools run unguarded,
and `bin/csr readiness` runs the real hook as a self-test.

## 1. Create the agency home

```bash
bin/csr init ~/CSRDesk/<agency>          # inside Claude Code: ${CLAUDE_PLUGIN_ROOT}/bin/csr
export CSR_DESK_HOME=~/CSRDesk/<agency>  # add to the shell profile on each workstation (the agency does this, not the desk)
bin/csr readiness                        # the checklist: READY, or every step that is left and how to do it
```

Or run `/csr-setup` and Claude walks the agency through steps 2–6. `init` never overwrites an existing file.

## 2. Agency profile — `agency/agency.json`

Agency name, address, phone, email, the **producer** (name, NPN, license state), the **states and lines written**,
the **AMS** (`applied_epic` · `hawksoft` · `ezlynx` · `none`) and the **compliance owner**. The profile puts the
producer block on every ACORD 25 and the producer's signature on every client email. It also tells readiness
which state rules must be confirmed, and flags renewals for accounts in states the agency doesn't write.

## 3. Markets — carriers and wholesalers

List every market the agency can place with in `agency/markets.csv`:

| column | values |
|---|---|
| market | name as it should appear on submissions |
| kind | `carrier` · `wholesaler` · `mga` |
| admitted | `Y` / `N` (a non-admitted carrier says in `notes` how it's reached) |
| appointed | `Y` / `N`; only appointed markets are researched or recommended |
| lines / states | `GL\|AUTO`, `NJ\|NY`, or `*` |
| appetite_url | where its appetite or class guide lives, if known |

Then build the appetite file. The agent researches and proposes; a person confirms:

```bash
bin/csr setup validate
bin/csr setup research > research.json       # one task per appointed market for the csr-04-market agent
bin/csr setup propose proposed.csv            # schema-checked; parked in agency/appetite.proposed.csv, NOT live
bin/csr setup confirm "Market"                # a person, in their own terminal: it asks their name and the market typed back
```

CSR 4 uses only confirmed rows from appointed markets, and admitted carriers come first. When no admitted market
fits a line, the packet opens the wholesale path (the appointed wholesalers/MGAs for that line and state) with
the state's surplus-lines declination rule attached.

## 4. State rules — researched, then confirmed by compliance

The plugin ships **NJ, NY and CT** notice periods and **NJ, NY and PA** surplus-lines rules, read from the primary
text on 2026-10-13. Every row is cited and every row is *unconfirmed* until the agency's compliance owner checks it
against its source and runs `bin/csr rules confirm <STATE>` in their own terminal. The confirmation is recorded in
`<home>/rule_confirmations.csv`, bound to a digest of the exact rows. If a plugin update or an agency edit changes
a row, the sign-off lapses and the packet says to re-confirm. Shipped files are never edited, so corrections reach
every agency. A state the agency adds goes in `<home>/state_rules.csv`, with its source and verified date.

| | New Jersey | New York | Connecticut |
|---|---|---|---|
| Non-renewal notice | 30–120 days ([N.J.A.C. 11:1-20.2(b)](https://www.law.cornell.edu/regulations/new-jersey/N-J-A-C-11-1-20-2)) | 60–120 days; excess liability 30–120 ([§3426(e)(3)](https://newyork.public.law/laws/n.y._insurance_law_section_3426)) | at least 60 days; professional liability 90 ([§38a-323](https://www.cga.ct.gov/current/pub/chap_700.htm)) |
| Cancellation, nonpayment | at least 10 days (11:1-20.2(e)) | 15 days once in force 60 days; 20 in the first 60 | at least 10 days ([§38a-324](https://www.cga.ct.gov/current/pub/chap_700.htm)) |
| Cancellation, other | 30–120 days (11:1-20.2(d)) | 15 days, listed grounds only | 10 days for fraud/crime/wilful act; 60 days otherwise; professional 90 |
| Not covered | WC/EL, surplus-lines policies, multi-state risks ([11:1-20.1](https://www.law.cornell.edu/regulations/new-jersey/N-J-A-C-11-1-20-1)) | WC/EL, principally marine, excess-line policies (§3426(l)(2)) | WC, residual-market auto; surplus-lines insurers **are** bound (§38a-324(b)) |

| Surplus lines | Rule |
|---|---|
| NJ | declined by 3 NJ-authorized insurers; exportable-list classes exempt ([N.J.A.C. 11:1-33.2](https://www.law.cornell.edu/regulations/new-jersey/N-J-A-C-11-1-33-2)) |
| NY | affidavit of diligent effort, 3 declinations, a new effort per policy and renewal ([§2118 / Reg 41, DFS](https://www.dfs.ny.gov/insurance/ogco2006/rg060805.htm)) |
| PA | documented declinations from at least 3 admitted insurers; export list exempt ([31 Pa. Code §124.5](https://www.law.cornell.edu/regulations/pennsylvania/31-Pa-Code-SS-124-5)) |

**Deliberately not loaded:**
- **PA notice periods (Act 86, 40 P.S. §3401 ff.).** The legislature's site refuses automated access, and the
  regulations don't restate the day counts. Secondary sources say 60 days for non-renewal and 15 for nonpayment;
  PA compliance adds the rows with the statute as source.
- **CT surplus lines.** A law-firm report says Public Act 26-69 repealed CT's diligent-effort requirement effective
  2026-10-01; that isn't confirmed from an official source.
- **Workers' compensation everywhere,** NJ medical malpractice, and NY jumbo risks.

Readiness lists every state and line the agency writes whose rule is missing, stale or unconfirmed.

## 5. AMS

```bash
bin/csr ams status                                        # what is missing, and what the vendor requires
bin/csr ams preview <ticket> --system applied_epic|hawksoft|ezlynx
```

Applied Epic is the primary target, with HawkSoft and EZLynx alongside. All three gate their APIs:
- **Applied Epic:** the [Applied Dev Center](https://devcenter.myappliedproducts.com). Production needs Applied's
  approval and an agency SDK/API license.
- **HawkSoft:** the certified [Partner API](https://blog.hawksoft.com/partner-api-3.0) program ([terms](https://hawksoft.com/terms/api)).
- **EZLynx:** partner/enterprise access ([partners](https://www.ezlynx.com/partners/automation)).

None publishes an endpoint reference, so the desk invents none. Once access is granted, `agency/ams.json` holds
the https base URL, the approved paths (optionally `paths.health`, a read endpoint) and the name of the token's
environment variable, which must start with `CSR_DESK_`. The token itself is never in the file.
- `bin/csr ams test` proves the credentials with one read call.
- `bin/csr ams push <ticket>` writes the ticket note and the client record. It's logged, idempotent, and stopped
  by the kill switch. With `ams_system: none`, `bin/csr ams export`
writes masked import files instead.

## 6. Security model: what protects the human-only steps

A person, not an agent, does these four things:
- confirms a market's appetite;
- confirms a state's rules;
- turns the desk live;
- locks the protected files.

Four layers, each assuming the one before it can fail:

1. **Terminal attestation, in the engine.** `setup confirm`, `rules confirm`, `live on|off` and `lock|unlock` need an
   interactive terminal and the person typing the market, state or word shown. Claude Code's Bash tool has no
   terminal, so an agent running the identical command is refused. These steps are run in the person's own terminal.
2. **The plugin hook** (`engine/csrdesk/guard.py`, on every tool):
   - It refuses the human-only subcommands in any argument order or quoting.
   - It refuses any command or file write that touches an agency's `runtime/` (live switch, kill switch, tickets,
     audit, store, pepper), the confirmed appetite, the rule confirmations or `ams.json`. Paths are normalized,
     symlinks resolved and case folded.
   - It asks a person before every irreversible connector call.
3. **The audit chain decides releases.** A draft is releasable only if the hash-chained audit log recorded the AE's
   APPROVE of that exact content. Editing a ticket file grants nothing.
4. **The OS.** `bin/csr lock` sets the user-immutable flag on the human-only files where the OS supports it (macOS).
   Elsewhere, have IT make them read-only to the account Claude Code runs as.

What no hook can do is fully contain an agent that has a shell: Python can build a path at runtime. That's why
layers 1, 3 and 4 exist, and why the AMS token must live in a `CSR_DESK_*` environment variable on an https
endpoint.

## 7. Measure, then turn it on

- **Replay first.** Put about 20 closed tickets in `<home>/replay/`, each with an `expected` block written from
  what the CSRs actually did, and run `bin/csr replay <home>/replay`. Readiness requires all of them to agree.
- **Turn it on last.** A person runs `bin/csr live on` in their own terminal. Until then every release and AMS
  write is a dry run.
- **Stop it any time.** `<home>/runtime/STOP` halts every ticket before its next step, and refuses every release
  and AMS push.
