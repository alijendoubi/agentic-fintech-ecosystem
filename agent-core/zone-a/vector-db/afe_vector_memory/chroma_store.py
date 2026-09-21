"""Chroma-backed `MemoryStore`.

Embeddings are always computed here with the injected `Embedder` and passed to Chroma
explicitly, and the collection is created with `embedding_function=None`, so Chroma never
loads or downloads its default ONNX model. Every failure that originates in the client or
the server is re-raised as `MemoryUnavailableError` (fail closed): the caller can never
mistake an outage for "no precedent".
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from .config import VectorMemorySettings
from .embedding import Embedder, HashingEmbedder, embed_checked
from .errors import MemoryUnavailableError, MemoryValidationError, VectorMemoryError
from .fake import wall_clock_ms
from .models import (
    MemoryHit,
    MemoryKind,
    MemoryRecord,
    Outcome,
    RetentionPolicy,
    validate_query_filters,
)
from .store import DEFAULT_MAX_RESULTS, DEFAULT_N_RESULTS

CLEANUP_BATCH = 500
CLEANUP_MAX_BATCHES = 2000
COLLECTION_CONFIG: dict[str, Any] = {"hnsw": {"space": "cosine"}}


class ChromaCollection(Protocol):
    """The slice of `chromadb.Collection` used here."""

    def upsert(self, **kwargs: Any) -> Any: ...
    def query(self, **kwargs: Any) -> Any: ...
    def get(self, **kwargs: Any) -> Any: ...
    def delete(self, **kwargs: Any) -> Any: ...
    def count(self) -> int: ...


class ChromaClient(Protocol):
    """The slice of `chromadb.ClientAPI` used here."""

    def heartbeat(self) -> Any: ...
    def get_or_create_collection(self, name: str, **kwargs: Any) -> ChromaCollection: ...


def build_where(
    *,
    now_ms: int,
    regime: str | None = None,
    symbol: str | None = None,
    kind: MemoryKind | None = None,
) -> dict[str, Any]:
    """Chroma `where` clause: unexpired, plus the optional equality filters."""
    clauses: list[dict[str, Any]] = [{"expires_ms": {"$gt": now_ms}}]
    if regime is not None:
        clauses.append({"regime": {"$eq": regime}})
    if symbol is not None:
        clauses.append({"symbol": {"$eq": symbol}})
    if kind is not None:
        clauses.append({"kind": {"$eq": str(kind)}})
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def _metadata(record: MemoryRecord, expires_ms: int) -> dict[str, str | int]:
    return {
        "kind": str(record.kind),
        "symbol": record.symbol,
        "regime": record.regime,
        "ts_ms": record.ts_ms,
        "expires_ms": expires_ms,
        "outcome": str(record.outcome),
        "signal_id": record.signal_id,
    }


def _record_from(record_id: object, document: object, meta: object) -> MemoryRecord:
    if not isinstance(meta, Mapping) or not isinstance(document, str):
        raise MemoryUnavailableError("store returned a record without metadata or document")
    try:
        return MemoryRecord(
            record_id=str(record_id),
            kind=MemoryKind(meta["kind"]),
            text=document,
            symbol=meta["symbol"],
            regime=meta["regime"],
            ts_ms=meta["ts_ms"],
            outcome=Outcome(meta["outcome"]),
            signal_id=meta.get("signal_id", ""),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise MemoryUnavailableError(f"store returned a malformed record: {exc}") from exc


def _first_row(result: Mapping[str, Any], key: str) -> Sequence[Any]:
    try:
        rows = result[key]
        return rows[0] if rows is not None else []
    except (KeyError, IndexError, TypeError) as exc:
        raise MemoryUnavailableError(f"store reply is missing '{key}'") from exc


class ChromaStore:
    """`MemoryStore` over one Chroma collection."""

    def __init__(
        self,
        collection: ChromaCollection,
        embedder: Embedder | None = None,
        retention: RetentionPolicy | None = None,
        clock: Callable[[], int] = wall_clock_ms,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> None:
        self._collection = collection
        self._embedder = embedder or HashingEmbedder()
        self._retention = retention or RetentionPolicy()
        self._clock = clock
        self._max_results = max_results

    def add(self, record: MemoryRecord) -> None:
        if not isinstance(record, MemoryRecord):
            raise MemoryValidationError("add() requires a MemoryRecord")
        vector = embed_checked(self._embedder, [record.text])[0]
        expires = self._retention.expires_at_ms(record.kind, record.ts_ms)
        self._call(
            self._collection.upsert,
            ids=[record.record_id],
            embeddings=[vector],
            documents=[record.text],
            metadatas=[_metadata(record, expires)],
        )

    def query(
        self,
        text: str,
        *,
        n_results: int = DEFAULT_N_RESULTS,
        regime: str | None = None,
        symbol: str | None = None,
        kind: MemoryKind | None = None,
    ) -> list[MemoryHit]:
        validate_query_filters(
            n_results=n_results,
            regime=regime,
            symbol=symbol,
            kind=kind,
            max_results=self._max_results,
        )
        if not isinstance(text, str) or not text.strip():
            raise MemoryValidationError("query text must be a non-empty string")
        vector = embed_checked(self._embedder, [text])[0]
        result = self._call(
            self._collection.query,
            query_embeddings=[vector],
            n_results=n_results,
            where=build_where(now_ms=self._clock(), regime=regime, symbol=symbol, kind=kind),
            include=["documents", "metadatas", "distances"],
        )
        ids = _first_row(result, "ids")
        documents = _first_row(result, "documents")
        metadatas = _first_row(result, "metadatas")
        distances = _first_row(result, "distances")
        if not len(ids) == len(documents) == len(metadatas) == len(distances):
            raise MemoryUnavailableError("store reply has rows of differing lengths")
        try:
            return [
                MemoryHit(_record_from(rid, doc, meta), float(dist))
                for rid, doc, meta, dist in zip(ids, documents, metadatas, distances, strict=True)
            ]
        except (TypeError, ValueError, MemoryValidationError) as exc:
            raise MemoryUnavailableError(f"store returned a malformed hit: {exc}") from exc

    def cleanup_expired(self) -> int:
        removed = 0
        for _ in range(CLEANUP_MAX_BATCHES):
            result = self._call(
                self._collection.get,
                where={"expires_ms": {"$lte": self._clock()}},
                include=[],
                limit=CLEANUP_BATCH,
            )
            try:
                ids = list(result["ids"])
            except (KeyError, TypeError) as exc:
                raise MemoryUnavailableError("store reply is missing 'ids'") from exc
            if not ids:
                return removed
            self._call(self._collection.delete, ids=ids)
            removed += len(ids)
        raise MemoryUnavailableError("cleanup did not converge; store may be ignoring deletes")

    def count(self) -> int:
        total = self._call(self._collection.count)
        if isinstance(total, bool) or not isinstance(total, int):
            raise MemoryUnavailableError("store returned a non-integer count")
        return total

    def ping(self) -> None:
        self.count()

    @staticmethod
    def _call(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except VectorMemoryError:
            raise
        except Exception as exc:  # noqa: BLE001 - any client/server failure means "unavailable"
            detail = f"{type(exc).__name__}: {exc}"
            raise MemoryUnavailableError(f"chroma call failed: {detail}") from exc


def open_chroma_store(
    settings: VectorMemorySettings,
    *,
    embedder: Embedder | None = None,
    client: ChromaClient | None = None,
    clock: Callable[[], int] = wall_clock_ms,
) -> ChromaStore:
    """Connect (HttpClient by default), verify reachability, and open the collection.

    chromadb's HttpClient has no timeout parameter (its httpx session uses `timeout=None`),
    so connecting runs under `settings.connect_timeout_s` in a daemon thread
    (client construction alone takes 2-3 s cold). Raises
    `MemoryUnavailableError` if the server cannot be reached in time; there is no offline
    fallback, so a caller that starts without memory must do so explicitly.
    """

    make_client = (lambda: client) if client is not None else _http_client_factory(settings)

    def connect() -> ChromaCollection:
        opened = make_client()
        opened.heartbeat()
        return opened.get_or_create_collection(
            settings.collection, configuration=COLLECTION_CONFIG, embedding_function=None
        )

    collection = ChromaStore._call(run_with_deadline, connect, settings.connect_timeout_s)  # noqa: SLF001
    return ChromaStore(collection, embedder, settings.retention, clock, settings.max_results)


def run_with_deadline[T](fn: Callable[[], T], timeout_s: float) -> T:
    """Run `fn` in a daemon thread; raise `MemoryUnavailableError` if it outlives `timeout_s`.

    The thread cannot be killed, so a hung call is abandoned (daemon: it never blocks exit).
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = exc

    worker = threading.Thread(target=target, name="chroma-call", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise MemoryUnavailableError(f"chroma call exceeded {timeout_s}s")
    if "error" in box:
        raise box["error"]
    return box["value"]  # type: ignore[no-any-return]


def _http_client_factory(settings: VectorMemorySettings) -> Callable[[], ChromaClient]:
    """Import chromadb now (slow, must not count against the deadline); connect later."""
    try:
        import chromadb
        from chromadb.config import Settings as ChromaSettings
    except ImportError as exc:  # pragma: no cover - depends on the deployment image
        raise MemoryUnavailableError("chromadb is not installed") from exc

    def make() -> ChromaClient:
        return chromadb.HttpClient(  # type: ignore[no-any-return]
            host=settings.host,
            port=settings.port,
            ssl=settings.ssl,
            settings=ChromaSettings(anonymized_telemetry=False),
        )

    return make
