"""A tiny Chroma stand-in that understands the `where` operators ChromaStore emits."""

from __future__ import annotations

import math
from typing import Any


def _matches(meta: dict[str, Any], where: dict[str, Any]) -> bool:
    if "$and" in where:
        return all(_matches(meta, clause) for clause in where["$and"])
    ((field, cond),) = where.items()
    ((op, value),) = cond.items()
    actual = meta[field]
    return {
        "$eq": actual == value,
        "$gt": actual > value,
        "$lte": actual <= value,
    }[op]


class FakeCollection:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[list[float], str, dict[str, Any]]] = {}
        self.fail: Exception | None = None
        self.ignore_deletes = False
        self.raw_query_result: Any = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _check(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if self.fail is not None:
            raise self.fail

    def upsert(self, **kwargs: Any) -> None:
        self._check("upsert", kwargs)
        for rid, emb, doc, meta in zip(
            kwargs["ids"], kwargs["embeddings"], kwargs["documents"], kwargs["metadatas"],
            strict=True,
        ):
            self.rows[rid] = (emb, doc, meta)

    def query(self, **kwargs: Any) -> Any:
        self._check("query", kwargs)
        if self.raw_query_result is not None:
            return self.raw_query_result
        (query,) = kwargs["query_embeddings"]
        scored = []
        for rid, (emb, doc, meta) in self.rows.items():
            if not _matches(meta, kwargs["where"]):
                continue
            dot = sum(a * b for a, b in zip(query, emb, strict=True))
            norm = math.sqrt(sum(a * a for a in query)) * math.sqrt(sum(b * b for b in emb))
            scored.append((1.0 - dot / norm, rid, doc, meta))
        scored.sort(key=lambda row: (row[0], row[1]))
        top = scored[: kwargs["n_results"]]
        return {
            "ids": [[r[1] for r in top]],
            "documents": [[r[2] for r in top]],
            "metadatas": [[r[3] for r in top]],
            "distances": [[r[0] for r in top]],
        }

    def get(self, **kwargs: Any) -> Any:
        self._check("get", kwargs)
        ids = [rid for rid, (_, _, meta) in self.rows.items() if _matches(meta, kwargs["where"])]
        return {"ids": ids[: kwargs["limit"]]}

    def delete(self, **kwargs: Any) -> None:
        self._check("delete", kwargs)
        if not self.ignore_deletes:
            for rid in kwargs["ids"]:
                self.rows.pop(rid, None)

    def count(self) -> int:
        self._check("count", {})
        return len(self.rows)


class FakeClient:
    def __init__(self, collection: FakeCollection | None = None) -> None:
        self.collection = collection or FakeCollection()
        self.heartbeat_error: Exception | None = None
        self.created_with: dict[str, Any] = {}

    def heartbeat(self) -> int:
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        return 1

    def get_or_create_collection(self, name: str, **kwargs: Any) -> FakeCollection:
        self.created_with = {"name": name, **kwargs}
        return self.collection
