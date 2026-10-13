# csr-desk

An agentic CSR desk that plugs into any commercial-lines insurance agency. Eleven CSR agents (intake, loss runs,
COI/ACORD, markets, client desk, renewals, claims, records, dec pages, exclusions, policy checking) and an AE
feedback loop. Routing, stop-limiters, gates and arithmetic are deterministic code; the desk is drafts-only until
the agency turns it live, and every irreversible act needs a person.

```text
/plugin marketplace add CodeBuffalo0225/csr-agent-team
/plugin install csr-desk@csr-desk
/csr-setup                      # guided onboarding for the agency
/csr-readiness                  # READY, or exactly what is left
/csr <ticket.json>              # work a ticket to the AE packet
```

Each agency's data lives in its own home (`CSR_DESK_HOME`), never in the plugin. It ships:
- NJ, NY and CT notice rules and NJ, NY and PA surplus-lines rules, each researched and cited, awaiting the
  agency's compliance sign-off.
- Applied Epic (primary), HawkSoft and EZLynx adapters.
- A fail-closed safety hook.

```bash
bin/csr demo                                   # every sample ticket end to end (real NHTSA + Census calls)
python3 -m unittest discover -s tests          # full suite, incl. 63 spine-spec vectors
```

- Deploying into an agency: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- Architecture, routing, limiters, review log: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Repo rules: [AGENTS.md](AGENTS.md) · agents: [agents/](agents/) · commands: [commands/](commands/) · hook: [hooks/](hooks/)
