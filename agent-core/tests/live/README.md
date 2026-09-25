# Live-stack integration tests (ALI-166)

These tests run against a **running** dev compose stack, over its real network paths
and real mTLS. Nothing is faked: Aegis, execution-motor, refdata-bridge, Redis and the
HITL terminal are the real containers. The tests are skipped unless `AFE_LIVE_STACK=1`.

## Run

```bash
cd agent-core/infrastructure/dev-tls && sh generate-dev-certs.sh --yes-i-know-this-is-dev-only
cd .. && cp .env.example .env            # fill every blank with DEV values
mkdir -p secrets/aegis-config            # put a DEV limits.json there (owner-supplied; never commit)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build

cd ../tests/live
python -m venv .venv && . .venv/bin/activate   # Windows: .venv/Scripts/activate
pip install -r requirements.txt
AFE_LIVE_STACK=1 python -m pytest -v
```

The dev override publishes these endpoints on loopback:

| Variable | Default |
|---|---|
| `AFE_LIVE_AEGIS` | `127.0.0.1:50051` |
| `AFE_LIVE_MOTOR` | `127.0.0.1:50052` |
| `AFE_LIVE_REDIS` | `redis://127.0.0.1:6379` |
| `AFE_LIVE_HITL` | `http://127.0.0.1:3000` |

## What is covered

- Aegis refuses a client with no certificate over mTLS.
- Aegis denies an RPC outside the caller's identity role.
- The kill-switch state is readable over mTLS.
- A snapshot published to Redis reaches Aegis through refdata-bridge.
- A real signal gets a real Aegis decision, and every failed control carries a reason.
- execution-motor refuses an unapproved or tampered decision over its own mTLS listener.
- The HITL terminal answers its health endpoint.

## What is not covered

These need owner-supplied credentials:

- the real Polygon feed (ALI-165)
- real LLM calls (ALI-152 and ALI-154)
- a real broker

Outside the dev limits' trading session, Aegis rejects every signal, fail-closed, and
the tests accept that. An APPROVED decision is exercised through the tamper check.

## Clock

Timestamps come from Redis `TIME`, which is the Docker VM clock, not the host clock.
On Docker Desktop the two can drift by close to a second, which is most of Aegis's 1 s
freshness window for reference data.

## Redis credentials (ALI-20)

Redis refuses anonymous clients. The tests connect as the service that owns each channel:
`sensory-array` publishes snapshots, `regime-detector` publishes regime labels, and
`healthcheck` reads `TIME`. Passwords are read from the environment or from the dev stack's
`agent-core/infrastructure/.env` (`REDIS_SENSORY_PASSWORD`, `REDIS_REGIME_PASSWORD`,
`REDIS_HEALTHCHECK_PASSWORD`).
