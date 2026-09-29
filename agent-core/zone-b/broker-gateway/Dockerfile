# syntax=docker/dockerfile:1.7
#
# Broker gateway (ADR-004 Option C): the only holder of broker credentials.
# BUILD CONTEXT IS agent-core/ (not this directory): the image needs the generated stubs from
# shared/proto and the motor's attestation modules (reused, not copied into this package):
#
#   docker build -f agent-core/zone-b/broker-gateway/Dockerfile -t afe/broker-gateway:dev agent-core
#
# The context filter is Dockerfile.dockerignore (next to this file, BuildKit).
# Nothing risk-relevant has a default: TLS material, the client allow-list, the attestation key
# registry, the broker credentials and GATEWAY_ENV (unset = production) come from the operator.

ARG PYTHON_TAG=3.12.7-slim-bookworm

FROM python:${PYTHON_TAG} AS build
WORKDIR /build
COPY shared/proto ./shared/proto
RUN pip install --no-cache-dir "grpcio-tools>=1.67.0"
WORKDIR /build/shared/proto
RUN PYTHON=python bash ./generate.sh  # via bash: the file is mode 100644 in git

FROM python:${PYTHON_TAG} AS runtime
RUN groupadd -g 10002 gateway \
 && useradd -u 10002 -g gateway -M -d /app -s /usr/sbin/nologin gateway
WORKDIR /app
COPY zone-b/broker-gateway/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=build /build/shared/generated ./shared/generated
COPY zone-b/broker-gateway/broker_gateway ./broker_gateway
# Shared attestation code, reused from execution-motor (canonical text, verifier, key registry).
# Exactly these files; tests/test_shared_code.py fails if the gateway imports anything else.
COPY zone-b/execution-motor/execution_motor/__init__.py \
     zone-b/execution-motor/execution_motor/canonical.py \
     zone-b/execution-motor/execution_motor/errors.py \
     zone-b/execution-motor/execution_motor/keys.py \
     zone-b/execution-motor/execution_motor/verifiers.py \
     ./execution_motor/
ENV PYTHONPATH=/app:/app/shared/generated \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_LISTEN_ADDR=0.0.0.0:50071
EXPOSE 50071
USER 10002:10002
# No shell/curl dependency: a plain TCP connect to the listen port (same approach and reason as
# execution-motor and Aegis: an authenticated check would need a client certificate here).
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,socket; a=os.environ.get('GATEWAY_LISTEN_ADDR','127.0.0.1:50071'); h,_,p=a.rpartition(':'); h=h.strip('[]'); h='127.0.0.1' if h in ('','0.0.0.0') else h; s=socket.create_connection((h,int(p)),timeout=2); s.close()"]
ENTRYPOINT ["python", "-m", "broker_gateway"]
