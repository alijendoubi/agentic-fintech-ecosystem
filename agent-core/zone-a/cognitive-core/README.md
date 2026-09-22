# cognitive-core

Zone A's LangGraph Blue/Red/Judge/Compression debate, plus the runner service that turns a
market snapshot into a `TradeSignal`, submits it to Aegis, and (this package's PKG-A3 addition)
relays an approved `AegisDecision` on to execution-motor. See
`agent-core/docs/specs/phase_3_aegis_execution.md` section 5 for the target data flow:

```
cognitive-core --SubmitSignal(TradeSignal)--> AEGIS --AegisDecision(+attestation)--> execution-motor
```

Implemented and unit-tested in isolation (`python -m pytest`); not wired end to end with a
real Aegis or execution-motor — see "Known limitations" below and the top-level
`agent-core/README.md` "Current status" table.

## Running the tests

```bash
cd agent-core/zone-a/cognitive-core
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest --cov=. --cov-report=term-missing
ruff check --config ../../../ruff.toml .
```

## Configuration

Full detail (parsing, defaults, validation) lives in the module docstrings of `config.py`
(debate settings) and `runner_config.py` (runner/sink settings) — this is a summary. Every
invalid value raises `ConfigError` at startup: nothing here runs with a silently-guessed
default for a safety-relevant setting.

### Aegis sink (existing)

| Variable | Default | Notes |
|---|---|---|
| `ZONE_B_GRPC_HOST` / `_PORT` | `aegis` / `50051` | Aegis `SubmitSignal` target |
| `COGNITIVE_AEGIS_TIMEOUT_S` | `1.0` | per-call deadline |
| `AEGIS_CLIENT_TLS_CA` / `_CERT` / `_KEY` | unset (insecure, dev only) | PEM file paths; `_CERT`/`_KEY` must be given together. `ENVIRONMENT=production` refuses an insecure channel unless `AEGIS_CLIENT_TLS_CA` is set |

### execution-motor relay (new, PKG-A3)

| Variable | Default | Notes |
|---|---|---|
| `MOTOR_TARGET` | `execution-motor:50052` | `host:port` for the execution-motor `Execute` RPC. The port is a **placeholder** — execution-motor has no Dockerfile/service/entrypoint yet, so this has never been verified against a real deployment (TODO(owner)) |
| `COGNITIVE_MOTOR_TIMEOUT_S` | `1.0` | per-call deadline for `Execute` |
| `MOTOR_CLIENT_TLS_CA` / `_CERT` / `_KEY` | unset (insecure, dev only) | Same shape and rules as the Aegis TLS variables above, applied to the execution-motor channel: `_CERT`/`_KEY` must be given together, and `ENVIRONMENT=production` refuses an insecure channel unless `MOTOR_CLIENT_TLS_CA` is set |

Relay behavior (`sinks.AegisRelaySink`):

- The runner still submits every signal to Aegis exactly as before; the returned
  `SinkReceipt` always reflects the Aegis outcome (`DECISION_APPROVED` /
  `DECISION_HELD_FOR_HUMAN` / `DECISION_REJECTED`).
- Only a `DECISION_APPROVED` decision that actually carries an `Attestation` is forwarded to
  execution-motor, via `MotorSink.send(decision)` with the **exact** `AegisDecision` object
  Aegis returned (no re-derivation). A held or rejected decision — or an approved one with no
  attestation set — is logged (`motor_relay_skipped`) and stops there: Aegis is the only
  authority that may let a signal reach execution.
- A failure relaying an approved decision to execution-motor is **not** swallowed: it is
  logged as `motor_relay_failed` and re-raised as `SinkError`, which the runner already
  treats like any other undelivered signal (`signal_send_failed` log line,
  `CycleOutcome.SINK_FAILED`, counted in `/health`). A relayed-but-failed signal is never
  silently dropped without a trace.

### Position sizer (`COGNITIVE_ORDER_QUANTITY`)

The cognitive core has no portfolio or risk state, so it never invents a real order quantity.
**Building a real risk-based position sizer is explicitly out of scope for this package** —
it is a strategy/risk decision, not something an LLM debate should decide unilaterally.

- Outside production (`ENVIRONMENT` unset or anything other than `production`),
  `COGNITIVE_ORDER_QUANTITY` defaults to `0`, which makes every debate abstain
  (`SIGNAL_ABSTAIN`, never force-sized) — the existing fail-safe behavior. Test fixtures
  (`tests/runner_fakes.py::runner_settings`) set a **TEST-ONLY fixed quantity of 10 shares**
  so tests can exercise the actionable path; this is a fixture constant, not a sizing policy,
  and must never be read as one.
- In production (`ENVIRONMENT=production`), `COGNITIVE_ORDER_QUANTITY` is **required**: unset
  or a non-positive value raises `ConfigError` at `RunnerSettings.from_env()`, so the process
  refuses to start rather than silently trading nothing (or, if the default were ever changed,
  silently trading an arbitrary fixed size).
- Whatever quantity is configured, `signals.abstain_reasons` still abstains any signal whose
  resolved quantity is `<= 0` or non-finite at build time (`tests/test_signals.py::
  test_non_positive_quantity_abstains`), so a bad value anywhere in the pipeline degrades to
  an abstain instead of an order.

## Known limitations

- **`execution_motor.proto` does not exist in this worktree.** It is being built on branch
  `pkg-x3/motor-service` (PKG-X3), alongside execution-motor's `Execute(AegisDecision) returns
  (ExecuteAck)` gRPC server. Until it lands and `shared/proto/generate.sh` produces
  `execution_motor_pb2_grpc.py`:
  - `sinks.MotorStub` is a hand-written local `Protocol` standing in for the generated stub
    (documented in its docstring); `sinks.MotorGrpcSink` itself needs no change once the real
    stub exists — it only calls `.Execute(decision, timeout=...)`.
  - `__main__.build_sink` tries `sinks.load_generated_motor_protos()`; on `ImportError` it
    logs `motor_relay_unavailable` and falls back to a plain `AegisGrpcSink` (Aegis is still
    submitted to normally; nothing is forwarded to execution-motor) rather than blocking
    startup. Once the generated stubs are importable, `build_sink` automatically upgrades to
    `AegisRelaySink` with no further code change (see
    `tests/test_health_main.py::test_build_sink_wires_the_relay_once_motor_protos_are_available`
    for a simulation of that upgrade).
  - The request type needs no stand-in: `AegisDecision` is already generated from
    `shared/proto/aegis.proto` and is forwarded to execution-motor unmodified.
- Nothing in this package talks to a real Aegis or execution-motor process; both legs are
  exercised only against fakes/mocks in the test suite (`tests/test_sinks.py`,
  `tests/test_aegis_tls.py`, `tests/test_health_main.py`).
- Bedrock model IDs in `config.py` are placeholders (see its module docstring); verify against
  the Bedrock console before any live use.
