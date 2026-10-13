"""CSR 8 — the database. The only writer.

Every other agent proposes; CSR 8 writes. One writer means one dedup rule, one place PII is masked
before storage, and one append-only event log — instead of ten agents each half-updating a record.

SQLite (stdlib) at data/runtime/agency.db by default; the agency's AMS (Applied Epic, HawkSoft, EZLynx,
QQCatalyst, NowCerts) is the production target, reached through a connector whose write tools
policy.py classifies WRITE. This file is the local system of record until that connector is wired.

Dedup, first match wins: FEIN digits (hashed, never stored raw) -> normalized insured name + ZIP.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "runtime" / "agency.db"
WRITER = "CSR8"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  account_key TEXT PRIMARY KEY, fein_hash TEXT, name_key TEXT NOT NULL, insured_name TEXT NOT NULL,
  record TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS accounts_fein ON accounts(fein_hash);
CREATE INDEX IF NOT EXISTS accounts_name ON accounts(name_key);
CREATE TABLE IF NOT EXISTS tickets (
  ticket_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL,
  packet TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, ticket_id TEXT,
  account_key TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS events_append_only_u BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_append_only_d BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""

_SUFFIX = re.compile(r"\b(llc|l\.l\.c\.|inc|incorporated|corp|corporation|co|company|ltd|lp|llp|pc|pllc)\b\.?", re.I)


def name_key(name: str, zip_code: str = None) -> str:
    n = _SUFFIX.sub("", name.lower())
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    return f"{n}|{(zip_code or '')[:5]}"


PEPPER_ENV = "CSR_DESK_PEPPER"
PEPPER_FILE = ROOT / "data" / "runtime" / "pepper"


def _pepper() -> bytes:
    """A per-install secret so a FEIN digest cannot be brute-forced (there are only 10^9 FEINs; a plain
    SHA-256 of one falls in a fraction of a second). From $CSR_DESK_PEPPER, else a random key created once in
    data/runtime/pepper (gitignored, owner-only). Losing it only breaks dedup against older records."""
    env = os.environ.get(PEPPER_ENV)
    if env:
        return env.encode()
    if not PEPPER_FILE.exists():
        PEPPER_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(PEPPER_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return PEPPER_FILE.read_text().strip().encode()


def fein_hash(fein: str) -> str:
    """Computed once at intake from the raw FEIN; only this keyed digest travels or is stored."""
    digits = re.sub(r"\D", "", fein or "")
    return hmac.new(_pepper(), digits.encode(), hashlib.sha256).hexdigest() if len(digits) == 9 else None


class Store:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def _now(self) -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    def find(self, insured_name: str, zip_code: str = None, fein_digest: str = None):
        fh = fein_digest
        if fh:
            row = self.db.execute("SELECT account_key FROM accounts WHERE fein_hash = ?", (fh,)).fetchone()
            if row:
                return row[0], "fein"
        # name + ZIP matches only when FEINs cannot disagree: two different FEINs are two legal entities,
        # however alike the names ("Acme Roofing LLC" and "Acme Roofing Inc")
        for key, stored in self.db.execute("SELECT account_key, fein_hash FROM accounts WHERE name_key = ?",
                                           (name_key(insured_name, zip_code),)):
            if not (fh and stored and fh != stored):
                return key, "name_zip"
        return None, None

    def write(self, actor: str, *, fein_digest: str, masked_account: dict, ticket_id: str, ticket_type: str,
              status: str, packet: dict) -> dict:
        """Upsert the account (masked) and the ticket, append an event. Raises PermissionError for any
        actor but CSR 8. `fein_digest` is fein_hash() of the raw FEIN; the raw value never reaches the store."""
        if actor != WRITER:
            raise PermissionError(f"{actor} cannot write: CSR 8 is the only writer (AGENTS.md hard rule 5)")
        zip_code = (masked_account.get("mailing_address") or {}).get("zip")
        key, matched_on = self.find(masked_account["insured_name"], zip_code, fein_digest)
        now = self._now()
        record = json.dumps(masked_account, sort_keys=True)
        with self.db:
            if key is None:
                seed = name_key(masked_account["insured_name"], zip_code) + (fein_digest or "")
                key = hashlib.sha256(seed.encode()).hexdigest()[:16]
                self.db.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?)",
                                (key, fein_digest, name_key(masked_account["insured_name"], zip_code),
                                 masked_account["insured_name"], record, now, now))
                action = "created"
            else:
                self.db.execute("UPDATE accounts SET record = ?, insured_name = ?, updated = ?, "
                                "fein_hash = COALESCE(fein_hash, ?) WHERE account_key = ?",
                                (record, masked_account["insured_name"], now, fein_digest, key))
                action = f"updated (matched on {matched_on})"
            self.db.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?) ON CONFLICT(ticket_id) DO UPDATE SET "
                            "status = excluded.status, packet = excluded.packet, updated = excluded.updated",
                            (ticket_id, key, ticket_type, status, json.dumps(packet, sort_keys=True, default=str), now))
            self.db.execute("INSERT INTO events (ts, actor, ticket_id, account_key, kind, detail) VALUES (?,?,?,?,?,?)",
                            (now, actor, ticket_id, key, "write", json.dumps({"action": action, "status": status})))
        return {"account_key": key, "action": action}

    def events(self, ticket_id: str = None) -> list:
        q, args = "SELECT ts, actor, ticket_id, kind, detail FROM events", ()
        if ticket_id:
            q, args = q + " WHERE ticket_id = ?", (ticket_id,)
        return [dict(zip(("ts", "actor", "ticket_id", "kind", "detail"), r)) for r in self.db.execute(q + " ORDER BY id", args)]
