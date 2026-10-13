# csr-agent-team

An agentic CSR desk for a commercial insurance agency. Eleven CSR agents and an AE feedback loop, with
if/else routing and stop-limiters enforced in code. Drafts only.

```bash
bin/csr demo                                   # run every sample ticket end to end
python3 -m unittest discover -s tests          # full suite, incl. 63 spine-spec vectors
bin/csr replay data/replay                     # replay harness on the SAMPLE cases
```

- Architecture, routing rules, limiters, improvements: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Repo rules: [AGENTS.md](AGENTS.md)
- Agents: [.claude/agents/](.claude/agents/) · orchestrator: [.claude/commands/csr.md](.claude/commands/csr.md)

## Deploying into an agency
See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md):
1. markets (appointed carriers + wholesalers/MGAs) → researched appetite → a person confirms it (`bin/csr setup ...`)
2. NJ + NY notice and surplus-lines rules ship researched and cited; compliance confirms them (`bin/csr rules`)
3. Applied Epic (primary), HawkSoft or EZLynx once the vendor grants API access (`bin/csr ams ...`)
4. replay ~20 closed tickets (`bin/csr replay`) before trusting it unsupervised

Without each, the desk says `needs_key` / `needs_data` instead of guessing.
