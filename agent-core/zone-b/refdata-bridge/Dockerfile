# ─────────────────────────────────────────────────────────────
# Zone B: Reference-Data Bridge (`python -m refdata_bridge.main`)
#
# BUILD CONTEXT IS agent-core/ (not this directory): the image needs ../../shared/proto
# to generate the gRPC stubs, same convention as zone-a/cognitive-core/Dockerfile.
#   docker build -f zone-b/refdata-bridge/Dockerfile -t afe-refdata-bridge agent-core
# docker-compose:
#   build:
#     context: ..
#     dockerfile: zone-b/refdata-bridge/Dockerfile
# ─────────────────────────────────────────────────────────────

# Stage 1: generate Python message + gRPC stubs from the shared protos.
FROM python:3.12-slim AS protos
WORKDIR /proto
RUN pip install --no-cache-dir "grpcio-tools>=1.67,<2"
COPY shared/proto/ ./
RUN mkdir /generated \
    && python -m grpc_tools.protoc -I. --python_out=/generated --grpc_python_out=/generated \
        market_snapshot.proto trade_signal.proto order_request.proto aegis.proto

# Stage 2: runtime.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY zone-b/refdata-bridge/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY zone-b/refdata-bridge/refdata_bridge/ ./refdata_bridge/
COPY --from=protos /generated ./generated

RUN useradd --system --no-create-home --shell /bin/false refdata \
    && chown -R refdata:refdata /app
USER refdata

ENV PYTHONPATH=/app \
    AFE_PROTO_DIR=/app/generated \
    LOG_LEVEL=info \
    REDIS_URL=redis://redis:6379 \
    SNAPSHOT_CHANNEL=sensory:snapshots \
    REGIME_CHANNEL=regime:labels \
    AEGIS_TARGET=aegis:50051 \
    BATCH_INTERVAL_S=1.0 \
    HEALTH_HOST=0.0.0.0 \
    HEALTH_PORT=8080 \
    HEARTBEAT_PATH=/tmp/refdata-bridge.heartbeat

EXPOSE 8080

# Healthy = GET /health returns 200 (last successful Aegis push was recent enough).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "refdata_bridge.main"]
