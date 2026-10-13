"""Every number the desk enforces lives here (fork-gate rule: a rule with a number in it is code).

Prompts and agent files describe intent and point at these names; they never restate the value.
Change a threshold here and every gate, route and test moves with it.
"""
from __future__ import annotations

# ── Renewals (CSR 6) ──────────────────────────────────────────────────────────
# The spec: CSR 6 tells CSR 5 a renewal is coming 45 days before expiration.
RENEWAL_NOTICE_DAYS = 45
# The full ladder, days before expiration -> (owner, task). 45 is the spec'd client notice; the earlier
# rungs exist because a commercial remarket that starts at 45 days usually cannot finish (see ARCHITECTURE.md).
RENEWAL_LADDER = (
    (120, "CSR2", "order currently-valued loss runs on every expiring line"),
    (120, "CSR9", "pull the expiring declarations pages and baseline limits, deductibles, forms"),
    (90, "CSR10", "review exclusions and endorsements on the expiring forms"),
    (90, "CSR4", "remarket decision; if remarketing, build the target market list"),
    (60, "CSR4", "submissions out to target markets"),
    (RENEWAL_NOTICE_DAYS, "CSR5", "client renewal notice and updated exposure request"),
    (30, "AE", "renewal proposal to the client"),
    (15, "AE", "unbound renewal: escalation"),
)
RENEWAL_ESCALATE_DAYS = 15               # inside this many days an open renewal escalates to the AE
RENEWAL_SCAN_HORIZON_DAYS = 120          # CSR 6 opens a renewal ticket at the first rung

# ── Remarket fork (CSR 6 -> CSR 4) ───────────────────────────────────────────
REMARKET_RATE_CHANGE_PCT = 10.0          # carrier's indicated renewal increase at or above this -> remarket
REMARKET_LOSS_RATIO = 0.60               # incurred / premium over the loss-run window at or above this -> remarket

# ── Loss runs (CSR 2) ─────────────────────────────────────────────────────────
LOSS_RUN_YEARS = 5                       # years of history a submission carries
LOSS_RUN_MAX_AGE_DAYS = 90               # a loss run valued longer ago than this is stale; re-request
LARGE_LOSS_USD = 25_000                  # a single claim at or above this is called out to the AE

# ── Claims (CSR 7) ────────────────────────────────────────────────────────────
CLAIM_URGENT_USD = 25_000                # estimated loss at or above this goes to the AE as URGENT

# ── Market (CSR 4) ────────────────────────────────────────────────────────────
MARKET_CAP = 5                           # carriers on one submission list
MARKET_FLOOR = 60.0                      # weighted market-fit total below this is never submitted to

# ── Client contact (CSR 5) ────────────────────────────────────────────────────
MAX_CLIENT_ASKS = 3                      # information requests to one client on one ticket, then the AE calls

# ── Stop-limiter (every ticket) ───────────────────────────────────────────────
MAX_HOPS = 40                            # agent steps on one ticket, revise cycles included
MAX_VISITS_PER_STEP = 4                  # runs of one step (e.g. CSR5:verify) on one ticket: 1 + MAX_AE_REVISIONS_PER_CSR (CSR 8 exempt)
MAX_PINGPONG = 2                         # A->B->A->B cycles before the pair is stopped
MAX_AE_REVISIONS_PER_CSR = 3             # AE sends work back to the same CSR at most this many times
MAX_AE_REVISIONS_TOTAL = 6               # AE send-backs across the ticket
MAX_MESSAGES = 80                        # bus messages on one ticket (a message storm is a loop too)
TICKET_BUDGET = {"calls": 40, "writes": 12}   # outside calls (public data, connectors) per ticket
WALL_CLOCK_HOURS = {                     # ticket age at which it halts to a human, by type
    "QUOTE": 72, "RENEWAL": 24 * 30, "COI": 4, "SERVICE": 24, "CLAIM": 2, "POLICY_CHECK": 72,
}
KILL_SWITCH = "STOP"                     # <home>/runtime/STOP: if it exists, every ticket halts before its next step

# ── Commercial auto size class (ISO, by GVW in lb) ───────────────────────────
SIZE_CLASSES = (
    (10_000, "light"),
    (20_000, "medium"),
    (45_000, "heavy"),
    (float("inf"), "extra-heavy"),
)

# ── Market filters (CSR 4, hard — run before any judgment) ───────────────────
AM_BEST_ORDER = ("A++", "A+", "A", "A-", "B++", "B+", "B", "B-", "C++", "C+", "C", "C-", "D", "E", "F", "S")
AM_BEST_FLOOR = "A-"                     # a carrier rated below this is never on a submission list
APPETITE_MAX_AGE_DAYS = 365              # an appetite row verified longer ago than this is stale, not used

# ── Claims notice (CSR 7) ─────────────────────────────────────────────────────
LATE_NOTICE_DAYS = 30                    # reported this long after the date of loss -> late-notice flag to the AE
