# csr-agent-team

An agentic CSR desk for a commercial insurance agency. Ten CSR agents and an AE feedback loop, with
if/else routing and stop-limiters enforced in code. Drafts only.

```bash
bin/csr demo                                   # run every sample ticket end to end
python3 -m unittest discover -s tests          # 151 tests, incl. 63 spine-spec vectors
```

- Architecture, routing rules, limiters, improvements: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Repo rules: [AGENTS.md](AGENTS.md)
- Agents: [.claude/agents/](.claude/agents/) · orchestrator: [.claude/commands/csr.md](.claude/commands/csr.md)

To use your own markets, copy `data/appetite.example.csv` to `data/appetite.csv` and fill it from your
carrier appetite guides (it is gitignored). Without it, CSR 4 reports `needs_key` instead of guessing.
