# syntax=docker/dockerfile:1.7
#
# Execution Motor: attestation-gated, cap-checked, halt-aware order execution.
# BUILD CONTEXT IS agent-core/ (not this directory), because the generated Python stubs
# come from ../../shared/proto:
#
#   docker build -f agent-core/zone-b/execution-motor/Dockerfile -t afe/execution-motor:dev agent-core
#
# The context filter is Dockerfile.dockerignore (next to this file, BuildKit).
#
# Risk-relevant settings have NO defaults in this image: TLS material, the attestation key
# registry, broker credentials and MOTOR_ENV are all left for the operator (see README.md).
# MOTOR_ENV is deliberately unset (treated as production, same convention as Aegis's
# AEGIS_ENV).

ARG PYTHON_TAG=3.12.7-slim-bookworm

FROM python:${PYTHON_TAG} AS build
WORKDIR /build
COPY shared/proto ./shared/proto
RUN pip install --no-cache-dir "grpcio-tools>=1.67.0"
WORKDIR /build/shared/proto
RUN PYTHON=python ./generate.sh

FROM python:${PYTHON_TAG} AS runtime
RUN groupadd -g 10001 motor \
 && useradd -u 10001 -g motor -M -d /app -s /usr/sbin/nologin motor
WORKDIR /app
COPY zone-b/execution-motor/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=build /build/shared/generated ./shared/generated
COPY zone-b/execution-motor/__init__.py ./__init__.py
COPY zone-b/execution-motor/execution_motor ./execution_motor
ENV PYTHONPATH=/app:/app/shared/generated \
    PYTHONUNBUFFERED=1 \
    MOTOR_LISTEN_ADDR=0.0.0.0:50061 \
    MOTOR_STATE_DIR=/var/lib/execution-motor
RUN mkdir -p /var/lib/execution-motor && chown motor:motor /var/lib/execution-motor
VOLUME ["/var/lib/execution-motor"]
EXPOSE 50061
USER 10001:10001
# No shell/curl dependency: a plain TCP connect to the listen port (same approach as
# zone-b/aegis's Dockerfile, whose comment explains why an authenticated Health RPC is not
# used for the container healthcheck: the healthcheck process would need its own client
# certificate, which this image does not carry).
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,socket; a=os.environ.get('MOTOR_LISTEN_ADDR','127.0.0.1:50061'); h,_,p=a.rpartition(':'); h=h.strip('[]') or '127.0.0.1'; s=socket.create_connection((h,int(p)),timeout=2); s.close()"]
ENTRYPOINT ["python", "-m", "execution_motor"]
