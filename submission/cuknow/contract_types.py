"""Use the harness's contract classes when available (that is what the scorer expects);
otherwise fall back to identical local dataclasses so the package also works standalone."""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from contract import Answer, Conflict, Snapshot, Source  # harness/ is on sys.path under run_eval.py
except ImportError:  # pragma: no cover - only when run outside the kit
    for parent in Path(__file__).resolve().parents:
        if (parent / "harness" / "contract.py").exists():
            sys.path.insert(0, str(parent / "harness"))
            break
    try:
        from contract import Answer, Conflict, Snapshot, Source
    except ImportError:
        from dataclasses import dataclass, field
        from typing import Optional

        @dataclass
        class Source:
            url: str
            snapshot_id: Optional[str] = None
            quote: Optional[str] = None

        @dataclass
        class Answer:
            text: str
            sources: list = field(default_factory=list)
            confident: bool = True
            conflict_note: Optional[str] = None

        @dataclass
        class Conflict:
            topic: str
            values: list
            sources: list
            resolution: Optional[str] = None
            reason: Optional[str] = None

        Snapshot = None

__all__ = ["Answer", "Conflict", "Snapshot", "Source"]
