"""The audit trail: one JSONL line per agent boundary, hash-chained.

Each line carries the sha256 of the line before it, so an edited or deleted line breaks `verify`. What a
regulator or an E&O carrier asks after a bad outcome is "what did the agency know, and who decided": every
step records the rule that routed it, its status and summary, the hash of what it saw and produced, and
the budget used. PII never enters: the orchestrator logs the masked ticket's hash, not the ticket.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

GENESIS = "0" * 64


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return json.loads(lines[-1])["hash"] if lines else GENESIS

    def append(self, entry: dict) -> dict:
        prev = self._last_hash()
        body = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **entry, "prev": prev}
        body["hash"] = digest(body)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(body, sort_keys=True, default=str) + "\n")
        return body

    def verify(self) -> dict:
        if not self.path.exists():
            return {"ok": True, "lines": 0}
        prev = GENESIS
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            rec = json.loads(line)
            claimed = rec.pop("hash")
            if rec["prev"] != prev or digest(rec) != claimed:
                return {"ok": False, "lines": len(lines), "broken_at": i}
            prev = claimed
        return {"ok": True, "lines": len(lines)}
