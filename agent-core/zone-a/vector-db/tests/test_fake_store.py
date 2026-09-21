from __future__ import annotations

import pytest
from afe_vector_memory import InMemoryStore, MemoryUnavailableError
from tests.helpers import make_record


@pytest.mark.parametrize("operation", ["add", "query", "cleanup", "count", "ping"])
def test_fail_with_makes_every_call_raise(operation: str) -> None:
    store = InMemoryStore()
    store.fail_with = OSError("boom")
    actions = {
        "add": lambda: store.add(make_record()),
        "query": lambda: store.query("x"),
        "cleanup": store.cleanup_expired,
        "count": store.count,
        "ping": store.ping,
    }
    with pytest.raises(MemoryUnavailableError, match="boom"):
        actions[operation]()


def test_recovers_after_failure_is_cleared() -> None:
    store = InMemoryStore()
    store.fail_with = OSError("boom")
    store.fail_with = None
    store.add(make_record())
    assert store.count() == 1
