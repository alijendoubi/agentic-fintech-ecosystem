# sensory-array

Zone B real-time ingestor (Polygon WebSocket L2 quotes/trades -> normalised
`MarketSnapshot` -> QuestDB ILP + Redis).

## Ordering and freshness guards

* Quotes are guarded per symbol by exchange timestamp. A quote older than the
  last accepted one is dropped (`Reject::OutOfOrder`) before any state change
  and counted in the `out_of_order_quotes` metric. Equal timestamps are accepted.
* Zone A regime labels are guarded the same way (`RegimeReject::OutOfOrder`,
  `regime_out_of_order` metric).
* Event-time staleness is still flagged by the TTL checker (`FEED_LAG_BREACH`,
  `FEED_MAX_LAG_MS`, default 1000 ms); the L2/trade TTLs use local receive time.

## Follow-up (not implemented): Aegis `PushReferenceData` sink

Aegis exposes the additive RPC `PushReferenceData`
(`agent-core/shared/proto/aegis.proto`). This crate has no `build.rs`, no
`tonic`/`prost` dependency and no generated stubs, so the client was not added.
TODO(owner): add proto codegen (`tonic-build`), a pluggable `AegisRefdataSink`
configured from `AEGIS_REFDATA_TARGET` plus mTLS paths, and tests against a fake
gRPC server.
