# syntax=docker/dockerfile:1.7
#
# HITL backend (ALI-156). BUILD CONTEXT IS agent-core/:
#   docker build -f agent-core/zone-c/hitl-backend/Dockerfile -t afe/hitl-backend:dev agent-core
# Context filter: Dockerfile.dockerignore next to this file. No secrets or defaults for them here.

ARG PYTHON_TAG=3.12.7-slim-bookworm

FROM python:${PYTHON_TAG} AS build
WORKDIR /build
COPY shared/proto ./shared/proto
RUN pip install --no-cache-dir "grpcio-tools>=1.67.0"
WORKDIR /build/shared/proto
RUN PYTHON=python bash ./generate.sh

FROM python:${PYTHON_TAG} AS runtime
RUN groupadd -g 10002 hitl \
 && useradd -u 10002 -g hitl -M -d /app -s /usr/sbin/nologin hitl
WORKDIR /app
COPY zone-c/hitl-backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=build /build/shared/generated ./shared/generated
COPY zone-c/hitl-backend/hitl_backend ./hitl_backend
COPY zone-c/audit-logger/afe_audit ./afe_audit
ENV PYTHONPATH=/app:/app/shared/generated \
    AFE_PROTO_DIR=/app/shared/generated \
    PYTHONUNBUFFERED=1 \
    HITL_BACKEND_LISTEN=0.0.0.0:8090
EXPOSE 8090
USER 10002:10002
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8090/healthz', timeout=2)"]
ENTRYPOINT ["python", "-m", "hitl_backend"]
