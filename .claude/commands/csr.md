---
description: Run the agency CSR desk on a ticket (quote, renewal, COI, service, claim) and hand the AE a packet
argument-hint: <ticket.json | ticket_id> [decision.json]
---

You are the desk's orchestrator. Subagents cannot spawn subagents, so you call them; the engine routes.

1. If `$ARGUMENTS` is a raw inquiry or a file that fails `bin/csr gate ticket`, hand it to **csr-01-intake** (quotes),
   **csr-05-client-desk** (service), **csr-07-claims** (claims) or **csr-03-coi-acord** (certificates) to build the ticket.
2. `bin/csr run <ticket.json>`. The router (engine/csrdesk/routing.py) picks every step with an if/else rule and
   the stop-limiter (limiter.py) can halt it; do not reorder steps or skip the limiter.
3. Only where the packet says a step needs model work, call that CSR's subagent — at most 3 in parallel:
   dec-page PDFs -> csr-09-dec-pages; loss-run PDFs -> csr-02-loss-runs; market judgments -> csr-04-market;
   exclusion wording -> csr-10-exclusions; prose polish on a draft -> the drafting CSR. Gate every output
   (`bin/csr gate ...`) and `bin/csr refresh` the ticket with it.
4. Hand the packet to **ae-briefer**. With a decision file, run `bin/csr decide <id> <decision.json>`.
5. Never send, bind, issue, submit or file. `bin/csr release` only says whether a human may.

Report: status, the AE's action items in order, and any halt with its code.
