"""
Regime Detector — Zone A
========================
Runs a 5-state Gaussian HMM on rolling market features from QuestDB.
Publishes regime labels to Redis channel `regime:labels` every second.

States: TRENDING_BULL | TRENDING_BEAR | HIGH_VOL_CHOP | LOW_VOL_CHOP | CRISIS
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import redis.asyncio as aioredis
import structlog
from dotenv import load_dotenv
from hmmlearn.hmm import GaussianHMM
from questdb.ingress import Sender  # noqa: F401 (used for type hints in future)

load_dotenv()

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────

QUESTDB_HOST = os.environ.get("QUESTDB_HOST", "localhost")
QUESTDB_PORT = int(os.environ.get("QUESTDB_PORT", "9000"))
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
MODEL_PATH = Path(os.environ.get("MODEL_PATH", "/app/models/hmm_5state.pkl"))
INFERENCE_INTERVAL_S = float(os.environ.get("INFERENCE_INTERVAL_S", "1.0"))
RETRAIN_INTERVAL_S = float(os.environ.get("RETRAIN_INTERVAL_S", str(4 * 3600)))
FEATURE_WINDOW = int(os.environ.get("FEATURE_WINDOW", "200"))
MIN_BARS_FOR_INFERENCE = int(os.environ.get("MIN_BARS_FOR_INFERENCE", "20"))
N_STATES = 5
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()

# HMM state index → regime label name (assigned after training via heuristics)
STATE_LABELS = [
    "TRENDING_BULL",
    "TRENDING_BEAR",
    "HIGH_VOL_CHOP",
    "LOW_VOL_CHOP",
    "CRISIS",
]

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, LOG_LEVEL, logging.INFO)
    ),
    logger_factory=structlog.PrintLoggerFactory(),
)
log = structlog.get_logger()


# ─────────────────────────────────────────────────────────────
# Feature extraction
# ─────────────────────────────────────────────────────────────

def extract_features(
    mid_prices: list[float],
    spreads: list[float],
    ofis: list[float],
) -> Optional[np.ndarray]:
    """
    Build feature matrix from rolling price/spread/OFI history.

    Features per bar:
      0: log_return       — 1-bar log return
      1: realized_vol     — 20-bar rolling annualized std of log returns
      2: spread_norm      — spread / mid_price (normalized)
      3: ofi              — order flow imbalance

    Returns shape (N, 4) or None if insufficient data.
    """
    n = len(mid_prices)
    if n < 2:
        return None

    prices = np.array(mid_prices, dtype=np.float64)
    spreads_arr = np.array(spreads, dtype=np.float64)
    ofis_arr = np.array(ofis, dtype=np.float64)

    log_returns = np.diff(np.log(prices + 1e-10))  # shape (n-1,)

    # Rolling 20-bar realized vol (annualized)
    window = 20
    rvols = np.zeros(len(log_returns))
    for i in range(len(log_returns)):
        start = max(0, i - window + 1)
        chunk = log_returns[start : i + 1]
        if len(chunk) > 1:
            rvols[i] = np.std(chunk, ddof=1) * np.sqrt(252 * 6.5 * 3600)

    # Align arrays: log_returns has length n-1, use spreads/ofis from index 1..n
    spreads_aligned = spreads_arr[1:]
    prices_aligned = prices[1:]
    ofis_aligned = ofis_arr[1:]

    spread_norm = np.where(
        prices_aligned > 0, spreads_aligned / prices_aligned, 0.0
    )

    X = np.column_stack([log_returns, rvols, spread_norm, ofis_aligned])

    # Replace inf/nan with 0
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    return X


# ─────────────────────────────────────────────────────────────
# State labeling heuristic (post-training)
# ─────────────────────────────────────────────────────────────

def assign_state_labels(model: GaussianHMM) -> dict[int, str]:
    """
    Assign regime names to HMM states using mean feature vectors.

    Heuristics based on (log_return, realized_vol):
      - Highest mean return     → TRENDING_BULL
      - Lowest mean return      → TRENDING_BEAR
      - Highest realized vol    → CRISIS
      - 2nd highest vol         → HIGH_VOL_CHOP
      - Lowest vol              → LOW_VOL_CHOP
    """
    means = model.means_  # shape (n_states, n_features)
    # mean log return (feature 0), realized vol (feature 1)
    returns = means[:, 0]
    vols = means[:, 1]

    state_labels: dict[int, str] = {}
    remaining = set(range(N_STATES))

    # CRISIS: highest vol
    crisis_idx = int(np.argmax(vols))
    state_labels[crisis_idx] = "CRISIS"
    remaining.discard(crisis_idx)

    # TRENDING_BULL: highest return among remaining
    remaining_list = list(remaining)
    bull_idx = remaining_list[int(np.argmax(returns[remaining_list]))]
    state_labels[bull_idx] = "TRENDING_BULL"
    remaining.discard(bull_idx)

    # TRENDING_BEAR: lowest return among remaining
    remaining_list = list(remaining)
    bear_idx = remaining_list[int(np.argmin(returns[remaining_list]))]
    state_labels[bear_idx] = "TRENDING_BEAR"
    remaining.discard(bear_idx)

    # HIGH_VOL_CHOP: highest vol among remaining
    remaining_list = list(remaining)
    hvchop_idx = remaining_list[int(np.argmax(vols[remaining_list]))]
    state_labels[hvchop_idx] = "HIGH_VOL_CHOP"
    remaining.discard(hvchop_idx)

    # LOW_VOL_CHOP: whatever is left
    state_labels[remaining.pop()] = "LOW_VOL_CHOP"

    return state_labels


# ─────────────────────────────────────────────────────────────
# HMM model manager
# ─────────────────────────────────────────────────────────────

class RegimeModel:
    def __init__(self):
        self.model: Optional[GaussianHMM] = None
        self.state_labels: dict[int, str] = {}
        self.last_trained: float = 0.0
        self._load_or_init()

    def _load_or_init(self):
        if MODEL_PATH.exists():
            try:
                saved = joblib.load(MODEL_PATH)
                self.model = saved["model"]
                self.state_labels = saved["state_labels"]
                self.last_trained = saved.get("trained_at", 0.0)
                log.info("Loaded HMM from disk", path=str(MODEL_PATH))
                return
            except Exception as e:
                log.warning("Failed to load model, will train from scratch", error=str(e))

        self.model = GaussianHMM(
            n_components=N_STATES,
            covariance_type="full",
            n_iter=200,
            tol=1e-4,
            random_state=42,
        )
        log.info("Initialized fresh HMM (untrained)")

    def is_trained(self) -> bool:
        return self.model is not None and hasattr(self.model, "means_")

    def train(self, X: np.ndarray) -> bool:
        """
        Fit the HMM on feature matrix X (shape N x 4).
        Returns True on success.
        """
        if len(X) < MIN_BARS_FOR_INFERENCE + 2:
            return False
        try:
            self.model.fit(X)
            self.state_labels = assign_state_labels(self.model)
            self.last_trained = time.time()
            self._persist()
            log.info(
                "HMM trained",
                n_bars=len(X),
                state_labels=self.state_labels,
            )
            return True
        except Exception as e:
            log.error("HMM training failed", error=str(e))
            return False

    def predict(self, X: np.ndarray) -> tuple[str, float]:
        """
        Predict regime for the latest observation.
        Returns (label, confidence) where confidence = max posterior probability
        smoothed over last 5 bars.
        """
        if not self.is_trained() or len(X) < 1:
            return "REGIME_UNKNOWN", 0.0

        try:
            # Use last min(5, len) bars for smoothing
            window = min(5, len(X))
            X_window = X[-window:]
            posteriors = self.model.predict_proba(X_window)  # shape (window, n_states)
            mean_posterior = posteriors.mean(axis=0)
            state_idx = int(np.argmax(mean_posterior))
            confidence = float(mean_posterior[state_idx])
            label = self.state_labels.get(state_idx, "REGIME_UNKNOWN")
            return label, confidence
        except Exception as e:
            log.error("HMM inference failed", error=str(e))
            return "REGIME_UNKNOWN", 0.0

    def _persist(self):
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "model": self.model,
                "state_labels": self.state_labels,
                "trained_at": self.last_trained,
            },
            MODEL_PATH,
        )
        log.info("Model persisted", path=str(MODEL_PATH))


# ─────────────────────────────────────────────────────────────
# QuestDB feature fetcher
# ─────────────────────────────────────────────────────────────

async def fetch_features_from_questdb(
    symbol: str,
    limit: int = FEATURE_WINDOW,
) -> Optional[tuple[list, list, list]]:
    """
    Query QuestDB REST API for the latest `limit` rows for `symbol`.
    Returns (mid_prices, spreads, ofis) or None on error.
    """
    import aiohttp

    url = (
        f"http://{QUESTDB_HOST}:{QUESTDB_PORT}/exec"
        f"?query=SELECT+mid_price,spread,order_flow_imbalance"
        f"+FROM+market_data"
        f"+WHERE+symbol%3D%27{symbol}%27"
        f"+ORDER+BY+ingestion_ts+DESC"
        f"+LIMIT+{limit}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2.0)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                rows = data.get("dataset", [])
                if not rows:
                    return None
                # rows are newest-first; reverse for chronological order
                rows = list(reversed(rows))
                mid_prices = [r[0] for r in rows if r[0] is not None]
                spreads = [r[1] for r in rows if r[1] is not None]
                ofis = [r[2] for r in rows if r[2] is not None]
                return mid_prices, spreads, ofis
    except Exception as e:
        log.debug("QuestDB fetch failed", symbol=symbol, error=str(e))
        return None


async def fetch_all_symbols_from_questdb(limit: int = FEATURE_WINDOW) -> list[str]:
    """Return list of distinct symbols in market_data table."""
    import aiohttp

    url = (
        f"http://{QUESTDB_HOST}:{QUESTDB_PORT}/exec"
        f"?query=SELECT+DISTINCT+symbol+FROM+market_data+LIMIT+100"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2.0)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return [row[0] for row in data.get("dataset", [])]
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────
# Main async loop
# ─────────────────────────────────────────────────────────────

async def main():
    log.info(
        "Regime Detector starting",
        questdb=f"{QUESTDB_HOST}:{QUESTDB_PORT}",
        redis=REDIS_URL,
        model_path=str(MODEL_PATH),
    )

    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    model = RegimeModel()

    last_retrain = model.last_trained
    inference_count = 0

    while True:
        loop_start = time.monotonic()

        # Discover active symbols from QuestDB
        symbols = await fetch_all_symbols_from_questdb()
        if not symbols:
            log.debug("No symbols in QuestDB yet, waiting...")
            await asyncio.sleep(INFERENCE_INTERVAL_S)
            continue

        # Retrain periodically (or on first run)
        now = time.time()
        should_retrain = (now - last_retrain) >= RETRAIN_INTERVAL_S

        for symbol in symbols:
            result = await fetch_features_from_questdb(symbol)
            if result is None:
                continue

            mid_prices, spreads, ofis = result

            if len(mid_prices) < MIN_BARS_FOR_INFERENCE:
                continue

            X = extract_features(mid_prices, spreads, ofis)
            if X is None or len(X) < MIN_BARS_FOR_INFERENCE:
                continue

            # Train / retrain
            if should_retrain or not model.is_trained():
                model.train(X)
                last_retrain = time.time()
                should_retrain = False

            if not model.is_trained():
                continue

            # Inference
            label, confidence = model.predict(X)
            ts_ns = int(time.time() * 1e9)

            payload = json.dumps(
                {
                    "symbol": symbol,
                    "label": label,
                    "confidence": round(confidence, 4),
                    "ts_ns": ts_ns,
                }
            )
            await redis_client.publish("regime:labels", payload)

            inference_count += 1
            log.debug(
                "Regime published",
                symbol=symbol,
                label=label,
                confidence=f"{confidence:.3f}",
            )

        elapsed = time.monotonic() - loop_start
        sleep_time = max(0.0, INFERENCE_INTERVAL_S - elapsed)
        await asyncio.sleep(sleep_time)

        if inference_count % 100 == 0 and inference_count > 0:
            log.info("Regime detector heartbeat", inferences=inference_count)


if __name__ == "__main__":
    asyncio.run(main())


# ─────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────

def test_hmm_5_states():
    model = RegimeModel()
    assert model.model.n_components == 5


def test_feature_shape():
    mid_prices = [100.0 + i * 0.01 + (i % 3) * 0.05 for i in range(50)]
    spreads = [0.05] * 50
    ofis = [0.1 * (i % 5 - 2) for i in range(50)]
    X = extract_features(mid_prices, spreads, ofis)
    assert X is not None
    assert X.shape[1] == 4
    assert X.shape[0] == len(mid_prices) - 1


def test_confidence_in_range():
    """After training, posterior probabilities must sum to ~1."""
    mid_prices = [100.0 + np.random.randn() * 0.5 for _ in range(100)]
    spreads = [0.05] * 100
    ofis = [np.random.randn() * 0.1 for _ in range(100)]
    X = extract_features(mid_prices, spreads, ofis)
    model = RegimeModel()
    model.train(X)
    # predict on last 10 bars
    posteriors = model.model.predict_proba(X[-10:])
    row_sums = posteriors.sum(axis=1)
    assert np.allclose(row_sums, 1.0, atol=1e-6), f"Posteriors don't sum to 1: {row_sums}"


def test_state_label_assignment():
    """All 5 state labels must be assigned after training."""
    mid_prices = [100.0 + np.random.randn() * 0.5 for _ in range(150)]
    spreads = [0.05] * 150
    ofis = [np.random.randn() * 0.1 for _ in range(150)]
    X = extract_features(mid_prices, spreads, ofis)
    model = RegimeModel()
    model.train(X)
    assert len(model.state_labels) == 5
    assert set(model.state_labels.values()) == {
        "TRENDING_BULL", "TRENDING_BEAR", "HIGH_VOL_CHOP", "LOW_VOL_CHOP", "CRISIS"
    }
