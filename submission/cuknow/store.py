"""Versioned store: one "view" of the website per snapshot, sharing parsed pages between
snapshots whenever a page did not change."""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from .docs import Chunk, Doc, Row, parse_doc
from .index import BM25
from .products import row_family
from .text import parse_iso


@dataclass
class View:
    sid: str
    day: date
    docs: dict[str, Doc]
    chunks: list[Chunk] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    bm25: BM25 | None = None
    carried: set = field(default_factory=set)   # urls not in this crawl, kept from an earlier one


class Store:
    def __init__(self):
        self.views: list[View] = []
        self._cache: dict[str, Doc] = {}
        self.boiler: set[str] = set()

    # ---------------------------------------------------------------- ingest
    def ingest(self, snapshot) -> View:
        pages = [p for p in snapshot.pages if p.markdown]
        # Lines repeated on many pages are navigation / footer / widget boilerplate.
        cnt = Counter()
        for p in pages:
            cnt.update({l.strip() for l in p.markdown.splitlines() if l.strip() and not l.lstrip().startswith("|")})
        limit = max(12, int(0.04 * len(pages)))
        self.boiler |= {l for l, c in cnt.items() if c >= limit and len(l) < 400}

        docs: dict[str, Doc] = {}
        for p in pages:
            h = hashlib.md5((p.url + "\0" + p.markdown).encode("utf-8")).hexdigest()
            d = self._cache.get(h)
            if d is None:
                d = parse_doc(p.url, p.title, p.markdown, self.boiler)
                for r in d.rows:
                    r.family = row_family(f"{r.context} {r.section} {r.label}")
                self._cache[h] = d
            docs[p.url] = d

        view = View(sid=snapshot.snapshot_id, day=parse_iso(snapshot.taken_on) or date.today(), docs=docs)
        # A page missing from this crawl is not necessarily gone from the site (crawls miss pages):
        # keep its last known version, flagged as "carried" so it is trusted less.
        if self.views:
            prev = self.views[-1]
            for url, d in prev.docs.items():
                if url not in docs and d.ptype not in ("listing", "junk"):
                    docs[url] = d
                    view.carried.add(url)
        for d in docs.values():
            if d.ptype == "junk":
                continue
            view.chunks.extend(d.chunks)
            view.rows.extend(d.rows)
        view.bm25 = BM25([c.tok for c in view.chunks])
        self.views.append(view)
        self.views.sort(key=lambda v: v.day)
        return view

    # ---------------------------------------------------------------- time travel
    def view_for(self, as_of: date | None) -> View:
        if not self.views:
            raise RuntimeError("nothing ingested")
        if as_of is None:
            return self.views[-1]
        best = self.views[0]
        for v in self.views:
            if v.day <= as_of:
                best = v
        return best

    def latest(self) -> View:
        return self.views[-1]
