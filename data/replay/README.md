# Replay cases

SAMPLE cases built from the fictional tickets in `data/examples/`, so the harness is tested and the format is
shown. The real evaluation is 20 tickets the agency already closed, each with an `expected` block written
from what the CSRs actually did (see `engine/csrdesk/replay.py`). Real tickets hold client PII: put them in
`data/runtime/replay/` (gitignored), never here.

```bash
bin/csr replay data/replay                 # these samples
bin/csr replay data/runtime/replay         # the agency's closed tickets
```
