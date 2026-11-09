"""A small BM25 inverted index (pure Python)."""
from __future__ import annotations

import math
from collections import Counter, defaultdict


class BM25:
    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.2, b: float = 0.6):
        self.k1, self.b = k1, b
        self.N = len(docs_tokens)
        self.len = [len(t) for t in docs_tokens]
        self.avg = (sum(self.len) / self.N) if self.N else 1.0
        self.post: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, toks in enumerate(docs_tokens):
            for t, c in Counter(toks).items():
                self.post[t].append((i, c))
        self.idf = {t: math.log(1 + (self.N - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.post.items()}

    def search(self, qweights: dict[str, float], top: int = 50) -> list[tuple[int, float]]:
        scores: dict[int, float] = defaultdict(float)
        k1, b, avg = self.k1, self.b, self.avg
        for t, w in qweights.items():
            plist = self.post.get(t)
            if not plist:
                continue
            idf = self.idf[t] * w
            for i, tf in plist:
                scores[i] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * self.len[i] / avg))
        return sorted(scores.items(), key=lambda x: -x[1])[:top]
