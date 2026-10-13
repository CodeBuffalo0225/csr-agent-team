@AGENTS.md

## Claude Code
- `/csr <ticket.json>` is the orchestrator (`.claude/commands/csr.md`); subagents live in `.claude/agents/` (one per CSR + ae-briefer). Subagents cannot spawn subagents. At most 3 run in parallel.
- Name at most 3 skills per task: production-agent-design, fork-gate, schema-gate.
