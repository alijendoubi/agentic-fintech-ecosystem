# Broker Gateway: the only holder of broker credentials (Zone B, ADR-004 Option C)

`execution-motor` holds no broker credentials. Every broker call it makes goes to this small,
single-purpose gRPC service, which adds the Alpaca credentials and forwards the call, but only
after these checks:

| Operation (RPC) | Broker call (fixed) | Needs an Aegis attestation |
|---|---|---|
| `SubmitOrder` | `POST /v2/orders` | **yes**: see below |
| `CancelOrder` | `DELETE /v2/orders/{id}` (id `[A-Za-z0-9-]{1,64}`) | no (risk-reducing: kill-switch sweep, spec §5.2) |
| `GetOrderByClientOrderId` | `GET /v2/orders:by_client_order_id` | no (read-only) |
| `ListOpenOrders` | `GET /v2/orders?status=open&limit=500&direction=asc` | no (read-only) |
| `GetPositions` | `GET /v2/positions` | no (read-only) |
| `GetAccount` | `GET /v2/account` | no (read-only) |

Nothing else exists: there is no generic "forward this URL" operation, and a method outside
`BrokerGatewayService` is `UNIMPLEMENTED`. Contract: `agent-core/shared/proto/broker_gateway.proto`.

Status: implemented and tested with unit tests, a real loopback mTLS gRPC server, the
cross-package e2e (`agent-core/tests/e2e`) and a container smoke run (see "What was verified").
**Never run against the real Alpaca API.**

## What a submit must pass (`broker_gateway/authorize.py`)

In this order; the first failure refuses the request and **nothing is sent**:

1. `canonical_text` is `afe-attest-v2` text (Phase 3 spec §7) that re-serialises byte for byte
   (strict parser, then the same `build_canonical_text` execution-motor uses). Any other version,
   `afe-attest-v1` included, is `unsupported_version`.
2. The proof's `key_id` equals the text's `key_id` line, and the signature verifies under that key
   with Aegis's **public** key from this gateway's own registry (`GATEWAY_ATTESTATION_KEYS_FILE`,
   same JSON format as the motor's). Dev (Ed25519) keys are refused when `GATEWAY_ENV` is production.
3. Timing on the gateway's clock: `now < expires_at_ns`; `decided_at_ns <= now + skew`;
   `expires_at_ns - decided_at_ns <= GATEWAY_MAX_ATTESTATION_TTL_MS` (default 5000, the spec's
   PROPOSED 5 s); and `decided_at_ns >= process start + skew` (see "Replay store").
4. Every field of the outgoing order equals the attested value: `client_order_id` = signed
   `signal_id`, `symbol`, `side` (BUY -> `buy`, SELL and SELL_SHORT -> `sell`), `order_type`,
   `qty_nanos`, `limit_price_nanos`, `stop_price_nanos`. `time_in_force` must be `day`.
5. Single use: the signed `signal_id` has not been forwarded before.

The broker request body is then built from the **attested** fields (int nanos -> exact decimal
strings, no float). Refusal codes are in `authorize.Refusal`.

### Replay store: in memory

A dict under a lock, pruned once an entry can no longer pass the expiry check. It is **not**
persisted and not shared (no Redis: the motor has none, and the gateway should not depend on
another network service). A restart empties it, so the gateway refuses every attestation
decided before `process start + GATEWAY_MAX_CLOCK_SKEW_MS`: anything a previous process could
have forwarded was decided before that instant (it accepted nothing decided more than one skew
ahead of its own clock). Cost: attestations in flight across a restart are refused (fail
closed). Residual risk: a backwards wall-clock step larger than the skew allowance could let a
pruned, expired entry look valid again; the broker's own duplicate `client_order_id` refusal
(ASSUMED: Alpaca answers 422) is the next layer. Only one gateway replica may run (a second
replica would have its own store). TODO(owner): if the gateway is ever replicated, move the
store to a shared, durable backend.

## Outcomes

Every evaluated request returns a `BrokerReply`: `refusal` (nothing sent), `transport_error`
(the broker gave no HTTP answer: for a submit the outcome is unknown and the motor reconciles),
or the broker's `http_status` + `body`, passed through **unparsed** (all Alpaca parsing,
reconciliation and read retries stay in `execution_motor/alpaca.py`). The gateway itself never
retries. A client whose certificate CN is not in `GATEWAY_ALLOWED_CLIENT_CNS` gets
`PERMISSION_DENIED` before any work.

## Configuration (all from env; `broker_gateway/config.py`)

| Variable | Required | Meaning |
|---|---|---|
| `GATEWAY_ENV` | no | `production` (default when unset!), `staging`, `development`, `test`. |
| `GATEWAY_LISTEN_ADDR` | no | Default `0.0.0.0:50071`. |
| `GATEWAY_TLS_CERT`, `GATEWAY_TLS_KEY`, `GATEWAY_TLS_CLIENT_CA` | yes | Server cert/key and the CA client certs must chain to. There is **no** plaintext mode. |
| `GATEWAY_ALLOWED_CLIENT_CNS` | yes | Comma-separated client certificate CNs (normally only `execution-motor`). |
| `GATEWAY_ATTESTATION_KEYS_FILE` | yes | Aegis's public verification keys, JSON array like the motor's `MOTOR_ATTESTATION_KEYS_FILE`. During an attestation-key rotation list both keys (key-rotation runbook, procedure A). |
| `GATEWAY_MAX_ATTESTATION_TTL_MS` | no | Default 5000 (spec §7 PROPOSED lifetime). |
| `GATEWAY_MAX_CLOCK_SKEW_MS` | no | Default 1000 (same as the motor's `MOTOR_MAX_CLOCK_SKEW_MS`). |
| `GATEWAY_BROKER_TIMEOUT_MS` | no | Default 5000 (what the motor's Alpaca client used). The motor's deadline for a gateway call is 7 s. |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | yes | Alpaca **paper** credentials. Template-looking values are refused. Never logged, never in `repr`. |
| `ALPACA_BASE_URL` | no | Paper by default. The live host needs `AFE_ENABLE_LIVE_TRADING=true` **and** `AFE_LIVE_TRADING_CONFIRM=I-UNDERSTAND-THIS-TRADES-REAL-MONEY`; compose passes neither. Any other host is refused. |

HTTP client: redirects are never followed and proxy variables are **ignored** (`trust_env=False`),
so the credentials only ever go to the pinned host. TODO(owner): if production egress must use a
proxy, add an explicit setting. Execution-motor also checks that every reply names the endpoint
it expects (paper), so a gateway pointed at live cannot be used by a paper-configured motor.

## Shared code with execution-motor (no copy)

The gateway imports `execution_motor.canonical`, `.verifiers`, `.keys` and `.errors` (pure
attestation code: canonical text, Ed25519/ECDSA-P256 verifier, key registry). `conftest.py` and
`mypy.ini` put `../execution-motor` on the path; the Dockerfile copies exactly those five files
(the same "copy a sibling library" pattern the motor uses for `afe_audit`/`afe_manifest`).
`tests/test_shared_code.py` fails if the gateway starts importing anything else from the motor,
if the Dockerfile list drifts, or if a shared module grows a network or broker dependency.

## Build and test

```bash
cd agent-core/zone-b/broker-gateway
pip install -r requirements-dev.txt
ruff check --config ../../../ruff.toml . && ruff format --check --config ../../../ruff.toml .
python -m mypy .
python -m pytest -q
```

Docker (build context is `agent-core/`):

```bash
docker build -f agent-core/zone-b/broker-gateway/Dockerfile -t afe/broker-gateway:dev agent-core
```

Runtime image: `python:3.12.7-slim-bookworm`, non-root uid/gid 10002, TCP healthcheck, runs with
compose's `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`.

## What was verified (2026-09-29)

* Unit tests: every refusal above, including a real Rust-Aegis-signed fixture (buy, sell,
  sell_short) accepted, every field mismatch, expiry edges, v1, wrong/unknown key id, bad
  signature, replay, peer allow-list, allow-listed endpoints only, credentials absent from logs,
  replies and reprs; a real gRPC server over loopback mTLS (allowed CN, other CN, no cert, foreign
  CA, unknown method). The broker is an `httpx.MockTransport` fake: no network.
* `agent-core/tests/e2e`: execution-motor -> this gateway (in process) -> fake Alpaca, with the
  real Aegis fixture; a compromised-motor test calls the gateway directly with replayed,
  altered and missing attestations.
* Container: the image built with a local CA workaround (the plain build could not reach PyPI
  through this machine's TLS-intercepting proxy), started with the compose hardening flags and
  dev-tls material, became healthy, and refused/forwarded as expected for calls from the motor
  image over real mTLS (no egress, so forwards ended in `transport_error`). It refused to start
  without credentials and with a dev key in production.

Not verified: the real Alpaca API (response shapes are the motor's ASSUMED shapes), latency of
the extra hop (unmeasured; not in Aegis's 50 ms budget), behaviour under load.

## Open items

* TODO(owner): verify with Alpaca whether the paper/live API key can be restricted (IP allow-list,
  permissions) as defence in depth for the credential this process holds (ADR-004 residual risk).
* TODO(owner): production secret management for `ALPACA_*` (today: compose env from `.env`).
* TODO(owner): monitoring/alerting on `gateway_submit_refused` and `gateway_peer_refused` events.
