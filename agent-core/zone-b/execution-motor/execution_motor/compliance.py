"""Compliance gates on the order path (ALI-161): per-trade Compliance Manifest + SHARP promotion.

Both run inside the motor, which already sits on the audit network (``zone-bc-audit``) and sees
every order immediately before the broker; Zone A (cognitive-core) never touches the audit store.

* **SHARP gate** (``StrategyGate``): the strategy Aegis signed (``Attestation.strategy_id``,
  which the caller's ``TradeSignal.strategy_id`` must equal) must map (owner-supplied file) to
  a SHARP proposal whose audited state is ``PROMOTED`` (``CANARY`` too, only with an explicit
  opt-in). Checked before any idempotency claim, so a refused strategy never burns the signal.
* **Manifest** (``ManifestWriter``): after every motor check passed and immediately before the
  broker, one manifest is built from the Aegis decision plus the caller's ``ExecutionContext``
  and stored write-once (``afe_manifest``, which also writes one ``manifest.stored`` audit
  record). Any failure refuses the order (``RejectReason.MANIFEST_FAILED``).

Trust boundary: the context is NOT signed. It is bound to the attestation by
``signal_id``/``symbol``/``strategy_id`` (``bind_context``). ``strategy_id`` is signed by
Aegis (``afe-attest-v2``, ``Attestation.strategy_id``), so a context naming another strategy
is refused (``CONTEXT_MISMATCH``) and the SHARP gate checks the attested value. The gate
runs before the motor verifies the signature, but that is safe: a forged
``Attestation.strategy_id`` changes the rebuilt text, so the motor refuses the order
(``ATTESTATION_INVALID``) before the broker. Everything else in the context (texts, model
versions, snapshot) is trusted because the caller authenticated over mTLS.

The Zone C libraries (``afe_manifest``, ``afe_sharp``, ``afe_audit``) are imported only by the
factories at the bottom, so the motor and its unit tests do not depend on them.
"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Protocol

import structlog

from .errors import ConfigError
from .models import RejectReason

_log = structlog.get_logger("execution_motor.compliance")
_DETAIL_CHARS: Final = 200
_TRUE: Final = frozenset({"1", "true", "yes"})


class StrategyGate(Protocol):
    def check(self, strategy_id: str) -> str | None:
        """None if the strategy may trade, else a short reason. Must not raise."""
        ...


class ManifestWriter(Protocol):
    def write(self, decision: Any, context: Any) -> str:
        """Build and durably store the manifest; return its digest. Raise on any failure."""
        ...


@dataclass(frozen=True)
class ContextRefusal:
    reason: RejectReason
    detail: str


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_DETAIL_CHARS]


def bind_context(decision: Any, context: Any) -> ContextRefusal | None:
    """The context must describe exactly the signed order: same signal_id, symbol and
    strategy_id (the attested one, signed since afe-attest-v2)."""
    order = decision.order
    signal = context.signal
    if not (context.HasField("signal") and context.HasField("snapshot")):
        return ContextRefusal(RejectReason.CONTEXT_MISSING, "context lacks signal or snapshot")
    if not signal.signal_id or not (signal.signal_id == decision.signal_id == order.signal_id):
        return ContextRefusal(RejectReason.CONTEXT_MISMATCH, "signal_id differs from decision")
    if not (signal.symbol == order.symbol == context.snapshot.symbol):
        return ContextRefusal(RejectReason.CONTEXT_MISMATCH, "symbol differs from decision")
    if not signal.strategy_id.strip():
        return ContextRefusal(RejectReason.CONTEXT_MISMATCH, "signal has no strategy_id")
    if signal.strategy_id != decision.attestation.strategy_id:
        return ContextRefusal(
            RejectReason.CONTEXT_MISMATCH, "strategy_id differs from the attested strategy_id"
        )
    return None


@dataclass(frozen=True)
class ComplianceGates:
    """What the motor enforces. ``None`` for a gate means it is disabled (never in production)."""

    manifest: ManifestWriter | None
    strategy_gate: StrategyGate | None

    @property
    def requires_context(self) -> bool:
        return self.manifest is not None or self.strategy_gate is not None

    def check_strategy(self, attested_strategy_id: str) -> str | None:
        """SHARP gate over the ATTESTED strategy id (``AttestedOrder.attestation``)."""
        if self.strategy_gate is None:
            return None
        return self.strategy_gate.check(attested_strategy_id)

    def release_check(self, decision: Any, context: Any) -> Callable[[], str | None] | None:
        writer = self.manifest
        if writer is None:
            return None

        def _check() -> str | None:
            try:
                digest = writer.write(decision, context)
            except Exception as exc:  # noqa: BLE001 - any failure refuses the order (fail closed)
                _log.error("manifest_failed", signal_id=decision.signal_id, error=_short(exc))
                return f"manifest not stored: {_short(exc)}"
            _log.info("manifest_stored", signal_id=decision.signal_id, digest=digest)
            return None

        return _check


DISABLED: Final = ComplianceGates(manifest=None, strategy_gate=None)


# --------------------------------------------------------------------------- Aegis -> manifest


def _decimal_to_float(text: str) -> float:
    """ControlResult threshold/observed are decimal strings; the manifest proto has doubles.
    The exact strings are kept in ``reason``; unparsable or non-finite values become 0.0."""
    try:
        value = float(Decimal(text))
    except (InvalidOperation, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def ptc_checks_from_decision(decision: Any, cm_pb2: Any, aegis_pb2: Any) -> list[Any]:
    """Map every Aegis ``ControlResult`` to a ``PTCCheckResult`` (one per evaluated control)."""
    names = aegis_pb2.ReasonCode
    checks = []
    for result in decision.results:
        reason = (
            names.Name(result.reason) if result.reason in names.values() else str(result.reason)
        )
        text = f"{reason}; threshold={result.threshold!r}; observed={result.observed!r}"
        if result.detail:
            text += f"; {result.detail}"
        checks.append(
            cm_pb2.PTCCheckResult(
                check_name=result.control_id,
                ptc_type=cm_pb2.HARD_BLOCK if result.is_hard else cm_pb2.SOFT_BLOCK,
                passed=result.passed,
                reason=text,
                threshold_value=_decimal_to_float(result.threshold),
                actual_value=_decimal_to_float(result.observed),
            )
        )
    return checks


class AfeManifestWriter:
    """``ManifestWriter`` over ``afe_manifest`` (build + FilesystemManifestStore). Serialised: the
    store's hash chain needs ``latest_digest`` and ``put`` to be one step."""

    def __init__(
        self,
        store: Any,
        *,
        build: Callable[..., Any],
        inputs_cls: Any,
        cognitive_cls: Any,
        cm_pb2: Any,
        aegis_pb2: Any,
    ) -> None:
        self._store = store
        self._build = build
        self._inputs = inputs_cls
        self._cognitive = cognitive_cls
        self._cm = cm_pb2
        self._aegis = aegis_pb2
        self._lock = threading.Lock()

    def write(self, decision: Any, context: Any) -> str:
        versions = dict(context.model_versions)
        # Aegis identifies itself and its limits file in every decision: record both.
        if decision.aegis_version:
            versions.setdefault("aegis", decision.aegis_version)
        if decision.attestation.limits_config_sha256:
            versions.setdefault("aegis-limits-sha256", decision.attestation.limits_config_sha256)
        cognitive = self._cognitive(
            blue_node_thesis=context.blue_node_thesis,
            red_node_challenge=context.red_node_challenge,
            judge_synthesis=context.judge_synthesis,
            compression_summaries=tuple(context.compression_summaries),
        )
        hitl = context.hitl_override if context.HasField("hitl_override") else None
        with self._lock:
            inputs = self._inputs(
                snapshot=context.snapshot,
                signal=context.signal,
                order=decision.order,
                model_versions=versions,
                ptc_checks=ptc_checks_from_decision(decision, self._cm, self._aegis),
                prev_manifest_hash=self._store.latest_digest(),
                hitl_override=hitl,
                cognitive=cognitive,
            )
            built = self._build(inputs)
            self._store.put(built)
        return str(built.digest)


# ------------------------------------------------------------------------------- SHARP gate


class SharpStrategyGate:
    """``StrategyGate`` over ``afe_sharp``: read-only (``store.load`` + ``fold``, which re-verifies
    every transition against the audit log). Unmapped strategy, unknown proposal, unverifiable
    history or any state other than PROMOTED (CANARY with ``allow_canary``) refuses."""

    def __init__(
        self,
        strategies: Mapping[str, str],
        *,
        store: Any,
        audit_lookup: Any,
        fold: Callable[[Any, Any], Any],
        allowed_states: frozenset[str],
    ) -> None:
        self._strategies = dict(strategies)
        self._store = store
        self._lookup = audit_lookup
        self._fold = fold
        self._allowed = allowed_states

    def check(self, strategy_id: str) -> str | None:
        proposal_id = self._strategies.get(strategy_id)
        if proposal_id is None:
            return f"strategy {strategy_id!r} has no SHARP proposal mapping"
        try:
            history = self._store.load(proposal_id)
            if not history:
                return f"SHARP proposal {proposal_id!r} does not exist"
            state = str(self._fold(history, self._lookup).state)
        except Exception as exc:  # noqa: BLE001 - an unverifiable history refuses (fail closed)
            return f"SHARP state of {proposal_id!r} unverifiable: {_short(exc)}"
        if state not in self._allowed:
            return f"SHARP proposal {proposal_id!r} is {state}, not {sorted(self._allowed)}"
        return None


def load_strategy_map(path: Path) -> dict[str, str]:
    """Owner-supplied JSON object ``{"<strategy_id>": "<SHARP proposal_id>"}``."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read SHARP strategy map {path}: {_short(exc)}") from exc
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("SHARP strategy map must be a non-empty JSON object")
    if not all(
        isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
        for k, v in raw.items()
    ):
        raise ConfigError("SHARP strategy map keys and values must be non-empty strings")
    return {str(k): str(v) for k, v in raw.items()}


# ----------------------------------------------------------------------------------- config


@dataclass(frozen=True)
class ComplianceConfig:
    manifest_dir: Path | None
    strategy_map_file: Path | None
    allow_canary: bool
    audit_actor: str

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, production: bool) -> ComplianceConfig:
        """Both gates are on unless explicitly disabled; disabling is refused in production."""
        manifest_off = env.get("MOTOR_MANIFEST_DISABLED", "").strip().lower() in _TRUE
        sharp_off = env.get("MOTOR_SHARP_DISABLED", "").strip().lower() in _TRUE
        if production and (manifest_off or sharp_off):
            raise ConfigError(
                "MOTOR_MANIFEST_DISABLED / MOTOR_SHARP_DISABLED are refused in production"
            )
        manifest_dir = None if manifest_off else _required_path(env, "MOTOR_MANIFEST_DIR")
        strategy_map = None if sharp_off else _required_path(env, "MOTOR_SHARP_STRATEGY_MAP")
        actor = env.get("MOTOR_AUDIT_ACTOR", "execution-motor").strip() or "execution-motor"
        allow_canary = env.get("MOTOR_SHARP_ALLOW_CANARY", "").strip().lower() in _TRUE
        return cls(manifest_dir, strategy_map, allow_canary, actor)

    @property
    def needs_audit_db(self) -> bool:
        return self.manifest_dir is not None or self.strategy_map_file is not None


def _required_path(env: Mapping[str, str], name: str) -> Path:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required (or disable the gate outside production)")
    return Path(value)


def build_compliance(cfg: ComplianceConfig, env: Mapping[str, str]) -> ComplianceGates:
    """Wire the real Zone C libraries. Imports happen here only (see module docstring)."""
    if not cfg.needs_audit_db:
        _log.warning("compliance_disabled", note="no manifest, no SHARP gate: development only")
        return DISABLED
    import importlib

    from afe_audit import AuditLogger  # type: ignore[import-not-found]
    from afe_audit.db import DsnConnectionSource  # type: ignore[import-not-found]

    source = DsnConnectionSource.from_env(env)  # POSTGRES_HOST/PORT/DB/USER/PASSWORD
    manifest: ManifestWriter | None = None
    if cfg.manifest_dir is not None:
        import afe_manifest  # type: ignore[import-not-found]

        store = afe_manifest.FilesystemManifestStore(
            cfg.manifest_dir, AuditLogger(source), actor=cfg.audit_actor
        )
        manifest = AfeManifestWriter(
            store,
            build=afe_manifest.build_manifest,
            inputs_cls=afe_manifest.ManifestInputs,
            cognitive_cls=afe_manifest.CognitiveOutputs,
            cm_pb2=importlib.import_module("compliance_manifest_pb2"),
            aegis_pb2=importlib.import_module("aegis_pb2"),
        )
    else:
        _log.warning("manifest_disabled", note="MOTOR_MANIFEST_DISABLED: development only")
    gate: StrategyGate | None = None
    if cfg.strategy_map_file is not None:
        from afe_sharp.models import fold  # type: ignore[import-not-found]
        from afe_sharp.pg_audit import PostgresAuditLookup  # type: ignore[import-not-found]
        from afe_sharp.pg_store import PostgresProposalStore  # type: ignore[import-not-found]

        allowed = frozenset({"PROMOTED", "CANARY"} if cfg.allow_canary else {"PROMOTED"})
        gate = SharpStrategyGate(
            load_strategy_map(cfg.strategy_map_file),
            store=PostgresProposalStore(source),
            audit_lookup=PostgresAuditLookup(source),
            fold=fold,
            allowed_states=allowed,
        )
    else:
        _log.warning("sharp_gate_disabled", note="MOTOR_SHARP_DISABLED: development only")
    return ComplianceGates(manifest=manifest, strategy_gate=gate)
