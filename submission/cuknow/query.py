"""Question analysis: what is being asked, about which product, with which qualifiers, and when."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date

from .products import qualifiers, question_families
from .text import (DATE_LONG_RE, MONTHS, MONTH_YEAR_RE, RATE_WORDS, money_values, pct_values, stem,
                   term_months, tokens)

# Query-side synonyms: token -> extra tokens (weight 0.6). Generic banking vocabulary only.
SYNONYMS = {
    "cd": ["certificate"], "cds": ["certificate"], "car": ["auto", "vehicle"], "vehicle": ["auto"],
    "truck": ["auto", "vehicle"], "mortgage": ["home", "loan"], "house": ["home"],
    "heloc": ["home", "equity", "line", "credit"], "atm": ["atm", "location"],
    "locator": ["location", "find", "branch"], "hour": ["lobby", "drive"], "open": ["hour", "join"],
    "join": ["member", "membership"], "membership": ["join"], "apply": ["application"], "eligible": ["eligibility", "member", "membership", "charter", "live", "work"],
    "eligibility": ["member", "membership", "charter"], "phone": ["call", "contact"], "address": ["location"],
    "fee": ["charge", "cost"], "cost": ["fee"], "branche": ["branch", "location"], "branch": ["location", "office"],
    "wallet": ["apple", "google", "samsung", "pay"], "student": ["loan"],
    "university": ["student", "college", "school"], "college": ["student"], "employee": ["staff", "crew"],
    "volunteer": ["vto"], "vto": ["volunteer"], "routing": ["aba"], "update": ["change"],
    "contact": ["call", "phone"], "recognition": ["award", "ranked", "named", "recognized"],
    "award": ["recognition", "recognized", "ranked"], "close": ["closure", "closing"],
    "closing": ["closure", "close"], "member": ["membership", "join"], "app": ["mobile"], "online": ["shine"],
}

SYNONYMS = {stem(k): [stem(x) for x in v] for k, v in SYNONYMS.items()}

LINK_RE = re.compile(r"\b(link|url|web ?address|web ?page|webpage|websites? (for|to)|page (for|to|where)|site (for|to))\b", re.I)
HOURS_RE = re.compile(r"\b(hours?|what time|open on (saturday|sunday)|open (late|early)|schedule)\b", re.I)
CHANGE_RE = re.compile(r"\b(chang(e|ed|es)|previous(ly)?|used to|before|history|increase[ds]?|decrease[ds]?|"
                       r"went (up|down)|go(ne)? (up|down)|raised?|lowered|old(er)? rate|compare[ds]? (to|with)|"
                       r"since (may|june|july|august|september))\b", re.I)
SUPER_MAX = re.compile(r"\b(highest|best|top|maximum|max|most)\b", re.I)
SUPER_MIN = re.compile(r"\b(lowest|cheapest|minimum rate|least)\b", re.I)
RATE_INTENT = re.compile(r"\b(rates?|aprs?|apys?|dividends?|interest|yields?|percent(age)?|earn(s|ing)?)\b|%|"
                         r"\bpays? (on|for)\b|\bhow much (does|do|will|would|can)\b.*\b(pay|earn)|"
                         r"\bstill (available|offered)|no longer (available|offered)|"
                         r"discontinued", re.I)
LIST_RE = re.compile(r"\b(all|list|every|options|types|kinds|which \w+s\b|what \w+s (do|does)|do you offer)\b|"
                     r"\b(cards|loans|certificates|accounts|cds|rates)\b", re.I)
YESNO_RE = re.compile(r"^\s*(do|does|is|are|can|will|has|have)\b", re.I)


@dataclass
class QInfo:
    raw: str
    low: str
    content: list[str]
    weights: dict[str, float]
    families: list[str]
    quals: set[str]
    terms: list[int]
    years: list[int]
    amounts: list[float]
    pcts: list[float]
    rate_intent: bool
    link_intent: bool
    hours_intent: bool
    change_intent: bool
    superlative: str | None
    list_mode: bool
    yesno: bool
    asked_date: date | None = None
    asked_month: int | None = None
    branch: str | None = None
    extra: dict = field(default_factory=dict)


_MONTH_ALT_Q = "|".join(sorted((k for k in MONTHS if len(k) > 3 or k in ("jan", "feb", "may", "aug", "sep", "oct", "nov", "dec")),
                               key=len, reverse=True))


def _end_of_month(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def date_in_question(q: str) -> date | None:
    """'as of May 2026' -> 2026-05-31; 'on June 3, 2026' -> 2026-06-03."""
    m = DATE_LONG_RE.search(q)
    if m:
        try:
            return date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            pass
    m = MONTH_YEAR_RE.search(q)
    if m:
        return _end_of_month(int(m.group(2)), MONTHS[m.group(1).lower()])
    return None


MONTH_ONLY_RE = re.compile(rf"\b(?:in|as of|during|back in|on|of)\s+(?:early |late |mid-?)?({_MONTH_ALT_Q})\b(?![\s,]*\d)",
                           re.I)


def month_in_question(q: str) -> int | None:
    """'what was it in June?' -> 6 (the year is resolved against the snapshots)."""
    m = MONTH_ONLY_RE.search(q)
    return MONTHS[m.group(1).lower()] if m else None


BRANCH_RE = re.compile(r"\b(?:the\s+|our\s+|your\s+)?((?:[A-Z][\w.\-’']*\s+){0,2}[A-Z][\w.\-’']*)\s+(?:branch|office)\b")
NOT_BRANCH = {"Dupaco", "The", "Which", "What", "Each", "Every", "Any", "All", "General", "Main", "Your", "Our",
              "Do", "Does", "Is", "Are", "Can", "When", "Where", "Who", "How"}


def analyze(q: str) -> QInfo:
    low = q.lower().replace("’", "'")
    content = tokens(q)
    weights: dict[str, float] = {}
    for t in content:
        weights[t] = weights.get(t, 0) + 1.0
    # proper nouns (Forbes, Galena, Hy-Vee...) carry the meaning of a question: weight them up
    for m in re.finditer(r"(?<![.?!]\s)(?<!^)\b([A-Z][\w$\-]+)", q):
        for t in tokens(m.group(1)):
            if t in weights:
                weights[t] = 2.0
    for t in list(content):
        for s in SYNONYMS.get(t, []):
            if s not in weights:
                weights[s] = 0.5
    fams = question_families(low)
    years = [int(y) for y in re.findall(r"\b(19[89]\d|20[0-3]\d)\b", low)] if re.search(
        r"model|vehicle|car\b|auto|truck|atv|snowmobile|motorcycle|\b(19|20)\d\d (ford|chevy|toyota|honda)", low) else []
    amounts = money_values(q)
    if not amounts:
        # "balance of 10,000" without a dollar sign
        amounts = [float(x.replace(",", "")) for x in re.findall(r"\b(\d{1,3}(?:,\d{3})+)\b", q)]
    branch = None
    for m in BRANCH_RE.finditer(q):
        name = m.group(1).strip()
        words = [w for w in name.split() if w not in NOT_BRANCH]
        if words:
            branch = " ".join(words)
    qi = QInfo(
        raw=q, low=low, content=content, weights=weights, families=fams, quals=qualifiers(low),
        terms=term_months(low), years=years, amounts=amounts, pcts=pct_values(low),
        rate_intent=bool(RATE_INTENT.search(low)),
        link_intent=bool(LINK_RE.search(low)),
        hours_intent=bool(HOURS_RE.search(low)),
        change_intent=bool(CHANGE_RE.search(low)),
        superlative="max" if SUPER_MAX.search(low) else ("min" if SUPER_MIN.search(low) else None),
        list_mode=bool(LIST_RE.search(low)),
        yesno=bool(YESNO_RE.search(low)),
        asked_date=date_in_question(q),
        asked_month=month_in_question(q),
        branch=branch,
    )
    return qi


def rate_words_only(toks: list[str]) -> list[str]:
    return [t for t in toks if t not in RATE_WORDS]
