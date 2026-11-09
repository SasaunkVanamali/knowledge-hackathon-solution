"""Answering: rate tables, hours, links and a general retrieval path, with honest refusals."""
from __future__ import annotations

import calendar
import re
from collections import Counter, defaultdict
from datetime import date

from .docs import SENT_RE, Doc, Row
from .products import qualifiers
from .query import SYNONYMS, QInfo, analyze
from .store import Store, View
from .text import RATE_WORDS, parse_iso, pct_values, tokens

# How much each kind of page is trusted. Rate questions: the official rates page wins, product
# pages next; calculators, landing pages, PDFs and old articles are where stale numbers live.
AUTH_RATE = {"rates": 1.0, "product": 0.8, "pdf": 0.3, "landing": 0.3, "calculator": 0.2,
             "news": 0.15, "blog": 0.1, "listing": 0.02, "junk": 0.0}
AUTH_TEXT = {"product": 1.0, "rates": 0.85, "pdf": 0.55, "landing": 0.55, "calculator": 0.45,
             "news": 0.8, "blog": 0.6, "listing": 0.1, "junk": 0.0}

# qualifier present on a ROW but not asked for -> multiply score
EXTRA_PENALTY = {"jumbo": 0.3, "business": 0.3, "special": 0.75, "refinance": 0.4, "intro": 0.3,
                 "roth": 0.8, "traditional": 0.8, "equity": 0.3, "rewards": 0.5, "relief": 0.15,
                 "purchase": 1.0, "achieve": 0.3}
# qualifier asked for but missing on the row
MISSING_PENALTY = {"jumbo": 0.3, "business": 0.4, "special": 0.7, "refinance": 0.4, "intro": 0.4,
                   "roth": 0.6, "traditional": 0.6, "equity": 0.5, "rewards": 0.4, "relief": 0.3,
                   "purchase": 0.85, "achieve": 0.3}

REFUSAL = ("I'm sorry, I can't find that on the Dupaco website, so I don't want to guess. "
           "Please contact Dupaco directly and a team member can help.")

# words that never identify a product (used when checking that a question's subject is covered)
GENERIC = set(tokens("""
current currently today now latest new rate rates apr apy annual percentage yield dividend interest much
offer offered offering available get earn earning pay paying charge charged price cost standard regular
account accounts loan loans product products option options term terms month months year years
balance balances between tier tiers amount minimum maximum highest lowest best top typical
model vehicle car buy buying purchase type types kind kinds which list every all info information
fixed variable my me member members someone open opening use using like would know want could tell
please question ask find look looking right moment time period still
change changed changes history previous previously before used increase increased decrease decreased
went go gone up down raise raised lower lowered compare compared since
january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec week day date
""")) | RATE_WORDS
PRODUCT_WORDS = set(tokens("""
visa card credit mortgage home auto cd certificate saving savings checking heloc ira roth jumbo share money
market personal boat rv atv motorcycle truck house equity line refinance refi business intro introductory
platinum reward rewards fixed bright club holiday hsa health high launchpad yield special program
"""))
LINK_WORDS = set(tokens("link url website page web address site see use go visit"))
DATE_IN_TEXT = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2},? 20\d\d\b|"
                          r"\b\d{1,2}/\d{1,2}/20\d\d\b", re.I)


FORM_RE = re.compile(r"\(Required\)|PreviousNext|PreviousSubmit")
CARRIED = 0.6       # trust multiplier for a page that was missing from the selected crawl


def rate_rows(view: View) -> list[Row]:
    """Rows used for rate answers: table rows anywhere, plus rates stated as sentences on a rates page
    (rates mentioned in passing elsewhere are too noisy)."""
    rr = getattr(view, "_rate_rows", None)
    if rr is None:
        rr = [r for r in view.rows if r.kind == "table" or
              (r.doc.ptype == "rates" and not any(x.kind == "table" for x in r.doc.rows))]
        view._rate_rows = rr
    return rr


def _years_rank(r: Row):
    """0 = newest model-year tier in the row's own table, 1 = next... (None without model years).
    Tables relabel tiers over time ("2023-26" became "2024-27"), so tiers are compared by rank."""
    if not r.years:
        return None
    ranges = sorted({x.years for x in r.doc.rows if x.table == r.table and x.years}, key=lambda y: -y[1])
    return ranges.index(r.years)


def fmt_day(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def recency(doc: Doc, today: date) -> float:
    if doc.pub is None:
        return 1.0
    age = max(0.0, (today - doc.pub).days / 365.0)
    return 1.0 / (1.0 + 0.45 * age)


class Engine:
    def __init__(self, store: Store):
        self.store = store
        self._cache: dict = {}

    # ================================================================== entry point
    def answer(self, question: str, as_of: str | None = None):
        from .contract_types import Answer
        qi = analyze(question)
        when = parse_iso(as_of) if as_of else None
        if when is None and qi.asked_date is not None:
            when = qi.asked_date
        if when is None and qi.asked_month:
            # "in June" without a year: the most recent June covered by the snapshots
            last = self.store.latest().day
            y = last.year if qi.asked_month <= last.month else last.year - 1
            when = date(y, qi.asked_month, calendar.monthrange(y, qi.asked_month)[1])
        view = self.store.view_for(when)
        key = (re.sub(r"\W+", " ", question.lower()).strip(), view.sid)
        if key in self._cache:
            return self._cache[key]
        ans = None
        for handler in (self._hours, self._link, self._rates, self._membership):
            ans = handler(qi, view)
            if ans is not None:
                break
        if ans is None:
            ans = self._general(qi, view)
        if view is not self.store.latest() and ans.confident and not ans.text.lower().startswith("as of"):
            ans.text = f"As of {fmt_day(view.day)}: " + ans.text
        self._cache[key] = ans
        return ans

    # ================================================================== rates
    def _score_row(self, qi: QInfo, r: Row, soften: bool) -> float | None:
        s = 1.0
        # term (months) is a hard qualifier
        if qi.terms:
            if r.term is None:
                s *= 0.35
            elif r.term in qi.terms:
                s *= 2.0
            else:
                return None
        if qi.years:
            if r.years:
                if any(r.years[0] <= y <= r.years[1] for y in qi.years):
                    s *= 2.0
                else:
                    return None
            elif r.family in ("auto loan", "atv loan"):
                s *= 0.5
        if qi.amounts:
            if r.amount:
                if all(r.amount[0] <= a <= r.amount[1] + 0.999 for a in qi.amounts):
                    # overlapping tiers ("$300,000 or less" / "$832,750 or less"): the tightest wins
                    width = r.amount[1] - r.amount[0]
                    s *= 2.0 + (0.0 if width == float("inf") else 0.5 * 1e6 / (width + 1e6))
                else:
                    return None
            else:
                s *= 0.7
        rq = qualifiers(f"{r.context} {r.section} {r.label}")
        for q in rq - qi.quals:
            p = EXTRA_PENALTY.get(q, 1.0)
            if soften and q in ("business", "rewards", "special", "jumbo"):
                p = max(p, 0.9)
            s *= p
        for q in qi.quals - rq:
            s *= MISSING_PENALTY.get(q, 1.0)
        # lexical overlap with the row's product words
        content = [t for t in qi.content if t not in RATE_WORDS]
        hit = sum(1 for t in content if t in r.tok)
        s *= 1.0 + 0.35 * hit
        return s

    def _rows_for(self, qi: QInfo, view: View) -> list[Row]:
        if qi.families:
            return [r for r in rate_rows(view) if r.family in qi.families]
        return []

    def _missing_subject(self, qi: QInfo, rows: list[Row], strict: bool = False) -> list[str]:
        """Question words that look like a product feature but appear in no candidate row
        (e.g. '7/1 adjustable-rate' or 'construction')."""
        vocab = set()
        for r in rows:
            vocab |= r.tok
        fam_words = set()
        for f in qi.families:
            fam_words |= set(tokens(f))
        miss = []
        for t in qi.content:
            if t in GENERIC or t in vocab or t in fam_words or t.isdigit() or re.fullmatch(r"\d+(\.\d+)?", t):
                continue
            if t in PRODUCT_WORDS and not strict:
                continue
            miss.append(t)
        return miss

    def _rates(self, qi: QInfo, view: View):
        from .contract_types import Answer, Source
        listing = bool(qi.families and re.search(
            r"\b(which|what)\b.*\b(have|offer|offered|available)\b|\b(types|kinds) of\b", qi.low))
        if not qi.rate_intent and listing:
            qi.list_mode = True     # "Which credit cards do you have?": list the products with their rates
        elif not qi.rate_intent:
            # "What is the 10 month special?" - a term plus product words is a rate question too
            if not qi.terms or re.search(r"\b(when|where|why|who)\b|\bhow (?!much)", qi.low):
                return None
            if not qi.families:
                qi.families = self._infer_families(qi, view)
            if not qi.families:
                return None
        inferred = False
        self._not_listed = False
        if not qi.families:
            qi.families = self._infer_families(qi, view)
            inferred = bool(qi.families)
        if not qi.families and self._not_listed:
            return Answer(text="That rate isn't listed on the Dupaco website. " + REFUSAL_TAIL, confident=False)
        if not qi.families:
            # a rate question about something we have no rate table for (e.g. construction loans)
            if re.search(r"\b(rate|apr|apy|interest)\b", qi.low) and re.search(
                    r"\b(loan|mortgage|arm|line|card|account|certificate|savings)\b", qi.low):
                all_vocab = set()
                for r in rate_rows(view):
                    all_vocab |= r.tok
                subj = [t for t in qi.content if t not in GENERIC and t not in all_vocab and not t.isdigit()]
                if subj:
                    return Answer(text="That rate isn't listed on the Dupaco website. " + REFUSAL_TAIL,
                                  confident=False)
            return None
        cands = self._rows_for(qi, view)
        if not cands:
            return None
        not_listed = Answer(text="That rate isn't listed on the Dupaco website. " + REFUSAL_TAIL, confident=False)
        if inferred and self._missing_subject(qi, cands, strict=True):
            return not_listed          # e.g. "business line of credit": no such rate table
        wanted = qi.quals - {"purchase"}
        if wanted and not any(wanted <= qualifiers(f"{r.context} {r.section} {r.label}") for r in cands):
            gone = self._ended_by_question(qi, view)
            return gone if gone is not None else not_listed   # e.g. "jumbo mortgage rate"
        if re.search(r"\b(adjustable|arm|7/1|5/1|10/1|variable[- ]rate mortgage|construction|jumbo mortgage|"
                     r"fha|va loan|usda)\b", qi.low) and "mortgage" in qi.families:
            if self._missing_subject(qi, cands):
                return not_listed
        soften = qi.list_mode and not (qi.terms or qi.amounts or qi.years)
        scored = []
        for r in cands:
            s = self._score_row(qi, r, soften)
            if s is None:
                continue
            if r.kind == "expired":
                continue
            a = AUTH_RATE.get(r.doc.ptype, 0.1) * (CARRIED if r.doc.url in view.carried else 1.0)
            if r.kind == "text":
                a *= 0.85
            if a <= 0:
                continue
            scored.append((s * a, s, r))
        if not scored:
            gone = self._ended_by_question(qi, view)
            if gone is not None:
                return gone
            missing = self._missing_subject(qi, cands)
            return Answer(text=("I couldn't find a published rate matching that on the Dupaco website. "
                                + REFUSAL_TAIL), confident=False) if (qi.terms or qi.years or missing) else None
        scored.sort(key=lambda x: -x[0])
        best_total, _, best_row = scored[0]
        best_doc = best_row.doc
        ended = self._ended_offer(best_row, view)
        if ended is None and (qi.quals - qualifiers(f"{best_row.context} {best_row.section} {best_row.label}")
                              - {"purchase"}):
            ended = self._ended_by_question(qi, view)
        if ended is not None:
            return ended
        # rows from the best page only (the authoritative copy)
        page_rows = [(t, s, r) for t, s, r in scored if r.doc is best_doc]
        tables = defaultdict(list)
        for t, s, r in page_rows:
            tables[r.table].append((t, s, r))
        tscore = {k: max(x[0] for x in v) for k, v in tables.items()}
        specific = bool(qi.terms or qi.amounts or qi.years)
        keep_tables = [k for k, v in tscore.items() if v >= (0.999 if specific else 0.75) * best_total]
        if qi.list_mode and not specific:
            keep_tables = [k for k, v in tscore.items() if v >= 0.5 * best_total]
        chosen: list[Row] = []
        for k in sorted(keep_tables, key=lambda k: -tscore[k]):
            rows_k = tables[k]
            top = max(x[0] for x in rows_k)
            thr = 0.999 if specific else 0.8
            chosen += [r for t, s, r in rows_k if t >= thr * top]
        # list mode: add matching tables from other authoritative pages (e.g. business cards)
        if qi.list_mode and not specific:
            seen = {(r.label.lower(), r.primary()) for r in chosen}
            for t, s, r in scored:
                if r.doc is best_doc or r.doc.ptype not in ("rates", "product") or t < 0.35 * best_total:
                    continue
                k = (r.label.lower(), r.primary())
                if k in seen or any(r.label.lower() == c.label.lower() for c in chosen):
                    continue
                seen.add(k)
                chosen.append(r)
        if qi.superlative and len(chosen) > 1:
            vals = [(pct_values(r.primary()) or [0])[0] for r in chosen]
            pick = max if qi.superlative == "max" else min
            target = pick(vals)
            chosen = [r for r, v in zip(chosen, vals) if v == target][:1]
        chosen = chosen[:18]
        if not chosen:
            return None

        # yes/no "does any X pay 0.35%?"
        if qi.yesno and qi.pcts:
            fam_rows = [r for t, s, r in page_rows if t >= 0.5 * best_total] or chosen
            hits = [r for r in fam_rows if any(abs(v - p) < 1e-9 for p in qi.pcts for v in pct_values(r.value_str()))]
            listing = self._format_rows(fam_rows[:10])
            if hits:
                text = f"Yes. {self._format_rows(hits)}"
            else:
                pv = sorted({v for r in fam_rows for v in pct_values(r.primary())})
                rng = f" They range from {pv[0]:.2f}% to {pv[-1]:.2f}%." if pv else ""
                top = max(fam_rows, key=lambda r: (pct_values(r.primary()) or [0])[0])
                text = (f"No. None of the {self._product_name(fam_rows[0])} tiers pays {qi.pcts[0]:.2f}%.{rng} "
                        f"The highest is {top.primary()} ({top.label}). Current tiers: {listing}")
            return Answer(text=text, sources=self._sources(view, chosen), confident=True)

        if qi.change_intent:
            hist = self._history(chosen[:4], view)
            if hist:
                return Answer(text=hist, sources=self._sources(view, chosen), confident=True)

        eff = next((r.effective for r in chosen if r.effective), None)
        name = self._product_name(chosen[0])
        intro = f"{name} rates" if len(chosen) > 1 else name
        lead = f"Current {intro}" if view is self.store.latest() else intro
        text = f"{lead} on Dupaco's rates page{' (effective ' + eff + ')' if eff else ''}: {self._format_rows(chosen)}"
        if len(text) > 1600:
            text = text[:1600].rsplit(";", 1)[0] + "."
        note = self._stale_note(view, chosen)
        return Answer(text=text, sources=self._sources(view, chosen), confident=True, conflict_note=note)

    def _infer_families(self, qi: QInfo, view: View) -> list[str]:
        """No product named ('17-month jumbo special APY?'): pick the product families whose
        rate rows share the question's distinctive words (and term, if one is given)."""
        words = {t for t in qi.content if t not in GENERIC and not t.isdigit()}
        if not words and not qi.terms:
            return []
        best, fams = 0, set()
        for r in rate_rows(view):
            if not r.family or r.doc.ptype not in ("rates", "product"):
                continue
            sc = len(words & r.tok)
            if qi.terms and r.term in qi.terms:
                sc += 1
            if sc > best:
                best, fams = sc, {r.family}
            elif sc == best and sc > 0:
                fams.add(r.family)
        if best < (2 if qi.terms else 1):
            return []
        if best < len(words) + (1 if qi.terms else 0):
            # no single rate row covers everything asked about (e.g. "business line of credit")
            self._not_listed = True
            return []
        return sorted(fams)

    def _ended_offer(self, row: Row, view: View):
        """The best match only survives on a secondary page (guide, landing page...) but the official
        rates page dropped it in an earlier snapshot: the offer has ended, so don't quote it."""
        from .contract_types import Answer, Source
        if row.doc.ptype == "rates":
            return None
        path_toks = set(tokens(row.doc.url.split("dupaco.com", 1)[-1].replace("-", " ").replace("/", " ")))
        if row.doc.ptype == "product" and path_toks & set(tokens(row.family or "")):
            return None   # the product's own page (e.g. /business/credit-cards) is authoritative for it
        rq = qualifiers(f"{row.context} {row.section} {row.label}")

        def same(r: Row) -> bool:
            return (r.family == row.family and r.term == row.term and
                    qualifiers(f"{r.context} {r.section} {r.label}") == rq)

        rates_now = [d for d in view.docs.values() if d.ptype == "rates"]
        if not rates_now or any(same(r) for d in rates_now for r in d.rows):
            return None
        last_seen = None
        for v in self.store.views:
            if v.day >= view.day:
                break
            if any(same(r) for d in v.docs.values() if d.ptype == "rates" for r in d.rows):
                last_seen = v
        if last_seen is None:
            return None
        name = (row.label or row.context).replace("*", "").strip()
        return Answer(
            text=(f"The {name} is no longer offered: it has been removed from Dupaco's current rates page, "
                  f"even though an older page still mentions it. Please contact Dupaco for current options."),
            sources=[Source(url=d.url, snapshot_id=view.sid) for d in rates_now[:1]],
            confident=False,
            conflict_note=(f"{row.doc.url} still shows {row.value_str()}; the rates page last listed it on "
                           f"{last_seen.day}"))

    def _ended_by_question(self, qi: QInfo, view: View):
        """Nothing current matches (e.g. '25-month special', 'HELOC intro rate') but an earlier
        snapshot's rates page listed exactly that: the offer ended."""
        from .contract_types import Answer, Source
        last = None
        for v in self.store.views:
            if v.day >= view.day:
                break
            for d in v.docs.values():
                if d.ptype != "rates":
                    continue
                for r in d.rows:
                    if r.family not in qi.families:
                        continue
                    if qi.terms and r.term not in qi.terms:
                        continue
                    rq = qualifiers(f"{r.context} {r.section} {r.label}")
                    if qi.quals - {"purchase"} <= rq and (qi.terms or qi.quals - {"purchase"}):
                        last = (v, r)
        if last is None:
            return None
        v, r = last
        name = (r.label or r.context).replace("*", "").strip()
        rates_now = [d for d in view.docs.values() if d.ptype == "rates"]
        return Answer(
            text=(f"The {name} is no longer offered: it was removed from Dupaco's rates page and isn't listed "
                  f"today. Please contact Dupaco for current options."),
            sources=[Source(url=d.url, snapshot_id=view.sid) for d in rates_now[:1]],
            confident=False,
            conflict_note=f"last listed on {v.day} at {r.value_str()}")

    @staticmethod
    def _product_name(r: Row) -> str:
        name = r.context or r.section or r.family or "Rate"
        if r.family and r.family.split()[0] not in name.lower() and r.family not in name.lower():
            name = f"{name} ({r.family})"
        return name.strip(" :")

    @staticmethod
    def _format_rows(rows: list[Row]) -> str:
        parts = []
        for r in rows:
            label = r.label or r.context
            if r.context and r.context.lower() not in label.lower() and len(rows) > 1 and \
                    len({x.context for x in rows}) > 1:
                label = f"{r.context} – {label}"
            parts.append(f"{label}: {r.value_str()}")
        return "; ".join(parts) + "."

    def _sources(self, view: View, rows: list[Row]):
        from .contract_types import Source
        urls = []
        main = rows[0].doc
        sig = {(r.label.lower(), r.primary()) for r in rows}
        # pages that carry the same numbers (product pages are the canonical "see details" links)
        others = []
        for d in view.docs.values():
            if d is main or d.ptype not in ("product", "rates"):
                continue
            if any((r.label.lower(), r.primary()) in sig for r in d.rows):
                others.append(d)
        others.sort(key=lambda d: (d.ptype != "product", len(d.url)))
        for d in [main] + others[:3]:
            if d.url not in urls:
                urls.append(d.url)
        quote = f"{rows[0].label}: {rows[0].value_str()}"
        return [Source(url=u, snapshot_id=view.sid, quote=quote if i == 0 else None) for i, u in enumerate(urls)]

    def _stale_note(self, view: View, rows: list[Row]) -> str | None:
        notes = []
        for r in rows[:4]:
            for d in view.docs.values():
                if d is r.doc or d.ptype in ("rates",):
                    continue
                for o in d.rows:
                    if o.family == r.family and o.term == r.term and o.term is not None and \
                            qualifiers(f"{o.context} {o.label}") == qualifiers(f"{r.context} {r.label}") and \
                            o.primary() and r.primary() and o.primary() != r.primary() and \
                            _years_rank(o) == _years_rank(r) and \
                            ((o.amount is None and r.amount is None) or o.amount == r.amount):
                        notes.append(f"{d.url} shows {o.primary()} for {o.label}; the rates page ({r.primary()}) "
                                     f"is treated as authoritative")
                        break
        return " | ".join(dict.fromkeys(notes[:3])) or None

    def _history(self, rows: list[Row], view: View) -> str | None:
        out = []
        for r in rows:
            series = []
            for v in self.store.views:
                if v.day > view.day:
                    break
                d = v.docs.get(r.doc.url)
                if not d:
                    continue
                m = [o for o in d.rows if o.family == r.family and o.term == r.term and o.amount == r.amount
                     and qualifiers(f"{o.context} {o.label}") == qualifiers(f"{r.context} {r.label}")]
                if r.years:
                    m = [o for o in m if o.years and o.years[1] >= r.years[1] - 1] or m
                if m:
                    series.append((v.day, m[0].primary()))
            if not series:
                continue
            segs = []
            for day, val in series:
                if segs and segs[-1][1] == val:
                    segs[-1][2] = day
                else:
                    segs.append([day, val, day])
            label = r.label or r.context
            if len(segs) == 1:
                out.append(f"{label}: {segs[0][1]} — unchanged across the snapshots from {fmt_day(segs[0][0])} "
                           f"to {fmt_day(segs[0][2])}")
            else:
                hist = ", then ".join(f"{val} ({fmt_day(a)}" + (f" – {fmt_day(b)})" if b != a else ")")
                                      for a, val, b in segs[:-1])
                out.append(f"{label}: was {hist}; it is now {segs[-1][1]} (since {fmt_day(segs[-1][0])})")
        return ("Rate history from the website snapshots — " + "; ".join(out) + ".") if out else None

    # ================================================================== hours
    DAY = r"(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?"
    HOURS_LINE = re.compile(rf"^\s*(?:lobby|drive[- ]?(?:up|thru|through))?\s*{DAY}(\s*[-–—&,/]\s*{DAY})*\s*:\s*"
                            r"(closed|\d{1,2}(:\d\d)?\s*(a\.?m\.?|p\.?m\.?|noon)?\s*[-–—]\s*(\d{1,2}(:\d\d)?\s*"
                            r"(a\.?m\.?|p\.?m\.?)|noon))", re.I)

    def _hour_blocks(self, view: View):
        blocks = []   # (kind, doc, tuple(lines), context-text)
        for d in view.docs.values():
            if d.ptype in ("junk", "listing"):
                continue
            mode, cur, start = None, [], 0
            lines = d.lines
            for i, line in enumerate(lines):
                low = line.lower()
                if re.match(r"^(#+\s*)?(lobby|drive[- ]?(up|thru|through))\b", low):
                    if cur:
                        blocks.append((mode, d, tuple(cur), " ".join(lines[max(0, start - 8):start])))
                    mode = "lobby" if low.lstrip("# ").startswith("lobby") else "drive-up"
                    cur, start = [], i
                    rest = re.sub(r"^(#+\s*)?(lobby|drive[- ]?(up|thru|through))\s*", "", line, flags=re.I)
                    if self.HOURS_LINE.match(rest):
                        cur.append(self._norm_hours(rest))
                    continue
                if mode and self.HOURS_LINE.match(line):
                    cur.append(self._norm_hours(line))
                    continue
                if mode and line.strip() and cur:
                    blocks.append((mode, d, tuple(cur), " ".join(lines[max(0, start - 8):start])))
                    mode, cur = None, []
            if cur:
                blocks.append((mode, d, tuple(cur), " ".join(lines[max(0, start - 8):start])))
        return blocks

    @staticmethod
    def _norm_hours(s: str) -> str:
        s = s.strip().rstrip(";.")
        s = re.sub(r"\s*[-–—]\s*", "–", s)
        s = re.sub(r"\s*:\s*", ": ", s, count=1)
        s = re.sub(r"(\d)(a\.m\.|p\.m\.)", r"\1 \2", s)
        return s

    def _hours(self, qi: QInfo, view: View):
        from .contract_types import Answer, Source
        if not qi.hours_intent:
            return None
        if re.search(r"\b(phone|call center|contact center|member care|support line)\b", qi.low):
            return None
        blocks = self._hour_blocks(view)
        if not blocks:
            return None
        want = "drive-up" if re.search(r"drive|thru|through", qi.low) else (
            "lobby" if "lobby" in qi.low else None)
        if qi.branch:
            name = qi.branch.lower()
            key = [w for w in re.findall(r"[a-z0-9\-]+", name) if w not in ("avenue", "ave", "street", "st",
                                                                             "road", "rd", "drive", "dr")]
            mine = [b for b in blocks if key and all(k in (b[3] + " " + b[1].title + " " + b[1].h1).lower()
                                                      for k in key)]
            if want:
                mine = [b for b in mine if b[0] == want]
            if not mine:
                return Answer(text=(f"I don't have hours listed for the {qi.branch} branch on the website, so I "
                                    "don't want to guess. Please use the branch locator on the Contact page or "
                                    "contact Dupaco to confirm that branch's hours."), confident=False,
                              sources=[Source(url="https://www.dupaco.com/contact")])
            blocks = mine
        kinds = [want] if want else ["lobby", "drive-up"]
        parts, srcs = [], []
        for kind in kinds:
            bs = [b for b in blocks if b[0] == kind]
            if not bs:
                continue
            weight = Counter()
            docs_of = defaultdict(list)
            for b in bs:
                weight[b[2]] += 1.0 + recency(b[1], view.day)
                docs_of[b[2]].append(b[1])
            ranked = [blk for blk, _ in weight.most_common()]
            main = ranked[0]
            label = "Lobby" if kind == "lobby" else "Drive-up"
            where = f"at the {qi.branch} branch" if qi.branch else "at most branches"
            txt = f"{label} hours {where}: " + "; ".join(main)
            alts = []
            for blk in ranked[1:]:
                diff = [l for l in blk if l not in main]
                if diff:
                    alts.append("; ".join(diff))
                if len(alts) >= 3:
                    break
            if alts:
                txt += ". Some branches differ (e.g. " + " / ".join(alts) + ")"
            parts.append(txt + ".")
            for blk in ranked[:3]:
                srcs += [d.url for d in sorted(docs_of[blk], key=lambda d: d.pub or view.day, reverse=True)[:2]]
        if not parts:
            return None
        lead = "" if qi.branch else "Hours vary by branch. "
        tail = "" if qi.branch else (" Which branch are you interested in? Each branch's hours are in the "
                                    "locator at https://www.dupaco.com/contact.")
        return Answer(text=lead + " ".join(parts) + tail,
                      sources=[Source(url=u, snapshot_id=view.sid) for u in dict.fromkeys(srcs)] +
                              [Source(url="https://www.dupaco.com/contact")], confident=True)

    # ================================================================== links
    def _link(self, qi: QInfo, view: View):
        from .contract_types import Answer, Source
        if not qi.link_intent:
            return None
        q = {t for t in qi.content if t not in LINK_WORDS}
        for t in list(q):
            q |= set(SYNONYMS.get(t, []))
        if not q:
            return None
        action = set(tokens("apply application open sign enroll register get find start"))

        def qw(t):   # subject words count more than action words ("apply" for *membership*)
            return 0.3 if t in action else qi.weights.get(t, 0.5)

        score = Counter()
        seen_anchor = set()   # the same button repeated on many pages ("Apply today") counts once
        for d in view.docs.values():
            if d.ptype in ("junk", "listing"):
                continue
            a = AUTH_TEXT.get(d.ptype, 0.3)
            for anchor, url, line in d.links:
                if "dupaco.com" not in url or line.strip() in self.store.boiler:
                    continue
                target = url.split("://", 1)[-1].replace(".", " ").replace("/", " ").replace("-", " ")
                ctx = set(tokens(anchor + " " + line + " " + target))
                ov = sum(qw(t) for t in q & ctx)
                akey = (anchor.lower(), url.split("?")[0].rstrip("/"))
                if ov and akey not in seen_anchor:
                    seen_anchor.add(akey)
                    tdoc = view.docs.get(url.split("#")[0]) or view.docs.get(url.split("#")[0].rstrip("/"))
                    host = url.split("://", 1)[-1].split("/")[0]
                    if tdoc is not None:
                        tw = 1.0 if tdoc.ptype in ("product", "rates") else 0.3
                    elif host.startswith("www."):
                        tw = 0.5        # a site page that is not in the crawl may be a dead link
                    else:
                        # a portal subdomain (join., shine.) named after what was asked is the answer
                        tw = 2.0 if q & set(tokens(host.split(".")[0])) else 1.0
                    score[url.rstrip("/")] += a * tw * ov * ov
            # the page itself
            ptoks = set(tokens(d.title + " " + d.url.replace("-", " ").replace("/", " ")))
            ov = sum(qw(t) for t in q & ptoks)
            if ov and d.ptype in ("product", "rates"):
                score[d.url.rstrip("/")] += 1.5 * ov * ov
        if not score:
            return None
        # merge fragment variants (#rates, #dupaco-locator-widget_...) into their page
        merged = Counter()
        rep = {}
        for u, s in score.items():
            base = u.split("#")[0].split("?")[0].rstrip("/")
            host = base.split("://", 1)[-1].split("/")[0]
            if host and not host.startswith("www."):
                base = base.split("://", 1)[0] + "://" + host   # join.dupaco.com/products/... -> join.dupaco.com
            merged[base] += s
            if base not in rep or (score[rep[base]] < s and len(u) < 90):
                rep[base] = u if len(u) < 90 else base
        best, _ = merged.most_common(1)[0]
        url = rep[best]
        d = view.docs.get(best) or view.docs.get(best + "/")
        title = f" ({d.title})" if d and d.title else ""
        return Answer(text=f"You can use {url}{title}.", sources=[Source(url=url, snapshot_id=view.sid)],
                      confident=True)

    # ================================================================== membership eligibility
    ELIGIBLE_RE = re.compile(r"\b(eligib\w*|who can (join|become)|qualif\w* (for|to) (join|membership|become)|"
                             r"requirements? (to|for) (join|membership)|can (i|anyone|my \w+) (join|become)|"
                             r"field of membership|charter area)\b", re.I)
    SERVICE_AREA_RE = re.compile(r"[^.]*\bserves?\b[^.]*\bcounties\b[^.]*\.")

    def _membership(self, qi: QInfo, view: View):
        """'Who can join?': the join page's eligibility rules + where the charter area is."""
        from .contract_types import Answer, Source
        if not self.ELIGIBLE_RE.search(qi.low) or re.search(r"\b(online|website|app|link|url)\b", qi.low):
            return None
        probe = analyze("who is eligible to join live work charter area membership")
        ans = self._general(probe, view)
        if not ans.confident:
            return None
        # newest description of the service area (counties / states)
        area, area_doc = None, None
        for d in sorted(view.docs.values(), key=lambda d: d.pub or date.min, reverse=True):
            if d.ptype not in ("news", "product", "blog"):
                continue
            m = self.SERVICE_AREA_RE.search(" ".join(d.lines))
            if m:
                area, area_doc = m.group(0).strip(), d
                break
        text = ans.text
        if area and area not in text:
            text += f" The charter/service area: {area}"
        srcs = list(ans.sources)
        if area_doc:
            srcs.append(Source(url=area_doc.url, snapshot_id=view.sid, quote=area))
        return Answer(text=text, sources=srcs, confident=True)

    # ================================================================== general retrieval
    WANT_NUMBER = re.compile(r"^\s*(how (many|much|long|old)|what (number|year|percentage))|number of|\bcount\b", re.I)
    WANT_DATE = re.compile(r"^\s*when\b|what date|which date|still (open|available|offered)|"
                           r"(close|closing|end|ends|expire|expires)\??\s*$", re.I)
    NUM_IN_TEXT = re.compile(r"\b\d[\d,]*(\.\d+)?\b")

    def _general(self, qi: QInfo, view: View):
        from .contract_types import Answer, Source
        if not qi.weights:
            return Answer(text=REFUSAL, confident=False)
        hits = view.bm25.search(qi.weights, top=150 if not self.WANT_NUMBER.search(qi.low) else 500)
        if not hits:
            return Answer(text=REFUSAL, confident=False)
        qset = set(qi.content)
        idf = view.bm25.idf
        total_w = sum(idf.get(t, 0.0) for t in qset) or 1.0
        total_ww = sum(idf.get(t, 0.0) * w for t, w in qi.weights.items()) or 1.0
        want_num = bool(self.WANT_NUMBER.search(qi.low))
        want_date = bool(self.WANT_DATE.search(qi.low))
        want_url = bool(re.search(r"\b(online|website|apply|sign up|enroll|register)\b", qi.low))
        want_count = bool(re.search(r"\bhow many\b|number of|\bcount\b", qi.low))
        # consecutive question words ("apple pay", "money makeover"): a passage using the exact phrase wins
        bigrams = {f"{a} {b}" for a, b in zip(qi.content, qi.content[1:])}
        best = []
        for i, s in hits:
            c = view.chunks[i]
            d = c.doc
            a = AUTH_TEXT.get(d.ptype, 0.3) * recency(d, view.day) * (CARRIED if d.url in view.carried else 1.0)
            if a <= 0:
                continue
            ttoks = set(tokens(d.title))
            tov = sum(w for t, w in qi.weights.items() if t in ttoks)
            ctoks = set(c.tok)
            cov = sum(idf.get(t, 0.0) for t in qset & ctoks) / total_w
            wcov = sum(idf.get(t, 0.0) * w for t, w in qi.weights.items() if t in ctoks) / total_ww
            s2 = s * a * (1 + 0.3 * tov) * (0.5 + wcov)
            if want_num and self._number_near(c.text, qset):
                s2 *= 1.6
            if want_date and DATE_IN_TEXT.search(c.text):
                s2 *= 1.6
            if d.ptype in ("news", "blog") and c.pos <= 4:
                s2 *= 1.25          # the lede of an article summarises it
            if want_url and "://" in c.text:
                s2 *= 1.5
            if bigrams:
                ctext = c.__dict__.get("_tokstr")
                if ctext is None:
                    ctext = c._tokstr = " ".join(tokens(c.text))
                s2 *= 1.0 + 0.3 * sum(1 for bg in bigrams if bg in ctext)
            if FORM_RE.search(c.text):
                s2 *= 0.2           # web-form widgets ("(Required)", "PreviousNext") are not answers
            if len(c.text) < 40:
                s2 *= 0.3           # a bare heading or button label is not an answer
            best.append((s2, cov, c))
        best.sort(key=lambda x: -x[0])
        if not best:
            return Answer(text=REFUSAL, confident=False)
        s2, cov, c = best[0]
        if want_num and want_count:
            # counts repeated in many dated releases ("22 branch office locations"): the newest wins
            numc = [x for x in best if self._number_near(x[2].text, qset)]
            if numc:
                top = numc[0][0]
                mcov = max(x[1] for x in numc)
                pool = [x for x in numc if x[0] >= 0.2 * top and x[1] >= 0.9 * mcov]
                dated = [x for x in pool if x[2].doc.pub is not None]
                s2, cov, c = max(dated, key=lambda x: (x[2].doc.pub, x[0])) if dated else pool[0]
        if want_date and not DATE_IN_TEXT.search(c.text):
            # "is it closing?": the dated sentence may sit in another passage of the same article
            dated = [x for x in best[1:30] if x[2].doc is c.doc and DATE_IN_TEXT.search(x[2].text)
                     and x[0] >= 0.3 * s2]
            if dated:
                s2, cov, c = max(dated, key=lambda x: x[0])
        if cov < 0.25:
            return Answer(text=REFUSAL, confident=False)
        text = self._extract(qi, c.text, limit=450 if want_num else 480, want_num=want_num, want_date=want_date)
        # add a second passage from the same page if it covers what the first missed
        missing = qset - set(c.tok)
        if want_num and not self.NUM_IN_TEXT.search(text):
            # "how much ...?" but the chosen passage has no number: take the page's best numeric passage
            for s3, cov3, c3 in best[1:30]:
                if c3.doc is c.doc and self.NUM_IN_TEXT.search(c3.text) and c3.text not in text:
                    text = text + " " + self._extract(qi, c3.text, limit=260, want_num=True)
                    missing = set()
                    break
        if missing:
            for s3, cov3, c3 in best[1:6]:
                if c3.doc is c.doc and missing & set(c3.tok) and s3 > 0.5 * s2 and c3.text not in text:
                    text = text + " " + self._extract(qi, c3.text, limit=260, want_num=want_num,
                                                      want_date=want_date)
                    break
        title = c.doc.title.strip()
        if c.doc.ptype in ("product", "rates") and title and len(title) < 60 and                 title.lower() not in text.lower() and not text.lower().startswith(title.lower()[:20]):
            text = f"{title}: {text}"
        srcs = [Source(url=c.doc.url, snapshot_id=view.sid, quote=c.text[:200])]
        return Answer(text=text, sources=srcs, confident=True)

    def _number_near(self, text: str, qset: set) -> bool:
        """True when a number sits right next to a question word ("22 branch office locations",
        "294 Dupaco employees"); years and addresses do not count."""
        for sent in SENT_RE.split(text):
            words = re.findall(r"[\w$,.%]+", sent.lower())
            toks = [tokens(w) for w in words]
            for i, w in enumerate(words):
                if not re.fullmatch(r"\$?\d[\d,]*(\.\d+)?%?|first|second|third|fourth|fifth|sixth|seventh|"
                                    r"eighth|ninth|tenth", w.strip(".,")):
                    continue
                if re.fullmatch(r"(19|20)\d\d", w.strip(".,")):
                    continue
                if i + 1 < len(words) and re.match(r"(years?|months?|days?|hours?|minutes?|a\.?m|p\.?m)\b",
                                                   words[i + 1]) and not qset & {"year", "long", "hour"}:
                    continue
                window = toks[i + 1:i + 4] + toks[max(0, i - 2):i]
                if any(qset & set(t) for t in window):
                    return True
        return False

    def _extract(self, qi: QInfo, text: str, limit: int = 480, want_num: bool = False,
                 want_date: bool = False) -> str:
        if len(text) <= limit:
            return text
        sents = SENT_RE.split(text)
        q = set(qi.content) | {k for k, w in qi.weights.items() if w < 1}
        scored = []
        for i, s in enumerate(sents):
            sc = len(q & set(tokens(s))) + (0.3 if i == 0 else 0)
            if want_date and DATE_IN_TEXT.search(s):
                sc += 1.5
            if want_num and self.NUM_IN_TEXT.search(s):
                sc += 1.0 + (2.0 if self._number_near(s, set(qi.content)) else 0.0)
            if want_num and "how much" in qi.low and "$" in s:
                sc += 2.0           # "how much" asks for an amount of money
            scored.append((sc, i, s))
        top = sorted(scored, key=lambda x: (-x[0], x[1]))
        keep, size = [], 0
        for sc, i, s in top:
            if size + len(s) > limit and keep:
                break
            keep.append(i)
            size += len(s)
        return " ".join(sents[i] for i in sorted(keep))


REFUSAL_TAIL = ("I don't want to guess, so please contact Dupaco directly and a team member can give you a "
                "current quote.")
