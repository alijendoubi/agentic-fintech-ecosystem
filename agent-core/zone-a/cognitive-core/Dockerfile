# ─────────────────────────────────────────────────────────────
# Zone A: Cognitive Core runner (`python -m cognitive_core`)
#
# BUILD CONTEXT IS agent-core/ (not this directory): the image needs ../../shared/proto to
# generate the gRPC stubs and ../vector-db for the optional memory layer.
#   docker build -f zone-a/cognitive-core/Dockerfile -t afe-cognitive-core agent-core
# docker-compose (infrastructure/docker-compose.yml) currently points at
# `context: ../zone-a/cognitive-core`; it must become:
#   build:
#     context: ..
#     dockerfile: zone-a/cognitive-core/Dockerfile
#
# Image without chromadb (memory layer disabled at build time):
#   docker build --build-arg WITH_MEMORY=false ...
# ─────────────────────────────────────────────────────────────

# Stage 1: generate Python message + gRPC stubs from the shared protos.
FROM python:3.12-slim AS protos
WORKDIR /proto
RUN pip install --no-cache-dir "grpcio-tools>=1.67,<2"
COPY shared/proto/ ./
RUN mkdir /generated \
    && python -m grpc_tools.protoc -I. --python_out=/generated --grpc_python_out=/generated \
        market_snapshot.proto trade_signal.proto order_request.proto aegis.proto \
        execution_motor.proto

# Stage 2: runtime.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY zone-a/cognitive-core/requirements-runtime.txt ./requirements-runtime.txt
RUN pip install -r requirements-runtime.txt

ARG WITH_MEMORY=true
COPY zone-a/vector-db/requirements.txt ./requirements-memory.txt
RUN if [ "$WITH_MEMORY" = "true" ]; then pip install -r requirements-memory.txt; fi

# The hyphenated directory is installed under its importable name (see __init__.py/conftest.py).
COPY zone-a/cognitive-core/ ./cognitive_core/
COPY zone-a/vector-db/afe_vector_memory/ ./afe_vector_memory/
COPY --from=protos /generated ./generated

# Non-root. /var/run/afe holds the operator halt file (create it to stop all signals).
RUN useradd --system --no-create-home --shell /bin/false cognitive \
    && mkdir -p /var/run/afe \
    && chown cognitive:cognitive /var/run/afe
USER cognitive

# Safe defaults: quantity 0 means every debate abstains; memory is opt-in. LLM access is via
# the Bedrock IAM role (ADR-002): no API keys belong in this image or its environment.
ENV PYTHONPATH=/app \
    AFE_PROTO_DIR=/app/generated \
    COGNITIVE_HEALTH_HOST=0.0.0.0 \
    COGNITIVE_HEALTH_PORT=8080 \
    COGNITIVE_HALT_FILE=/var/run/afe/halt \
    COGNITIVE_SINK=grpc \
    COGNITIVE_MEMORY_ENABLED=false

EXPOSE 8080

# Exits non-zero unless /health answers 200 (a stalled loop answers 503).
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-m", "cognitive_core.healthcheck"]

CMD ["python", "-m", "cognitive_core"]
