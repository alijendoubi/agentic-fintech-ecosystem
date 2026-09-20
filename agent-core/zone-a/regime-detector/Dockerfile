# ─────────────────────────────────────────────────────────────
# Zone A: Regime Detector (Python / HMM)
#
# Build context: agent-core/zone-a/regime-detector  (this directory)
#   docker build -t afe-regime-detector agent-core/zone-a/regime-detector
# docker-compose:
#   build:
#     context: ../zone-a/regime-detector
#     dockerfile: Dockerfile
# ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# numpy/scipy/scikit-learn/hmmlearn ship manylinux wheels: no compiler needed.
# libgomp1 is the OpenMP runtime some of those wheels link against.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (cached layer)
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Source: thin entrypoint + package
COPY hmm.py ./
COPY regime_detector/ ./regime_detector/

# Model storage volume mount point; non-root user owns it
RUN useradd --system --no-create-home --shell /bin/false regime \
    && mkdir -p /app/models \
    && chown regime:regime /app/models
USER regime

# MODEL_HMAC_KEY (>= 32 chars) must be injected at runtime from a secret store.
# Without it models are trained in memory only and never loaded from / saved to disk.
ENV LOG_LEVEL=info \
    QUESTDB_HOST=questdb \
    QUESTDB_PORT=9000 \
    REDIS_URL=redis://redis:6379 \
    MODEL_DIR=/app/models \
    INFERENCE_INTERVAL_S=1.0 \
    RETRAIN_INTERVAL_S=14400 \
    FEATURE_WINDOW=200 \
    MIN_BARS_FOR_INFERENCE=20 \
    HEARTBEAT_PATH=/tmp/regime-detector.heartbeat

# Healthy = the inference loop touched its heartbeat file recently.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-m", "regime_detector.healthcheck"]

CMD ["python", "hmm.py"]
