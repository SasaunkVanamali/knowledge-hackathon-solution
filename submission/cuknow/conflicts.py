"""Find places where the website contradicts itself, decide which value is right, and say why.

Three kinds of contradiction are reported:
  1. stale copies  - a calculator / landing page / guide shows a different number than the official
                     rates page for the same product, term and tier (latest snapshot)
  2. changes       - the official rates page changed between snapshots, so older copies and older
                     answers disagree with today's value; the newest snapshot wins
  3. old releases  - press-release boilerplate (branch count, members, assets, Forbes mentions) differs
                     between releases; the newest release wins
Tiers are part of the comparison key, so two tiers of one table are never reported as a conflict.
Entries for one product + term are merged (one entry per "question a member would ask").
"""
from __future__ import annotations

import re
from collections import defaultdict

from .contract_types import Conflict, Source
from .docs import Row
from .products import qualifiers
from .store import Store, View
from .text import pct_values

TYPE_NAME = {"calculator": "calculator page", "landing": "promotional landing page", "pdf": "PDF document",
             "product": "product page", "news": "press release", "blog": "blog post", "rates": "rates page"}
MIRRORS = {"business", "roth", "traditional", "equity"}   # tables that mirror a personal product's numbers


def _sig(r: Row, table_rows: list[Row]):
    years_rank = None
    if r.years:
        ranges = sorted({x.years for x in table_rows if x.years}, key=lambda y: -y[1])
        years_rank = ranges.index(r.years) if r.years in ranges else None
    amt = None
    if r.amount:
        amt = (round(r.amount[0]), None if r.amount[1] == float("inf") else round(r.amount[1]))
    q = frozenset(qualifiers(f"{r.context} {r.section} {r.label}"))
    return (r.family, q, r.term, amt, years_rank)


def _group_key(sig):
    """Product + qualifiers + term: tiers of the same product/term (balance or loan-amount tiers,
    standard vs jumbo, purchase vs refinance) are merged into one entry."""
    # older model-year tiers stay separate (and low priority): 5.59% can be one tier's 84-month
    # rate and another tier's 72-month rate, so mixing them would blur two different facts
    return sig[0], frozenset(sig[1] - {"jumbo", "purchase", "refinance"}), sig[2], (sig[4] or 0)


def _num(v: str) -> float | None:
    p = pct_values(v)
    return p[0] if p else None


def _rows_by_sig(view: View, types: tuple[str, ...]):
    out = defaultdict(list)
    for d in view.docs.values():
        if d.ptype not in types or d.url in view.carried:
            continue
        tables = defaultdict(list)
        for r in d.rows:
            tables[r.table].append(r)
        here = defaultdict(list)
        for r in d.rows:
            if r.kind == "text" and d.ptype in ("news", "blog", "pdf"):
                continue   # rates mentioned in passing in articles are too noisy to call conflicts
            if r.family and r.primary():
                here[_sig(r, tables[r.table])].append(r)
        for s, rows in here.items():
            # one signature with two different values on ONE page = a tier we could not parse: skip
            if len({_num(x.primary()) for x in rows}) == 1:
                out[s].append(rows[0])
    return out


def _tier(r: Row) -> str:
    return r.label or r.context


def _topic(r: Row) -> str:
    fam = r.family or "rate"
    term = ""
    if r.term:
        term = f"{r.term // 12}-year " if r.term % 12 == 0 and r.term >= 120 else f"{r.term}-month "
    q = sorted(qualifiers(f"{r.context} {r.section} {r.label}") - {"purchase"})
    metric = "APY" if any("yield" in h.lower() or "apy" in h.lower() for h in r.headers) else "APR"
    name = r.context or fam
    return f"{term}{' '.join(q) + ' ' if q else ''}{fam} {metric} ({name})".strip()


def _rate_conflicts(store: Store):
    latest = store.latest()
    entries = {}   # group key -> dict(prio, topic, values, sources, resolution parts, reasons)

    def entry(key, row):
        if key not in entries:
            entries[key] = {"prio": 0.0, "topic": _topic(row), "values": [], "sources": [], "res": [],
                            "reasons": [], "urls": set()}
        return entries[key]

    def add_val(e, v):
        if v and v not in e["values"]:
            e["values"].append(v)

    # ---- 1. stale copies in the latest snapshot
    groups = _rows_by_sig(latest, ("rates", "product", "calculator", "landing", "pdf", "news", "blog"))
    for s, rows in groups.items():
        if s[0] is None:
            continue
        auth = [r for r in rows if r.doc.ptype == "rates"]
        if not auth:
            continue
        good = auth[0]
        gv = _num(good.primary())
        stale = [r for r in rows if r.doc.ptype != "rates" and _num(r.primary()) not in (None, gv)]
        if not stale:
            continue
        e = entry(_group_key(s), good)
        add_val(e, good.value_str())
        for r in stale:
            add_val(e, r.value_str())
        e["res"].append(f"{good.value_str()} ({_tier(good)})")
        e["sources"].append(Source(url=good.doc.url, snapshot_id=latest.sid, quote=f"{_tier(good)}: {good.value_str()}"))
        for r in stale[:2]:
            if r.doc.url not in e["urls"]:
                e["urls"].add(r.doc.url)
                e["sources"].append(Source(url=r.doc.url, snapshot_id=latest.sid,
                                           quote=f"{_tier(r)}: {r.value_str()}"))
        kinds = sorted({TYPE_NAME.get(r.doc.ptype, r.doc.ptype) for r in stale})
        e["reasons"].append(f"the official rates page (effective {good.effective or 'latest'}) is authoritative; "
                            f"the {', '.join(kinds)} was not updated")
        mirror = bool(set(s[1]) & MIRRORS) or s[4] not in (0, None)
        e["prio"] = max(e["prio"], 2.0 if mirror else 3.0)

    # ---- 2. values on the official rates page that changed between snapshots
    series = defaultdict(list)
    for v in store.views:
        for s, rows in _rows_by_sig(v, ("rates",)).items():
            if rows:
                series[s].append((v, rows[0]))
    for s, seq in series.items():
        if s[0] is None:
            continue
        changes = []
        for v, r in seq:
            if not changes or _num(changes[-1][1].primary()) != _num(r.primary()):
                changes.append((v, r))
        if len(changes) < 2:
            continue
        last_v, last_r = seq[-1]
        e = entry(_group_key(s), last_r)
        for v, r in changes:
            add_val(e, r.value_str())
        hist = " → ".join(f"{r.primary()} ({v.day})" for v, r in changes)
        if last_v is latest:
            e["res"].append(f"{last_r.value_str()} ({_tier(last_r)})")
            e["reasons"].append(f"the rates page changed over time ({hist}); the newest snapshot is current")
        else:
            still = [r for r in groups.get(s, []) if r.doc.ptype != "rates"]
            e["res"].append(f"no longer offered ({_tier(last_r)} was removed from the rates page after "
                            f"{last_v.day})")
            msg = (f"{_tier(last_r)} was last listed on the rates page on {last_v.day} ({hist}) and has since "
                   f"been removed, so it should no longer be quoted")
            if still:
                msg += "; it is still shown on " + ", ".join(sorted({r.doc.url for r in still})[:2])
                for r in still[:2]:
                    add_val(e, r.value_str())
                    e["sources"].append(Source(url=r.doc.url, snapshot_id=latest.sid,
                                               quote=f"{_tier(r)}: {r.value_str()}"))
            e["reasons"].append(msg)
        if not e["sources"]:
            e["sources"] = [Source(url=r.doc.url, snapshot_id=v.sid, quote=f"{_tier(r)}: {r.value_str()}")
                            for v, r in changes[:4]]
        mirror = bool(set(s[1]) & MIRRORS) or s[4] not in (0, None)
        if last_v is latest:
            p = 2.2
        else:   # an ended offer: worth reporting when some page still advertises it
            p = 2.6 if any(r.doc.ptype != "rates" for r in groups.get(s, [])) else 1.4
        e["prio"] = max(e["prio"], 1.0 if mirror else p)

    out = []
    for key, e in entries.items():
        res = "; ".join(dict.fromkeys(e["res"])) or None
        reason = "; ".join(dict.fromkeys(e["reasons"]))
        out.append((e["prio"], Conflict(topic=e["topic"], values=e["values"][:12], sources=e["sources"][:6],
                                        resolution=res, reason=reason)))
    return out


# press-release boilerplate facts: (topic, regex, value formatter)
STATS = [
    ("number of branch office locations", re.compile(r"\b(\d{1,2}) branch(?: office)? locations", re.I),
     lambda m: f"{m.group(1)} branch office locations"),
    ("membership size (members)", re.compile(r"membership has grown to (?:more than|over|nearly|almost) ([\d,]{5,})", re.I),
     lambda m: f"{m.group(1)} members"),
    ("total assets", re.compile(r"assets (?:exceeding|of nearly|of more than|of) \$([\d.]+) billion", re.I),
     lambda m: f"${m.group(1)} billion in assets"),
    ("number of employees", re.compile(r"It has (?:over|more than|nearly|almost) ([\d,]{3,5}) employees", re.I),
     lambda m: f"{m.group(1)} employees"),
    ("number of counties served", re.compile(r"serves residents in (\d{2,3}) counties", re.I),
     lambda m: f"{m.group(1)} counties"),
    ("times recognized by Forbes", re.compile(r"\b(second|third|fourth|fifth|sixth|seventh|eighth) time Forbes", re.I),
     lambda m: f"{m.group(1).lower()} time recognized by Forbes"),
]


def _stat_conflicts(store: Store):
    latest = store.latest()
    out = []
    for topic, rx, fmt in STATS:
        seen = {}   # value -> (pub date, doc)
        for d in latest.docs.values():
            if d.ptype not in ("news", "blog") or d.pub is None:
                continue
            m = rx.search(" ".join(d.lines))
            if not m:
                continue
            val = fmt(m)
            if val not in seen or d.pub > seen[val][0]:
                seen[val] = (d.pub, d)
        if len(seen) < 2:
            continue
        ordered = sorted(seen.items(), key=lambda kv: kv[1][0], reverse=True)
        newest_val, (newest_day, newest_doc) = ordered[0]
        srcs = [Source(url=d.url, snapshot_id=latest.sid, quote=v) for v, (_, d) in ordered[:5]]
        reason = ("older press releases still repeat earlier figures; the most recent release "
                  f"({newest_day}, {newest_doc.url}) is current")
        out.append((2.8, Conflict(topic=topic, values=[v for v, _ in ordered[:6]], sources=srcs,
                                  resolution=newest_val, reason=reason)))
    return out


def find_conflicts(store: Store, limit: int = 20) -> list[Conflict]:
    items = _rate_conflicts(store) + _stat_conflicts(store)
    items.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for p, c in items:
        key = tuple(sorted(c.values))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out[:limit]
