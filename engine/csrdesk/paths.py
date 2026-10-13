"""Where an agency's data lives. One install of the desk serves any agency; each agency has its own home.

    home()   1. $CSR_DESK_HOME, if set                     (an installed plugin: set once per agency)
             2. ./csr-desk in the working directory, if it holds agency/agency.json
             3. the repo's own data/ directory                  (development, the demo, the tests)

Layout of a home (bin/csr init creates it):

    agency/agency.json            the agency profile: name, producer, NPN, states and lines written, AMS
    agency/markets.csv            appointed carriers and wholesalers/MGAs
    agency/ams.json               AMS endpoints granted to the agency (the token stays in an env var)
    agency/appetite.proposed.csv  researched appetite awaiting a person's confirmation
    appetite.csv                  confirmed appetite: the only rows CSR 4 uses
    state_rules.csv               optional: the agency's confirmed copy; else the shipped researched rules
    surplus_lines_rules.csv       optional, same
    replay/                       closed tickets with what the CSRs did
    runtime/                      tickets, audit logs, agency.db, the FEIN pepper, desk.json (live switch), STOP

Shipped reference data (state rules, templates, sample tickets, conformance vectors) stays in the plugin.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = ROOT / "data"
ENV = "CSR_DESK_HOME"


class HomeNotSet(RuntimeError):
    pass


def installed() -> bool:
    """True when this code runs from an installed plugin (Claude Code's plugin cache), not a repo checkout."""
    return "/.claude/plugins/" in str(ROOT).replace("\\", "/") or not (ROOT / ".git").exists()


def source() -> str:
    if os.environ.get(ENV):
        return "env"
    if (Path.cwd() / "csr-desk" / "agency" / "agency.json").exists():
        return "cwd"
    return "dev"


def home() -> Path:
    """$CSR_DESK_HOME; else ./csr-desk if it holds an agency; else, only in a git checkout, the repo's data/.
    An installed plugin raises HomeNotSet rather than write agency data into its own replaceable directory."""
    s = source()
    if s == "env":
        return Path(os.environ[ENV]).expanduser()
    if s == "cwd":
        return Path.cwd() / "csr-desk"
    if installed():
        raise HomeNotSet(f"no agency home: set {ENV} (bin/csr init <dir> creates one). Agency data never goes in the plugin.")
    return SHIPPED


def agency_dir() -> Path:
    return home() / "agency"


def runtime() -> Path:
    return home() / "runtime"


def appetite() -> Path:
    return home() / "appetite.csv"


def replay_dir() -> Path:
    return home() / "replay"


def rules_file(name: str) -> Path:
    """The agency's own copy if it has one (compliance confirmed it there), else the shipped researched file."""
    own = home() / name
    return own if own.exists() else SHIPPED / name


def resolve(p, default: Path) -> Path:
    """A given path (relative to the home) or the default."""
    if not p:
        return default
    p = Path(p).expanduser()
    return p if p.is_absolute() else home() / p
