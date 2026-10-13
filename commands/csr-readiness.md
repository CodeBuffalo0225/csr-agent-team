---
description: "Show whether this agency's CSR desk is ready to run, and exactly what is left"
---

Run `${CLAUDE_PLUGIN_ROOT}/bin/csr readiness` and show the checklist exactly as printed. For each open step, give its
`->` fix in one line. Do not mark anything done that the checklist does not mark done, and do not perform the
human-only steps (setup confirm, rules confirm, the live switch) yourself.
