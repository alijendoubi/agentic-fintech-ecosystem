"""ALI-30: models are HMAC-verified, pickle-free, and refused when tampered."""

from __future__ import annotations

import io
import pickle
import zipfile
from pathlib import Path

import numpy as np
import pytest
from regime_detector.features import extract_features
from regime_detector.model import ModelError, TrainedModel, predict_regime, train_model
from regime_detector.model_store import (
    MAGIC,
    ModelStore,
    deserialize_model,
    serialize_model,
)
from synthetic import make_columns

KEY = b"k" * 32


@pytest.fixture(scope="module")
def model_and_features() -> tuple[TrainedModel, np.ndarray]:
    rng = np.random.default_rng(99)
    build = extract_features(*make_columns(rng, n_per_regime=60))
    assert build.matrix is not None
    return train_model(build.matrix, trained_at=42.0), build.matrix


def test_round_trip_preserves_predictions(
    tmp_path: Path, model_and_features: tuple[TrainedModel, np.ndarray]
) -> None:
    model, features = model_and_features
    store = ModelStore(tmp_path, KEY)
    assert store.save("AAPL", model)
    loaded = store.load("AAPL")
    assert loaded is not None
    assert loaded.labels == model.labels
    assert loaded.trained_at == 42.0
    assert np.allclose(loaded.scaler.mean, model.scaler.mean)
    assert predict_regime(loaded, features) == predict_regime(model, features)


def test_file_contains_no_pickle(
    tmp_path: Path, model_and_features: tuple[TrainedModel, np.ndarray]
) -> None:
    model, _ = model_and_features
    ModelStore(tmp_path, KEY).save("AAPL", model)
    blob = (tmp_path / "regime_AAPL.model").read_bytes()
    assert blob.startswith(MAGIC)
    payload = blob[len(MAGIC) + 32 :]
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert all(name.endswith(".npy") for name in archive.namelist())
    # allow_pickle=False: loading must never need a pickle opcode stream
    with np.load(io.BytesIO(payload), allow_pickle=False) as arrays:
        assert "means" in arrays.files


def test_tampered_payload_is_refused(
    tmp_path: Path, model_and_features: tuple[TrainedModel, np.ndarray]
) -> None:
    model, _ = model_and_features
    store = ModelStore(tmp_path, KEY)
    store.save("AAPL", model)
    path = tmp_path / "regime_AAPL.model"
    blob = bytearray(path.read_bytes())
    blob[-20] ^= 0xFF
    path.write_bytes(bytes(blob))
    assert store.load("AAPL") is None


def test_wrong_key_is_refused(
    tmp_path: Path, model_and_features: tuple[TrainedModel, np.ndarray]
) -> None:
    model, _ = model_and_features
    ModelStore(tmp_path, KEY).save("AAPL", model)
    assert ModelStore(tmp_path, b"z" * 32).load("AAPL") is None


def test_valid_file_cannot_be_replayed_under_another_symbol(
    tmp_path: Path, model_and_features: tuple[TrainedModel, np.ndarray]
) -> None:
    model, _ = model_and_features
    store = ModelStore(tmp_path, KEY)
    store.save("AAPL", model)
    (tmp_path / "regime_MSFT.model").write_bytes((tmp_path / "regime_AAPL.model").read_bytes())
    assert store.load("MSFT") is None


def test_pickle_file_in_place_of_model_is_refused_and_not_executed(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"

    class Evil:
        def __reduce__(self) -> tuple[object, tuple[object, ...]]:
            return (marker.write_text, ("x",))

    (tmp_path / "regime_AAPL.model").write_bytes(pickle.dumps(Evil()))
    assert ModelStore(tmp_path, KEY).load("AAPL") is None
    assert not marker.exists()


@pytest.mark.parametrize("content", [b"", b"short", MAGIC, MAGIC + b"\x00" * 32])
def test_garbage_files_are_refused(tmp_path: Path, content: bytes) -> None:
    (tmp_path / "regime_AAPL.model").write_bytes(content)
    assert ModelStore(tmp_path, KEY).load("AAPL") is None


def test_oversized_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "regime_AAPL.model").write_bytes(MAGIC + b"\x00" * 2_000_000)
    assert ModelStore(tmp_path, KEY).load("AAPL") is None


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert ModelStore(tmp_path, KEY).load("AAPL") is None


def test_no_key_disables_load_and_save(
    tmp_path: Path, model_and_features: tuple[TrainedModel, np.ndarray]
) -> None:
    model, _ = model_and_features
    keyed = ModelStore(tmp_path, KEY)
    keyed.save("AAPL", model)
    keyless = ModelStore(tmp_path, None)
    assert not keyless.enabled
    assert keyless.load("AAPL") is None
    assert not keyless.save("MSFT", model)
    assert not (tmp_path / "regime_MSFT.model").exists()


def test_invalid_symbol_never_touches_filesystem(
    tmp_path: Path, model_and_features: tuple[TrainedModel, np.ndarray]
) -> None:
    model, _ = model_and_features
    store = ModelStore(tmp_path, KEY)
    assert not store.save("../evil", model)
    assert store.load("../evil") is None
    assert list(tmp_path.iterdir()) == []


def test_save_failure_is_reported_not_raised(
    tmp_path: Path, model_and_features: tuple[TrainedModel, np.ndarray]
) -> None:
    model, _ = model_and_features
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert not ModelStore(blocker / "sub", KEY).save("AAPL", model)


def test_deserialize_rejects_symbol_mismatch(
    model_and_features: tuple[TrainedModel, np.ndarray],
) -> None:
    model, _ = model_and_features
    payload = serialize_model(model, "AAPL")
    with pytest.raises(ModelError):
        deserialize_model(payload, "MSFT")  # symbol mismatch inside metadata
