"""
Knowledge system for the "what's true now" hackathon.

build() returns an object with the three contract methods:
    ingest(snapshot)              - called once per snapshot, oldest first
    answer(question, as_of=None)  - current answer, or the answer as the site stood on `as_of`
    conflicts()                   - every contradiction found, with the value we believe and why

How it works (details in NOTES.md):
  * every snapshot becomes a "view"; unchanged pages are parsed once and shared between views
  * pages are classified (official rates page, product page, calculator, landing page, PDF,
    news, blog) and that class decides how much a page is trusted
  * rate tables are parsed into structured rows (product family, term, model years, balance tier,
    qualifiers such as jumbo / business / IRA) so tiers are never confused with conflicts
  * questions are routed to a rates, hours, link, or general-retrieval (BM25) path; when the
    site doesn't publish the answer we say so instead of guessing
Pure standard library: no API keys, no network, deterministic.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cuknow.conflicts import find_conflicts  # noqa: E402
from cuknow.contract_types import Answer  # noqa: E402
from cuknow.engine import Engine  # noqa: E402
from cuknow.store import Store  # noqa: E402


class KnowledgeSystem:
    def __init__(self):
        self.store = Store()
        self.engine = Engine(self.store)
        self._conflicts = None

    def ingest(self, snapshot) -> None:
        self.store.ingest(snapshot)
        self.engine._cache.clear()
        self._conflicts = None

    def answer(self, question: str, as_of: str | None = None):
        if not self.store.views:
            return Answer(text="I don't know yet - no website snapshots have been loaded.", confident=False)
        try:
            return self.engine.answer(question, as_of)
        except Exception as e:  # never crash the caller; an honest "don't know" beats a stack trace
            if os.environ.get("CUKNOW_DEBUG"):
                raise
            return Answer(text="I'm sorry, I can't answer that right now. Please contact Dupaco directly.",
                          confident=False, conflict_note=f"internal error: {type(e).__name__}")

    def conflicts(self):
        if self._conflicts is None:
            try:
                self._conflicts = find_conflicts(self.store) if self.store.views else []
            except Exception:
                if os.environ.get("CUKNOW_DEBUG"):
                    raise
                self._conflicts = []
        return list(self._conflicts)


def build():
    return KnowledgeSystem()
