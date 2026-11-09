"""Text utilities: page classification, cleaning, dates, tokenisation, chunking.

Everything here is pure standard library so the submission installs with no packages.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

# --------------------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------------------
MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
DATE_LONG_RE = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}}),?\s+(20\d\d|19\d\d)\b", re.I)
MONTH_YEAR_RE = re.compile(rf"\b({_MONTH_ALT})\.?,?\s+(20\d\d)\b", re.I)
DATE_NUM_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b")
URL_DATE_RE = re.compile(r"/(20\d\d|19\d\d)/(\d\d)/(\d\d)/")


def parse_iso(s: str | None) -> date | None:
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def publication_date(url: str, markdown: str) -> date | None:
    """Publication date of a news/blog page: from the URL (/2018/09/25/) or the byline
    date printed just above the H1 title."""
    m = URL_DATE_RE.search(url)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for line in markdown.splitlines()[:25]:
        if line.startswith("# "):
            break
        m = DATE_LONG_RE.search(line)
        if m:
            return _safe_date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
    return None


# --------------------------------------------------------------------------------------
# Page classification
# --------------------------------------------------------------------------------------
LANDING_HINTS = ("-lp", "switch-and-earn", "offer", "thankuse", "not-for-sale", "sprint-to-savings")


def page_type(url: str) -> str:
    """Coarse page type. Drives how much we trust a page ("authority")."""
    p = urlparse(url)
    path = p.path.lower().rstrip("/")
    if "privacy-policy" in path and p.query:
        return "junk"
    if path.startswith(("/newsroom/tag", "/newsroom/author", "/learn/blog/tag", "/learn/blog/author")):
        return "listing"
    if path.startswith("/resources") or p.query.startswith("category"):
        return "listing"
    if path in ("/rates", "/business-rates") or path.startswith("/rates/") or path.endswith("-rates"):
        return "rates"
    if "calculator" in path:
        return "calculator"
    if path.startswith("/newsroom"):
        return "news"
    if path.startswith(("/learn/blog", "/business/resources")) or URL_DATE_RE.search(path + "/"):
        return "blog"
    if path.startswith("/hubfs"):
        return "pdf"
    if any(h in path for h in LANDING_HINTS):
        return "landing"
    return "product"


# --------------------------------------------------------------------------------------
# Tokenisation
# --------------------------------------------------------------------------------------
TOKEN_RE = re.compile(r"\d+(?:\.\d+)?|[a-z][a-z0-9']*")
STOP = set("""
a an the and or of to in on for with by at from as is are was were be been being it its this that these those
i me my we our you your he she they them their what which who whom whose when where why how do does did can
could would should will shall may might must am has have had having not no yes if then than so such about
into over under up down out off again further once here there all any both each few more most other some
own same too very just also please tell know give get got need want like there's what's whats let us im
dupaco dupacos credit union cu s t https http www com html htm many much anyone someone something last ago
""".split())
# words that describe *asking for a rate*, not *which product*
def stem(w: str) -> str:
    """Very light stemmer: drop plurals, then truncate long words to 6 letters so that
    recognition/recognized, eligible/eligibility, member/membership share a stem."""
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    if len(w) > 6 and w.isalpha():
        # keep person-suffixes apart: employee vs employer, trustee vs truster
        w = w[:6] + w[-2:] if w.endswith(("ee", "er")) else w[:6]
    return w


RATE_WORDS = {stem(w) for w in ("rate", "rates", "apr", "aprs", "apy", "dividend", "interest", "yield",
                                "percent", "percentage", "annual")}


def tokens(text: str) -> list[str]:
    text = text.lower().replace("’", "'")
    out = []
    for t in TOKEN_RE.findall(text):
        t = t.strip("'")
        if t.endswith("'s"):
            t = t[:-2]
        if not t or t in STOP:
            continue
        out.append(stem(t))
    return out


# --------------------------------------------------------------------------------------
# Markdown cleaning
# --------------------------------------------------------------------------------------
IMG_RE = re.compile(r"!\[[^\]]*\]\((?:[^()]|\([^()]*\))*\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\(((?:[^()\s]|\([^()\s]*\))+)(?:[^()]|\([^()]*\))*\)")
TEASER_HEADINGS = re.compile(r"^#+\s*(handpicked for you|related (posts|articles|resources)|you might also like|"
                             r"more (stories|articles)|recent posts)\b", re.I)
JUNK_LINE = re.compile(r"^(✕|cancel|previous|next|go|loading locations\.\.\.|\d+ min read|"
                       r"third party plugins|messenger( launcher)?|keep reading|\* \* \*|- \d+|#+)$", re.I)
TEASER_DATE = re.compile(rf"^({_MONTH_ALT})\.?\s+\d{{1,2}},\s+20\d\d$", re.I)
CAPTCHA = re.compile(r"recaptcha|select all images|click verify", re.I)


def extract_links(markdown: str) -> list[tuple[str, str, str]]:
    """(anchor text, url, the line it sits in) for every markdown link."""
    out = []
    md = IMG_RE.sub("", markdown.replace("\\\n", " "))
    for line in md.splitlines():
        for m in LINK_RE.finditer(line):
            anchor = re.sub(r"[*_\\]", "", m.group(1)).strip()
            url = m.group(2).strip()
            if url.startswith(("http://", "https://")):
                out.append((anchor, url, LINK_RE.sub(r"\1", line)[:300]))
    return out


def _render_link(m: re.Match) -> str:
    anchor = re.sub(r"[*_\\]", "", m.group(1)).strip()
    url = m.group(2)
    # keep a URL visible only when it is short and informative (e.g. join.dupaco.com)
    if url.startswith("http") and "dupaco.com" in url and len(url) < 70 and "#" not in url:
        host = urlparse(url).netloc
        if host and not host.startswith("www."):
            return f"{anchor} ({url})" if anchor else url
    return anchor


def clean_markdown(markdown: str, ptype: str, boiler: set[str]) -> list[str]:
    """Return the page as cleaned lines (headings kept with their # prefix)."""
    md = markdown.replace("\\r\\n", "\n").replace("\\\n", " ").replace(" ", " ")
    lines = []
    raws = md.splitlines()
    # a line followed (within 3 lines) by a table is that table's title: never drop it as boilerplate
    titles = set()
    for i, raw in enumerate(raws):
        if raw.lstrip().startswith("|") and (i == 0 or not raws[i - 1].lstrip().startswith("|")):
            for j in range(i - 1, max(-1, i - 13), -1):
                if raws[j].strip():
                    titles.add(j)
                    break
    for k, raw in enumerate(raws):
        line = raw.strip()
        if TEASER_HEADINGS.match(line) and ptype in ("news", "blog", "pdf"):
            break
        if not line:
            lines.append("")
            continue
        if (line in boiler and k not in titles) or CAPTCHA.search(line) or "data:image" in line:
            continue
        if len(line) > 80 and sum(ch.isdigit() for ch in line) > 0.4 * len(line):
            continue   # chart data series on calculator pages
        if TEASER_DATE.match(line) or (line.endswith("...") and len(line) < 220):
            continue   # "Sep 17, 2026" / "Every parent dreams of ... by..." article teasers
        line = IMG_RE.sub("", line)
        if not line.startswith("|"):
            line = LINK_RE.sub(_render_link, line)
        line = line.replace("**", "").replace("\\#", "#").replace("\\|", "/").replace("\\", "").strip()
        if not line or JUNK_LINE.match(line):
            continue
        lines.append(line)
    return lines


# --------------------------------------------------------------------------------------
# Numbers in text
# --------------------------------------------------------------------------------------
NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "eighteen": 18,
            "twenty": 20, "twenty-four": 24, "thirty": 30, "thirty-six": 36, "forty-eight": 48,
            "sixty": 60, "seventy-two": 72, "eighty-four": 84}
_NUMW = "|".join(sorted(NUMWORDS, key=len, reverse=True))
TERM_RE = re.compile(rf"\b(\d{{1,3}}|{_NUMW})[\s-]*(months?|mos?\.?|mo\b|years?|yrs?)\b", re.I)


def term_months(text: str) -> list[int]:
    """'60-month' -> 60, '30-Year' -> 360, 'five-year' -> 60."""
    out = []
    for m in TERM_RE.finditer(text):
        n = m.group(1).lower()
        n = NUMWORDS.get(n) if n in NUMWORDS else int(n)
        unit = m.group(2).lower()
        out.append(n * 12 if unit.startswith("y") else n)
    return out


MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(k|thousand|million|m\b)?", re.I)


def money_values(text: str) -> list[float]:
    out = []
    for m in MONEY_RE.finditer(text):
        v = float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))
        mult = (m.group(3) or "").lower()
        if mult in ("k", "thousand"):
            v *= 1000
        elif mult in ("million", "m"):
            v *= 1_000_000
        out.append(v)
    return out


def amount_range(text: str) -> tuple[float, float] | None:
    """Balance / loan-amount tier of a table label: '$10,000 – $24,999.99', '$25,000+',
    '$100,000 or less', '$10,000-24,999'."""
    t = text.replace("–", "-").replace("—", "-")
    vals = money_values(t)
    # "$10,000-24,999" : second number without a $ sign
    m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s*-\s*\$?\s?([\d,]+(?:\.\d+)?)", t)
    if m:
        lo = float(m.group(1).replace(",", ""))
        hi = float(m.group(2).replace(",", ""))
        if hi > lo:
            return (lo, hi)
    if not vals:
        return None
    if re.search(r"or less|and under|or below|up to", t, re.I):
        return (0.0, vals[0])
    if "+" in t or re.search(r"and (up|over|above)|or more", t, re.I):
        return (vals[0], float("inf"))
    if len(vals) >= 2 and vals[1] > vals[0]:
        return (vals[0], vals[1])
    return None


YEAR_RANGE_RE = re.compile(r"\b((?:19|20)\d\d)\s*[-–]\s*((?:19|20)?\d\d)\b")


def model_years(text: str) -> tuple[int, int] | None:
    """'2024-27 Model Year' -> (2024, 2027); 'Model Year 2026-23' -> (2023, 2026);
    '2016 Model Year (and older)' -> (0, 2016)."""
    if "model" not in text.lower() and "year" not in text.lower():
        return None
    m = YEAR_RANGE_RE.search(text)
    if m:
        a = int(m.group(1))
        b = m.group(2)
        b = int(b) if len(b) == 4 else (a // 100) * 100 + int(b)
        return (min(a, b), max(a, b))
    m = re.search(r"\b((?:19|20)\d\d)\b", text)
    if m:
        y = int(m.group(1))
        if re.search(r"older|prior|before", text, re.I):
            return (0, y)
        if re.search(r"newer|later", text, re.I):
            return (y, 9999)
        return (y, y)
    return None


PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def pct_values(text: str) -> list[float]:
    return [float(x) for x in PCT_RE.findall(text)]


def strip_footnote(cell: str) -> str:
    """'4.20%1' -> '4.20%', '5.39% APR1' -> '5.39% APR', '12.50% – 19.50%2' -> '12.50% – 19.50%'."""
    c = cell.strip()
    c = re.sub(r"(%|APR|APY)\s*[¹²³\d]$", r"\1", c)
    c = re.sub(r"(%|APR|APY)\s*\d$", r"\1", c)
    return c.replace("\\*", "*").strip()
