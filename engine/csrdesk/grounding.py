"""Grounding gate (spine S5, spine-spec v1.1.0): which figures in agent prose are allowed to stand.

One ungrounded figure fails the whole text, and the text is discarded, not patched. In this desk
that means: a client email, a loss-run summary or an AE packet note that states a limit, premium,
loss amount or date-count the engine did not compute or read from the account record is rejected
whole. A model that "rounds" a $1,000,000 limit to "$2M" never reaches the AE.

Copied from northpike's claims.py (itself copied from cyera-bdr); the figure rules are verbatim and
must stay so, because every spine copy passes the same vectors. Renamed to grounding.py here because
"claims" means insurance claims in this repo. This module imports nothing from the repo.

Public API
    normalize_text(text)        NFKC-fold, straighten quotes, drop invisible characters
    ground(text, allowed)       -> {"ok": bool, "ungrounded": [figure as written, in order]}
    extract_figures(text)       -> [(as written, normalized key)]
    normalize_allowed(items)    -> set of keys; each item may be a whole quote, not only a figure

`allowed` holds whatever the caller can trace a figure to: quotes from fetched pages,
numbers the engine computed. Every figure in `text` must normalize to a key found in them.

Known sharp edge: a street number ("504 Fifth Ave"), a route ("Route 46") or any other bare
number of ten or more is a figure. A caller that renders a fixed footer or an address must
pass those numbers in `allowed` or strip that text before grounding. The rule is heuristic;
a miss in the safe direction costs a rewrite.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

# ── Text normalisation ────────────────────────────────────────────────────────

# Soft hyphen, zero-width and bidi-control characters, word joiner and invisible operators, BOM.
# Written as escapes so the source carries no invisible characters of its own.
_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_FOLD = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
                       "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2011": "-", "\u2010": "-"})


def normalize_text(text: str) -> str:
    """NFKC folds full-width digits and compatibility forms; curly quotes become
    straight; invisible characters are removed ('Var\\u200bonis' is 'Varonis')."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text)).translate(_FOLD)


# ── Figure extraction (spine S5, v1.1.0) ──────────────────────────────────────

_SCALE = {"thousand": 10**3, "k": 10**3, "million": 10**6, "m": 10**6, "mm": 10**6,
          "billion": 10**9, "b": 10**9, "bn": 10**9, "trillion": 10**12, "t": 10**12}
_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
           "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
_NUMWORD = "one|two|three|four|five|six|seven|eight|nine"
_WORDNUM = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten".split())}
_WORDNUM["hundred"] = 100
_RATIO_TOKEN = r"\d+|one|two|three|four|five|six|seven|eight|nine|ten|a hundred|hundred"
_FRACTION = "half|halves|third|thirds|quarter|quarters|fifth|fifths|tenth|tenths"
_CALENDAR_PREFIX = {"first", "second", "third", "fourth", "last", "next", "this", "back", "front",
                    "latter", "other", "same"}
_YEAR_BEFORE = re.compile(r"\b(?:in|by|since|from|until|through|before|after|during|early|late|mid|fiscal|fy)\s*-?\s*$", re.I)
_YEAR_AFTER = re.compile(r"^\s*(?:$|[.,;:!?)]|budget|plans?|planning|roadmap|priorit|goals?|research|report|survey|study|season|cycle|and\b|or\b)", re.I)
_LOOKBACK = re.compile(r"\b(?:last|past|next|coming)\s*$", re.I)
_DURATION_UNIT = re.compile(r"^\s*-?\s?(?P<u>minutes?|mins?|hours?|hrs?|business days?|days?|weeks?|wks?|months?|mos?|years?|yrs?)\b", re.I)

# Stripped before extraction: things that contain digits but are not claims.
_NOT_FIGURES = [
    re.compile(r"https?://\S+"),
    re.compile(r"\b\S+@\S+\.\w+\b"),
    re.compile(r"\+\d{1,3}[\s.\-]?(?:\(?\d{1,4}\)?[\s.\-]?){2,5}\d{2,4}\b"),         # international phone
    re.compile(r"\(?\b\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"),                          # US phone
    re.compile(r"\b[A-Z]{2}\s\d{5}(?:-\d{4})?\b"),                                   # "NY 10119"
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                                             # ISO date
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),                                       # 9/23/2026
    re.compile(r"\b(?:ISO|IEC|NIST(?:\s+SP)?|SP|SOC|PCI(?:\s+DSS)?|Part|Article|Art\.|Annex|Section|Item|"
               r"Rule|Form|CFR|Title|Req(?:uirement)?|§)\s*\d[\d.\-/]*", re.I),       # regulatory references
    re.compile(r"\b\d+\s+(?:NYCRR|CFR|U\.?S\.?C\.?)(?:\s+[\d.]+)?\b"),
    re.compile(r"\b\d{1,2}:\d{2}\b"),                                                 # clock times
    re.compile(r"\b\d{1,2}\s?(?:am|pm)\b", re.I),
    re.compile(r"\b24\s?[/x]\s?7\b", re.I),                                           # idiom
    re.compile(r"\b(?:Fortune|Global|Forbes Global|Inc\.?|S&P|Russell)\s+\d[\d,]*\b"),  # named lists
    re.compile(r"\b(?:Microsoft|Office|Windows|Dynamics|Workspace|iOS|Android|macOS|Python|Java|Web)\s?\d+(?:\.\d+)?\b"),
    re.compile(r"\b\d+x\d+\b", re.I),                                                 # 8x8, 4x4
    re.compile(r"\b(?:3M|23andMe|7-Eleven)\b"),
    re.compile(r"\bfirst\s+(?:30|60|90|100)\s+days\b", re.I),                          # new-leader idiom
]

_FIG = re.compile(
    r"(?P<cur>\$)?\s?(?P<num>\d(?:[\d,]*\d)?(?:\.\d+)?)"
    r"(?:\s?(?P<scale>thousand|million|billion|trillion|bn|mm|[kmbt])\b)?"
    r"(?P<plus>\+)?"
    r"(?:\s?(?P<unit>%|percent\b|[x×](?![a-z0-9])))?"
    r"(?P<ordinal>st|nd|rd|th)?",
    re.I,
)
_MULTIPLIER_WORD = re.compile(
    r"\b((?:doubl|tripl|quadrupl)(?:e|ed|es|ing)(?![\s\-]+(?:check|click|down|tap)\b)"
    r"|(?:two|three|four|five|six|seven|eight|nine|ten|hundred|\d+)-?fold"
    r"|twice(?=\s+as\b))\b", re.I)
_TIMES = re.compile(r"\b(?P<n>\d+(?:\.\d+)?|two|three|four|five|six|seven|eight|nine|ten|hundred)\s+times\b", re.I)
_PREFIX_X = re.compile(r"(?<![A-Za-z0-9])[x×](?P<n>\d+(?:\.\d+)?)\b", re.I)
_RATIO = re.compile(rf"\b(?P<a>{_RATIO_TOKEN})\s+(?:in|out of)\s+(?P<b>{_RATIO_TOKEN})\b", re.I)
_SLASH = re.compile(r"\b(?P<a>\d{1,2})/(?P<b>\d{1,2})\b(?=\s+[A-Za-z])")
_SLASH_ANY = re.compile(r"\b\d{1,2}/\d{1,2}\b")
_RANK = re.compile(r"#\s?(?P<n>\d+)\b")
_MAGNITUDE = re.compile(r"\b(?P<w>hundreds|thousands|millions|billions|dozens)\s+of\b", re.I)
_FRACTION_HYPHEN = re.compile(rf"\b(?:{_NUMWORD})-(?:{_FRACTION})\b", re.I)
_FRACTION_OF = re.compile(rf"\b(?P<prev>\w+\s+)?(?P<word>{_FRACTION})\s+of\b", re.I)
_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_TEENS = "ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
_SPELLED_PCT = re.compile(
    rf"\b(?:(?:{_TENS})(?:[\s\-](?:{_NUMWORD}))?|{_TEENS}|{_NUMWORD}|a hundred|one hundred)\s+percent\b", re.I)


def _num(tok: str) -> Decimal:
    t = tok.lower().strip()
    if t in ("a hundred", "hundred"):
        return Decimal(100)
    return Decimal(_WORDNUM[t]) if t in _WORDNUM else Decimal(t)


def _normalize_word(w: str) -> str:
    w = w.lower()
    for root in ("doubl", "tripl", "quadrupl"):
        if w.startswith(root):
            return root + "e"
    return w


def _dur_unit(u: str) -> str:
    u = u.lower()
    for full in ("minute", "hour", "day", "week", "month", "year"):
        if u.startswith(full[:2]) or u.startswith(full):
            return full
    if u.startswith("wk"):
        return "week"
    if u.startswith("hr"):
        return "hour"
    if u.startswith("mo"):
        return "month"
    if u.startswith("yr"):
        return "year"
    return u


def extract_figures(text: str) -> list[tuple[str, tuple]]:
    """Return [(as_written, normalized_key)] in order of appearance."""
    clean = normalize_text(text)
    for pat in _NOT_FIGURES:
        clean = pat.sub(lambda m: " " * len(m.group(0)), clean)
    found: list[tuple[int, str, tuple]] = []
    taken: list[tuple[int, int]] = []

    def claim(span, written, key):
        found.append((span[0], written, key))
        taken.append(span)

    def free(a, b=None):
        b = a + 1 if b is None else b
        return not any(s < b and a < e for s, e in taken)

    for m in _SPELLED_PCT.finditer(clean):
        claim(m.span(), m.group(0), ("word", m.group(0).lower()))
    for m in _RATIO.finditer(clean):
        if free(*m.span()):
            claim(m.span(), m.group(0), ("ratio", _num(m.group("a")), _num(m.group("b"))))
    for m in _SLASH.finditer(clean):
        a, b = int(m.group("a")), int(m.group("b"))
        if b <= 10 and a <= b and free(*m.span()):
            claim(m.span(), m.group(0), ("ratio", Decimal(a), Decimal(b)))
    for m in _SLASH_ANY.finditer(clean):          # every other N/M is a date, not a claim
        if free(*m.span()):
            taken.append(m.span())
    for m in _RANK.finditer(clean):
        claim(m.span(), m.group(0).replace(" ", ""), ("rank", Decimal(m.group("n"))))
    for m in _MAGNITUDE.finditer(clean):
        claim(m.span("w"), m.group("w"), ("word", m.group("w").lower()))
    for m in _TIMES.finditer(clean):
        if free(*m.span()):
            claim(m.span(), m.group(0), ("x", _num(m.group("n")).normalize()))
    for m in _PREFIX_X.finditer(clean):
        if free(*m.span()):
            claim(m.span(), m.group(0), ("x", Decimal(m.group("n")).normalize()))
    for m in _FRACTION_HYPHEN.finditer(clean):
        claim(m.span(), m.group(0), ("word", m.group(0).lower()))
    for m in _FRACTION_OF.finditer(clean):
        prev = (m.group("prev") or "").strip().lower()
        if prev in _CALENDAR_PREFIX or not free(m.start("word")):
            continue
        claim(m.span("word"), m.group("word"), ("word", m.group("word").lower()))
    for m in _MULTIPLIER_WORD.finditer(clean):
        claim(m.span(), m.group(0), ("word", _normalize_word(m.group(0))))

    for m in _FIG.finditer(clean):
        if not free(m.start("num")):
            continue
        start = m.start("num") if not m.group("cur") else m.start()
        # A digit glued to a letter on its left (Q4, H2, E5, M365) is a label.
        if m.start("num") > 0 and clean[m.start("num") - 1].isalpha():
            continue
        try:
            value = Decimal(m.group("num").replace(",", ""))
        except InvalidOperation:
            continue
        cur, scale, unit = m.group("cur"), (m.group("scale") or "").lower(), (m.group("unit") or "").lower()
        if scale:
            value *= _SCALE[scale]
        written = clean[start:m.end()].strip()
        after = clean[m.end():]
        before = clean[max(0, start - 16):start]
        if unit in ("%", "percent"):
            key = ("pct", value.normalize())
        elif unit in ("x", "×"):
            key = ("x", value.normalize())
        elif cur:
            key = ("usd", value.normalize())
        elif scale:
            key = ("num", value.normalize())
        else:
            if m.group("ordinal"):
                continue
            raw = m.group("num")
            d = _DURATION_UNIT.match(after)
            if d:
                u = _dur_unit(d.group("u"))
                if u == "minute" and value <= 60:
                    continue                                        # a meeting length
                if _LOOKBACK.search(before):
                    continue                                        # "over the last 12 months"
                found.append((start, f"{raw} {d.group('u')}".replace("  ", " "), ("dur", value.normalize(), u)))
                continue
            if "," not in raw and "." not in raw and len(raw) == 4 and 1900 <= int(raw) <= 2100:
                if _YEAR_BEFORE.search(before) or _YEAR_AFTER.match(after) or re.search(r"'s\s*$", before):
                    continue                                        # a year used as a year
            elif value < 10:
                continue                                            # counting, not a claim
            if re.search(rf"\b(?:{_MONTHS})\.?\s*$", before.lower()) or re.match(rf"^\s*(?:{_MONTHS})\b", after.lower()):
                continue                                            # a day of a named month
            key = ("num", value.normalize())
            written = raw + (m.group("plus") or "")
        found.append((start, written.rstrip("+") if key[0] == "num" else written, key))

    found.sort(key=lambda f: f[0])
    return [(w, k) for _, w, k in found]


def normalize_allowed(items) -> set:
    keys = set()
    for item in items:
        for _, key in extract_figures(str(item)):
            keys.add(key)
        s = str(item).strip()
        if re.fullmatch(r"\d+", s):
            keys.add(("num", Decimal(s).normalize()))
    return keys


def ground(text: str, allowed) -> dict:
    """Spine S5. Returns {ok, ungrounded:[as written]}."""
    allowed_keys = normalize_allowed(allowed)
    ungrounded = [w for w, k in extract_figures(text) if k not in allowed_keys]
    return {"ok": not ungrounded, "ungrounded": ungrounded}
