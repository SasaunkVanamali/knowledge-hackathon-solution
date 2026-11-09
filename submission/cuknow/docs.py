"""Turn one page (one version of it) into searchable chunks and structured table rows."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .text import (DATE_NUM_RE, amount_range, clean_markdown, extract_links, model_years, page_type,
                   pct_values, publication_date, strip_footnote, term_months, tokens)

# tab / section labels on rates pages, e.g. "- Term-Share Certificate Rates"
TAB_RE = re.compile(r"^-\s+[^|]{3,60}\b(rates?|accounts?|loans?|certificates?|iras?|cards?|savings|checking)\s*$",
                    re.I)
EFFECTIVE_RE = re.compile(r"effective (?:as of )?(\d{1,2}/\d{1,2}/20\d\d)", re.I)
BUTTON_WORDS = re.compile(r"^(explore|see details|learn more|apply today|open my .*|compare .*|see rates|"
                          r"select an option below:?|deposits|loans|depositsloans|apply now|get started)$", re.I)


def _is_pct_cell(c: str) -> bool:
    """A rate cell: short, or a sentence that starts with the rate ("1.99% intro APR for ...")."""
    return "%" in c and (len(c) < 60 or (len(c) < 160 and re.match(r"^\d+(\.\d+)?\s*%", c) is not None))


@dataclass
class Row:
    doc: "Doc"
    table: int                 # index of the table inside the page
    context: str               # table title, e.g. "Special Certificates"
    section: str               # heading above the title, e.g. "Certificates"
    headers: list[str]
    cells: list[str]           # footnote-stripped cells
    effective: str | None      # "09/25/2026"
    label: str = ""            # the non-numeric cells (product / tier / term)
    term: int | None = None    # months
    years: tuple | None = None # model-year range
    amount: tuple | None = None
    text: str = ""             # searchable / display text
    tok: frozenset = frozenset()
    family: str | None = None  # product family (see products.py)
    kind: str = "table"        # "table" row, or "text" for a rate stated in a sentence

    def pct_cells(self) -> list[tuple[str, str]]:
        return [(h, c) for h, c in zip(self.headers, self.cells) if _is_pct_cell(c)]

    def value_str(self) -> str:
        """Compact value string: 'Rate 7.25%, APR 7.29%' / '4.20% APY'."""
        parts = []
        for h, c in self.pct_cells():
            hl = h.lower()
            if "annual percentage yield" in hl or "apy" in hl:
                name = "APY"
            elif "annual percentage rate" in hl or hl.startswith("apr"):
                name = "APR"
            elif "dividend" in hl:
                name = "dividend rate"
            elif hl in ("rate", "interest rate"):
                name = "rate"
            else:
                name = re.sub(r"\s*\(.*?\)", "", h).strip() or "rate"
            parts.append(c if re.search(r"\b(APR|APY)\b", c) else f"{name} {c}")
        return ", ".join(parts)

    def primary(self) -> str:
        """The single value most people mean: APY for deposits, APR for loans, else first %."""
        pc = self.pct_cells()
        for want in ("annual percentage yield", "apy", "apr", "annual percentage rate"):
            for h, c in pc:
                if want in h.lower():
                    return c
        return pc[-1][1] if pc else ""


@dataclass
class Chunk:
    doc: "Doc"
    text: str
    heading: str
    kind: str = "text"         # "text" | "row"
    pos: int = 0               # position of the chunk inside its page
    row: Row | None = None
    tok: list = field(default_factory=list)


@dataclass
class Doc:
    url: str
    title: str
    ptype: str
    pub: date | None
    h1: str
    chunks: list[Chunk]
    rows: list[Row]
    links: list[tuple[str, str, str]]
    lines: list[str]


def _table_context(lines: list[str], start: int) -> tuple[str, str]:
    found = []
    i = start - 1
    while i >= 0 and len(found) < 2 and start - i < 14:
        s = lines[i].strip().lstrip("#").strip().lstrip("-").strip()
        i -= 1
        if not s or s.startswith("|") or len(s) > 80 or BUTTON_WORDS.match(s):
            continue
        if re.match(r"^\d[A-Z*]", s) or s.lower().startswith(("rates are effective", "*")):
            continue
        if re.match(r"^\[?[\w\s]+\]?$", s) or len(s) <= 80:
            found.append(s.strip("[]"))
    ctx = found[0] if found else ""
    sec = found[1] if len(found) > 1 else ""
    return ctx, sec


TEXT_PCT = re.compile(r"(\d+(?:\.\d+)?\s*%(?:\s*(?:APY|APR))?)")


def _text_fact(line: str, context: str, section: str) -> "Row | None":
    """A rate stated in a sentence ("6-month certificate APY: 3.50%"): one percentage, short line."""
    if len(line) > 200 or line.startswith(("|", "#")) or re.match(r"^\d[A-Z]", line):
        return None
    if re.search(r"example|if you borrow|payment obligation|would be \$", line, re.I):
        return None
    vals = TEXT_PCT.findall(line)
    if len(vals) != 1:
        return None
    label = TEXT_PCT.sub("", line).strip(" :;-–—,.")
    label = re.sub(r"\s{2,}", " ", label)
    if len(label) < 4:
        return None
    val = vals[0].strip()
    head = "APY" if re.search(r"\bAPY\b|yield", line, re.I) else ("APR" if re.search(r"\bAPR\b", line) else "Rate")
    r = Row(doc=None, table=0, context=context, section=section, headers=["", head], cells=[label, val],
            effective=None, label=label, kind="text")
    tm = term_months(label)
    r.term = tm[0] if tm else None
    r.years = model_years(label) if re.search(r"model", label, re.I) else None
    r.amount = amount_range(label)
    r.text = line
    r.tok = frozenset(tokens(f"{context} {section} {label}"))
    if re.search(r"offer ended|expired|no longer (available|offered)|ended on", line, re.I):
        r.kind = "expired"
    return r


def _split_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_doc(url: str, title: str, markdown: str, boiler: set[str]) -> Doc:
    ptype = page_type(url)
    pub = publication_date(url, markdown) if ptype in ("news", "blog") else None
    lines = clean_markdown(markdown, ptype, boiler)
    links = extract_links(markdown)
    h1 = next((l[2:].strip() for l in lines if l.startswith("# ")), title)
    clean_title = re.sub(r"\s*\|\s*Dupaco\s*$", "", title or h1).strip()

    chunks: list[Chunk] = []
    rows: list[Row] = []
    heading = ""
    buf: list[str] = []

    def flush():
        nonlocal buf
        if buf:
            text = " ".join(buf).strip()
            if len(text) > 3:
                for piece in _split_long(text):
                    chunks.append(Chunk(doc=None, text=piece, heading=heading))
        buf = []

    i, n, tcount, tab = 0, len(lines), 0, ""
    while i < n:
        line = lines[i]
        if line.startswith("|"):
            flush()
            tbl = []
            while i < n and lines[i].startswith("|"):
                tbl.append(lines[i])
                i += 1
            ctx, sec = _table_context(lines, i - len(tbl))
            if tab and tab.lower() not in f"{ctx} {sec}".lower():
                sec = f"{sec} / {tab}".strip(" /")
            eff = None
            for j in range(i, min(n, i + 6)):
                m = EFFECTIVE_RE.search(lines[j])
                if m:
                    eff = m.group(1)
                    break
            _parse_table(tbl, ctx, sec, eff, tcount, rows, chunks, heading)
            tcount += 1
            continue
        if TAB_RE.match(line):
            tab = line.lstrip("- ").strip()
        if line.startswith("#"):
            flush()
            heading = line.lstrip("#").strip()
            # a heading line is also content (branch names in the locator, etc.)
            buf.append(heading + ".")
            i += 1
            continue
        if not line:
            # merge short consecutive paragraphs (addresses, hours, lists)
            if buf and len(" ".join(buf)) > 350:
                flush()
            elif buf and len(buf[-1]) > 200:
                flush()
            i += 1
            continue
        fact = _text_fact(line, heading or h1, h1)
        if fact is not None:
            fact.table = 1000 + len(rows)
            rows.append(fact)
        buf.append(line if line.endswith((".", ":", "!", "?")) else line + (";" if len(line) < 60 else ""))
        i += 1
    flush()

    doc = Doc(url=url, title=clean_title, ptype=ptype, pub=pub, h1=h1, chunks=chunks, rows=rows,
              links=links, lines=lines)
    head_tok = tokens(clean_title)
    for k, c in enumerate(chunks):
        c.doc = doc
        c.pos = k
        c.tok = tokens(c.text) + tokens(c.heading) + head_tok
    for r in rows:
        r.doc = doc
    return doc


SENT_RE = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9“\"(])")


def _split_long(text: str, size: int = 520) -> list[str]:
    if len(text) <= size + 150:
        return [text]
    sents = SENT_RE.split(text)
    out, cur = [], ""
    for s in sents:
        if cur and len(cur) + len(s) > size:
            out.append(cur.strip())
            cur = ""
        cur += " " + s
    if cur.strip():
        out.append(cur.strip())
    return out


def _parse_table(tbl, ctx, sec, eff, tidx, rows, chunks, heading):
    if len(tbl) >= 2 and re.match(r"^\|?\s*:?-{2,}", tbl[1].strip().strip("|").strip()):
        headers = _split_cells(tbl[0])
        body = tbl[2:]
    else:
        headers, body = [], tbl
    headers = [re.sub(r"\d$", "", h).replace("\\*", "").strip() for h in headers]
    for line in body:
        raw = _split_cells(line)
        cells = [strip_footnote(c) for c in raw]
        if not any(c for c in cells):
            continue
        hdr = headers + [""] * (len(cells) - len(headers))
        label_cells = [c for h, c in zip(hdr, cells)
                       if c and "%" not in c and not re.fullmatch(r"[\d.,$]+|N/A|None|—|-", c)
                       and not re.search(r"\bmin\b|min\.|payment|points", h, re.I)]
        label = " | ".join(label_cells[:3])
        has_pct = any(_is_pct_cell(c) for c in cells)
        text_parts = [f"{h}: {c}" if h else c for h, c in zip(hdr, cells) if c]
        prefix = ctx if ctx and ctx.lower() not in label.lower() else ""
        text = (prefix + " — " if prefix else "") + "; ".join(text_parts)
        if has_pct:
            row = Row(doc=None, table=tidx, context=ctx, section=sec, headers=hdr, cells=cells, effective=eff,
                      label=label)
            lab_ctx = label + " " + ctx
            tm = term_months(label) or term_months(" ".join(cells[:2]))
            row.term = tm[0] if tm else None
            row.years = model_years(label) if re.search(r"model|year", lab_ctx, re.I) else None
            if row.years and row.term and row.term >= 120 and "year" in label.lower() and "model" not in label.lower():
                row.years = None
            row.amount = amount_range(label)
            row.text = text
            row.tok = frozenset(tokens(f"{ctx} {sec} {label} {' '.join(h for h in hdr if h)}"))
            rows.append(row)
            chunks.append(Chunk(doc=None, text=text, heading=heading, kind="row", row=row))
        else:
            chunks.append(Chunk(doc=None, text=text, heading=heading, kind="text"))


def effective_date(row: Row):
    if not row.effective:
        return None
    m = DATE_NUM_RE.search(row.effective)
    if not m:
        return None
    from datetime import date as _d
    try:
        return _d(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


__all__ = ["Doc", "Chunk", "Row", "parse_doc", "effective_date", "pct_values"]
