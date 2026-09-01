"""Okapi BM25 без pip-зависимости (формула как у rank_bm25.BM25Okapi)."""

from __future__ import annotations

import math
from collections import Counter


class BM25Okapi:
    """Корпус -- список токенизированных документов. Пустой корпус сюда не передают."""

    def __init__(
        self,
        corpus: list[list[str]],
        *,
        k1: float = 1.5,
        b: float = 0.75,
        epsilon: float = 0.25,
    ) -> None:
        """Посчитать IDF и длины документов; k1/b как у rank_bm25."""
        self.k1 = k1
        self.b = b
        self.corpus_size = len(corpus)
        self.doc_len = [len(doc) for doc in corpus]
        self.avgdl = sum(self.doc_len) / self.corpus_size
        self.doc_freqs = [Counter(doc) for doc in corpus]
        df: dict[str, int] = {}
        for freq in self.doc_freqs:
            for word in freq:
                df[word] = df.get(word, 0) + 1
        self.idf: dict[str, float] = {}
        idf_sum = 0.0
        negative: list[str] = []
        for word, count in df.items():
            idf = math.log(self.corpus_size - count + 0.5) - math.log(count + 0.5)
            self.idf[word] = idf
            idf_sum += idf
            if idf < 0:
                negative.append(word)
        eps = epsilon * (idf_sum / len(self.idf))
        for word in negative:
            self.idf[word] = eps

    def get_scores(self, query: list[str]) -> list[float]:
        """Скор каждого документа по запросу, тот же порядок, что корпус."""
        scores = [0.0] * self.corpus_size
        for q in query:
            idf = self.idf.get(q)
            if idf is None:
                continue
            for i, freq in enumerate(self.doc_freqs):
                f = freq.get(q, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1.0 - self.b + self.b * self.doc_len[i] / self.avgdl)
                scores[i] += idf * (f * (self.k1 + 1.0)) / denom
        return scores
