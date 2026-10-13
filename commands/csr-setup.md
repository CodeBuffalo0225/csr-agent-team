---
description: "Set up the CSR desk for a new agency — profile, markets, researched appetite, state rules, AMS — and show what is left"
argument-hint: "[agency home directory]"
---

# /csr-setup — plug the desk into an agency

`CLI = ${CLAUDE_PLUGIN_ROOT}/bin/csr` · the agency's data lives in its home (`$CSR_DESK_HOME`), never in the plugin.

1. **Home.** If `$CSR_DESK_HOME` is not set, run `CLI init $ARGUMENTS` (default `./csr-desk`) and tell the user to add
   `export CSR_DESK_HOME=<that path>` to their shell. `init` never overwrites an existing file.
2. **Profile.** Walk the user through `<home>/agency/agency.json`: agency name, address, phone, email, producer name,
   producer NPN and license state, states and lines written, AMS (`applied_epic`, `hawksoft`, `ezlynx` or `none`),
   compliance owner. Ask for each value; never fill one in from memory or the web. Then run `CLI readiness`.
3. **Markets.** Ask which carriers the agency is appointed with and which wholesalers/MGAs it uses, admitted or not,
   and for which lines and states. Write `<home>/agency/markets.csv`, then `CLI setup validate`.
4. **Appetite research.** Run `CLI setup research`. For each task, dispatch **csr-04-market** (at most 3 at once) to
   read that market's public appetite page and propose rows. Run `CLI setup propose <file>` on what comes back.
   Then stop: a person at the agency runs `CLI setup confirm "<market>"` in their own terminal (it asks their name and
   the market typed back). The hook refuses it from you, and the engine refuses it without a terminal.
5. **State rules.** `CLI rules` lists the states whose rules are researched but unconfirmed. Show the compliance owner
   the source URL on each row. They confirm with `CLI rules confirm <STATE>`, again in their own terminal. For a state the agency writes in that is not loaded, say so; do not supply a notice period.
6. **AMS.** `CLI ams status` names what the vendor requires (Applied Epic: Dev Center approval and an SDK/API license;
   HawkSoft: certified partner access; EZLynx: partner/enterprise access). Nothing is configured until the agency has it.
7. **Finish** with `CLI readiness`: show the checklist exactly as printed. The live switch stays off; a person turns
   it on last with `CLI live on` in their own terminal, and can run `CLI lock` to make the human-only files immutable.
