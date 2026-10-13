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

## Load your agency's data (all gitignored once filled)
1. `data/appetite.csv` from your carrier appetite guides (template `data/appetite.example.csv`) → `bin/csr appetite`
2. `data/state_rules.csv` with verified notice periods and their sources (template `data/state_rules.example.csv`) → `bin/csr rules`
3. Closed tickets with what your CSRs did, in `data/runtime/replay/` → `bin/csr replay data/runtime/replay`
4. Your AMS connector → `bin/csr ams status`; until then `bin/csr ams export <dir>`

Without each, the desk says `needs_key` / `needs_data` instead of guessing. See docs/ARCHITECTURE.md §8.
