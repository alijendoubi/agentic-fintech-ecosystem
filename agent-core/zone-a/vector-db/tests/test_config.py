from __future__ import annotations

import pytest
from afe_vector_memory import MemoryConfigError, RetentionPolicy, VectorMemorySettings


def test_defaults_from_empty_env() -> None:
    settings = VectorMemorySettings.from_env({})
    assert settings == VectorMemorySettings()
    assert (settings.host, settings.port, settings.ssl) == ("localhost", 8000, False)
    assert settings.retention == RetentionPolicy(90, 365)


def test_compose_style_env() -> None:
    settings = VectorMemorySettings.from_env(
        {
            "CHROMA_HOST": "vector-db",
            "CHROMA_PORT": "8000",
            "CHROMA_SSL": "TRUE",
            "CHROMA_TIMEOUT_S": "1.5",
            "CHROMA_CONNECT_TIMEOUT_S": "45",
            "MEMORY_COLLECTION": "afe.memory-v1",
            "MEMORY_RETENTION_DAYS_DEBATE": "30",
            "MEMORY_RETENTION_DAYS_REFLECTION": "400",
            "MEMORY_MAX_RESULTS": "50",
        }
    )
    assert settings.host == "vector-db"
    assert settings.ssl is True
    assert settings.timeout_s == 1.5
    assert settings.connect_timeout_s == 45.0
    assert settings.collection == "afe.memory-v1"
    assert settings.retention == RetentionPolicy(30, 400)
    assert settings.max_results == 50


def test_reads_process_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHROMA_HOST", "chroma.internal")
    assert VectorMemorySettings.from_env().host == "chroma.internal"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CHROMA_HOST", "http://vector-db"),
        ("CHROMA_HOST", "vector-db:8000"),
        ("CHROMA_HOST", ""),
        ("CHROMA_PORT", "0"),
        ("CHROMA_PORT", "70000"),
        ("CHROMA_PORT", "eight"),
        ("CHROMA_SSL", "maybe"),
        ("CHROMA_TIMEOUT_S", "0"),
        ("CHROMA_TIMEOUT_S", "nan"),
        ("CHROMA_TIMEOUT_S", "61"),
        ("CHROMA_CONNECT_TIMEOUT_S", "0.5"),
        ("MEMORY_COLLECTION", "a"),
        ("MEMORY_COLLECTION", "bad name!"),
        ("MEMORY_RETENTION_DAYS_DEBATE", "0"),
        ("MEMORY_RETENTION_DAYS_REFLECTION", "99999"),
        ("MEMORY_MAX_RESULTS", "0"),
        ("MEMORY_MAX_RESULTS", "101"),
    ],
)
def test_invalid_values_fail_closed(name: str, value: str) -> None:
    with pytest.raises(MemoryConfigError, match=name):
        VectorMemorySettings.from_env({name: value})
