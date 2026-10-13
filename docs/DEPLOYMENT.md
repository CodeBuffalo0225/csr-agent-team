# Deploying the desk into an agency

The desk ships with no agency's data. Deployment means loading four things in this order. Each has a
validator, and until it's loaded the desk says `needs_key` or `needs_data` instead of guessing.

## 1. Markets: carriers and wholesalers

```bash
cp data/agency/markets.example.csv data/agency/markets.csv     # gitignored: the agency's own list
bin/csr setup validate
```

Give one row per market the agency can place with:

| column | values |
|---|---|
| market | name as it should appear on submissions |
| kind | `carrier` · `wholesaler` · `mga` |
| admitted | `Y` / `N` (a non-admitted carrier says in `notes` how it's reached: via a wholesaler, or as surplus lines) |
| appointed | `Y` / `N`; only appointed markets are ever researched or recommended |
| lines | `GL\|AUTO\|WC...` or `*` |
| states | `NJ\|NY` or `*` |
| appetite_url | where its appetite or class guide lives, if known |

Then build the appetite file. Research is proposed by the agent and confirmed by a person:

```bash
bin/csr setup research > data/runtime/research.json     # one task per appointed market
# the csr-04-market subagent works each task: reads the public appetite page, proposes rows with URL + date
bin/csr setup propose data/runtime/proposed.csv          # schema-checked; lands in appetite.proposed.csv, NOT live
bin/csr setup confirm "Market Name" --by "Jane Doe"      # a person confirms one market; rows go live in data/appetite.csv
bin/csr appetite                                         # validates the live file
```

CSR 4 uses only confirmed rows from appointed markets, and admitted carriers come first. When no admitted
market is eligible for a line, the packet opens the wholesale path: the appointed wholesalers/MGAs for that
line and state, plus the state's surplus-lines diligent-effort rule (the declinations come before the
placement).

## 2. State notice and surplus-lines rules

`data/state_rules.csv` and `data/surplus_lines_rules.csv` ship with **NJ and NY researched from the primary
text on 2026-10-13**. Every row carries its source and an empty `confirmed_by`. Until the agency's compliance
owner checks each row and fills `confirmed_by`, every renewal packet shows it as *Confirm: … compliance has
not confirmed it*.

| | New Jersey | New York |
|---|---|---|
| Non-renewal notice | 30–120 days before expiration ([N.J.A.C. 11:1-20.2(b)](https://www.law.cornell.edu/regulations/new-jersey/N-J-A-C-11-1-20-2)) | 60–120 days; excess liability 30–120 ([Ins. Law §3426(e)(3)](https://newyork.public.law/laws/n.y._insurance_law_section_3426)) |
| Cancellation, nonpayment | at least 10 days (11:1-20.2(e)) | 15 days after notice once in force 60 days; 20 days in the first 60 (§3426(b),(c)) |
| Cancellation, other | 30–120 days (11:1-20.2(d)) | 15 days, listed grounds only, after 60 days (§3426(c)) |
| Not covered by these rules | WC/EL, surplus-lines policies, multi-state location risks, retro-rated ([11:1-20.1](https://www.law.cornell.edu/regulations/new-jersey/N-J-A-C-11-1-20-1)) | WC/EL, principally marine, excess-line policies (§3426(l)(2)) |
| Surplus lines diligent effort | declined by 3 NJ-authorized insurers; exportable-list classes exempt ([N.J.A.C. 11:1-33.2](https://www.law.cornell.edu/regulations/new-jersey/N-J-A-C-11-1-33-2)) | affidavit of diligent effort, 3 declinations from authorized insurers, new effort per policy and renewal ([Ins. Law §2118 / Reg 41, DFS OGC opinion](https://www.dfs.ny.gov/insurance/ogco2006/rg060805.htm)) |

Not loaded on purpose: workers' compensation (separate rules in both states), NJ medical malpractice (60-day
rule under 11:1-20.2(m)), NY jumbo risks (30 days; revenue over $100 million), and every other state. Add rows
for the states you write in, each with its source URL and verified date. `bin/csr rules` lists the unconfirmed
states.

## 3. Agency management system

```bash
bin/csr ams status                       # what is missing, and what the vendor requires
bin/csr ams preview <ticket> --system applied_epic|hawksoft|ezlynx    # exact payloads, masked, never sent
```

Applied Epic is the primary target, with HawkSoft and EZLynx alongside. All three gate their APIs:

- **Applied Epic:** the REST platform in the [Applied Dev Center](https://devcenter.myappliedproducts.com).
  Production credentials need Applied's approval and an agency-side SDK/API license. The older SOAP Epic SDK is
  legacy.
- **HawkSoft:** the certified [Partner API](https://blog.hawksoft.com/partner-api-3.0) program. It's vetted and
  excludes competitors ([API terms](https://hawksoft.com/terms/api)); write-back is log notes.
- **EZLynx:** partner or enterprise access provisioned by EZLynx ([partners](https://www.ezlynx.com/partners/automation)).

None publishes an open endpoint reference, so the desk invents none. Once access is granted, the agency's
integration owner writes `data/agency/ams.json` (gitignored) and puts the token in an environment variable,
never in the file:

```json
{"system": "applied_epic", "base_url": "https://<from your approved app>", "token_env": "EPIC_API_TOKEN",
 "paths": {"client": "/<client upsert path>", "note": "/<activity path>"}}
```

`bin/csr ams push <ticket>` writes the ticket note (Epic activity, HawkSoft log note, EZLynx note), plus the
client record when `paths.client` is set. These are WRITE calls, so they need the agency's live switch, and
none of them reaches a carrier. Until then, `bin/csr ams export <dir>` writes masked import files.

## 4. Turn it on, then measure

- `data/runtime/desk.json` with `{"live": true}` is the agency's switch. Without it every release and AMS
  write is a dry run.
- Replay about 20 closed tickets (`data/runtime/replay/`, gitignored) with what your CSRs actually did, and
  run `bin/csr replay data/runtime/replay` before trusting the desk unsupervised.
