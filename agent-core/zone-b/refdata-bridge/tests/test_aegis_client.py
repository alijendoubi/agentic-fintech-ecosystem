"""``aegis_client.py`` against a REAL loopback mTLS gRPC server (fake_aegis.py).

Proves the three fail-closed behaviours required of the bridge:
* a valid snapshot+regime batch is pushed and applied;
* an item Aegis rejects is reported in ``PushResult.rejected``, not silently
  treated as applied (the caller must not advance that item's watermark);
* an unreachable Aegis raises ``PushError`` (retried/logged by the service
  layer, not a crash) instead of hanging or silently doing nothing.
"""

from __future__ import annotations

from pathlib import Path

import aegis_pb2
import aegis_pb2_grpc
import grpc
import pytest
from fake_aegis import FakeAegisServicer, generate_mtls_material, start_fake_aegis_server

from refdata_bridge.aegis_client import AegisRefdataClient, PushError
from refdata_bridge.batch import PendingBatch
from refdata_bridge.mapping import ReferenceSnapshotData, RegimeLabelData

pytestmark = pytest.mark.asyncio


def _snap(symbol: str, as_of_ns: int = 1_700_000_000_000_000_000) -> ReferenceSnapshotData:
    return ReferenceSnapshotData(
        symbol=symbol,
        mid_price_nanos=150_000_000_000,
        adv_30d_nanos=1_000_000_000_000,
        as_of_ns=as_of_ns,
        is_stale=False,
    )


@pytest.fixture
def mtls_server():
    material = generate_mtls_material()
    servicer = FakeAegisServicer()
    server, port = start_fake_aegis_server(servicer, material)
    yield material, servicer, port
    server.stop(None)


def _client_channel(material, port: int) -> grpc.aio.Channel:
    creds = grpc.ssl_channel_credentials(
        root_certificates=material.ca_cert_pem,
        certificate_chain=material.client_cert_pem,
        private_key=material.client_key_pem,
    )
    return grpc.aio.secure_channel(f"localhost:{port}", creds)


async def test_valid_batch_is_pushed_and_applied(mtls_server) -> None:
    material, servicer, port = mtls_server
    channel = _client_channel(material, port)
    try:
        stub = aegis_pb2_grpc.AegisStub(channel)
        client = AegisRefdataClient(stub=stub, aegis_pb2=aegis_pb2, timeout_s=5.0)
        batch = PendingBatch(
            snapshots=[_snap("AAPL"), _snap("MSFT")],
            regime=RegimeLabelData(
                label="TRENDING_BULL", confidence=0.9, timestamp_ns=1_700_000_000_000_000_000
            ),
        )
        result = await client.push(batch)
        assert sorted(result.applied_snapshot_symbols) == ["AAPL", "MSFT"]
        assert result.regime_applied is True
        assert result.rejected == []
        assert servicer.calls == 1
        assert {s.symbol for s in servicer.applied} == {"AAPL", "MSFT"}
    finally:
        await channel.close()


async def test_rejected_item_is_reported_not_treated_as_applied(mtls_server) -> None:
    material, servicer, port = mtls_server
    servicer.reject_symbols = {"BADSYM"}
    channel = _client_channel(material, port)
    try:
        stub = aegis_pb2_grpc.AegisStub(channel)
        client = AegisRefdataClient(stub=stub, aegis_pb2=aegis_pb2, timeout_s=5.0)
        batch = PendingBatch(snapshots=[_snap("AAPL"), _snap("BADSYM")], regime=None)
        result = await client.push(batch)
        assert result.applied_snapshot_symbols == ["AAPL"]
        assert len(result.rejected) == 1
        assert result.rejected[0].key == "BADSYM"
        assert result.rejected[0].reason == "unknown_symbol"
    finally:
        await channel.close()


async def test_rejected_regime_is_reported(mtls_server) -> None:
    material, servicer, port = mtls_server
    servicer.fail_regime = True
    channel = _client_channel(material, port)
    try:
        stub = aegis_pb2_grpc.AegisStub(channel)
        client = AegisRefdataClient(stub=stub, aegis_pb2=aegis_pb2, timeout_s=5.0)
        batch = PendingBatch(
            snapshots=[],
            regime=RegimeLabelData(
                label="CRISIS", confidence=0.5, timestamp_ns=1_700_000_000_000_000_000
            ),
        )
        result = await client.push(batch)
        assert result.regime_applied is False
        assert result.rejected == [type(result.rejected[0])(key="regime", reason="stale")]
    finally:
        await channel.close()


async def test_unreachable_aegis_raises_push_error_not_hang_or_silent_drop() -> None:
    # Nothing is listening on this port: the RPC must fail fast and loudly.
    channel = grpc.aio.insecure_channel("127.0.0.1:1")
    try:
        stub = aegis_pb2_grpc.AegisStub(channel)
        client = AegisRefdataClient(stub=stub, aegis_pb2=aegis_pb2, timeout_s=1.0)
        batch = PendingBatch(snapshots=[_snap("AAPL")], regime=None)
        with pytest.raises(PushError):
            await client.push(batch)
    finally:
        await channel.close()


async def test_mtls_without_client_certificate_is_refused() -> None:
    """A caller that cannot present the market-data-writer client cert must fail,
    proving the server-side mTLS requirement (require_client_auth=True) is real,
    not merely configured and unused by the test harness."""
    material = generate_mtls_material()
    servicer = FakeAegisServicer()
    server, port = start_fake_aegis_server(servicer, material)
    try:
        creds = grpc.ssl_channel_credentials(root_certificates=material.ca_cert_pem)
        channel = grpc.aio.secure_channel(f"localhost:{port}", creds)
        try:
            stub = aegis_pb2_grpc.AegisStub(channel)
            client = AegisRefdataClient(stub=stub, aegis_pb2=aegis_pb2, timeout_s=3.0)
            with pytest.raises(PushError):
                await client.push(PendingBatch(snapshots=[_snap("AAPL")], regime=None))
        finally:
            await channel.close()
    finally:
        server.stop(None)


async def test_empty_batch_is_never_sent_over_the_wire() -> None:
    """A batch with nothing new must not generate a wire call at all."""
    calls = []

    class ExplodingStub:
        async def PushReferenceData(self, *_a, **_kw):  # noqa: N802
            calls.append(1)
            raise AssertionError("must not be called for an empty batch")

    client = AegisRefdataClient(stub=ExplodingStub(), aegis_pb2=aegis_pb2, timeout_s=1.0)
    result = await client.push(PendingBatch(snapshots=[], regime=None))
    assert calls == []
    assert result.applied_snapshot_symbols == []


def test_default_generated_dir_is_the_repo_layout() -> None:
    import refdata_bridge.aegis_client as mod

    expected = Path(mod.__file__).resolve().parents[3] / "shared" / "generated"
    assert mod.default_generated_dir() == expected


def test_container_layout_without_afe_proto_dir_fails_with_a_clear_error() -> None:
    """ALI-167: /app/refdata_bridge/aegis_client.py has no fourth parent. The module must
    still import (the old module-level constant raised IndexError at import time), and
    only a call that needs the default fails, with an actionable message."""
    import refdata_bridge.aegis_client as mod

    with pytest.raises(ImportError, match="AFE_PROTO_DIR"):
        mod.default_generated_dir("/app/refdata_bridge/aegis_client.py")
