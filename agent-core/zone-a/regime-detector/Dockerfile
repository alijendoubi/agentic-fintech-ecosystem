# ─────────────────────────────────────────────────────────────
# Zone A: Regime Detector (Python / HMM)
# ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# System deps for hmmlearn / numpy
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY hmm.py ./

# Model storage volume mount point
RUN mkdir -p /app/models

# Non-root user
RUN useradd -r -s /bin/false regime
RUN chown regime:regime /app/models
USER regime

ENV LOG_LEVEL=info \
    QUESTDB_HOST=questdb \
    QUESTDB_PORT=9000 \
    REDIS_URL=redis://redis:6379 \
    MODEL_PATH=/app/models/hmm_5state.pkl \
    INFERENCE_INTERVAL_S=1.0 \
    RETRAIN_INTERVAL_S=14400 \
    FEATURE_WINDOW=200 \
    MIN_BARS_FOR_INFERENCE=20

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import redis; r=redis.from_url('$REDIS_URL'); r.ping()" || exit 1

CMD ["python", "hmm.py"]
