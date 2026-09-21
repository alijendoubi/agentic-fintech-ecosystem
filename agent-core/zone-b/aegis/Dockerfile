# syntax=docker/dockerfile:1.7
#
# Aegis risk gate. BUILD CONTEXT IS agent-core/ (not this directory), because
# the crate's build.rs reads ../../shared/proto:
#
#   docker build -f agent-core/zone-b/aegis/Dockerfile -t afe/aegis:dev agent-core
#
# The layout below recreates zone-b/aegis and shared/proto side by side so the
# default proto path (../../shared/proto) resolves unchanged. The context filter
# is Dockerfile.dockerignore (next to this file, BuildKit).
#
# Optional: --build-arg AEGIS_FEATURES=pkcs11 builds the PKCS#11 signer. The
# HSM's PKCS#11 module (.so) is NOT in this image; mount or COPY the vendor
# module and point AEGIS_PKCS11_MODULE at it.

ARG RUST_TAG=1.98-bookworm
ARG RUNTIME_TAG=nonroot

FROM rust:${RUST_TAG} AS build
ARG AEGIS_FEATURES=""
RUN apt-get update \
 && apt-get install -y --no-install-recommends protobuf-compiler \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY shared/proto ./shared/proto
WORKDIR /build/zone-b/aegis
# Dependency layer: manifest + lockfile + build script first, with stub sources.
COPY zone-b/aegis/Cargo.toml zone-b/aegis/Cargo.lock zone-b/aegis/build.rs ./
RUN mkdir src \
 && echo 'fn main() {}' > src/main.rs \
 && echo '' > src/lib.rs \
 && cargo build --release --locked --features "${AEGIS_FEATURES}" \
 && rm -rf src
COPY zone-b/aegis/src ./src
RUN touch src/main.rs src/lib.rs \
 && cargo build --release --locked --features "${AEGIS_FEATURES}" --bin aegis \
 && mkdir -p /out/state \
 && cp target/release/aegis /out/aegis

# distroless/cc: glibc + libgcc only, no shell, no package manager, non-root
# user 65532. Pin by digest in CI once the base image policy is decided.
FROM gcr.io/distroless/cc-debian12:${RUNTIME_TAG}
COPY --from=build /out/aegis /usr/local/bin/aegis
COPY --from=build --chown=65532:65532 /out/state /var/lib/aegis
# Only non-risk defaults. Limits, identities, TLS material and the signer have
# NO defaults: Aegis refuses to start without them. AEGIS_ENV is deliberately
# unset (treated as production).
ENV AEGIS_STATE_DIR=/var/lib/aegis \
    AEGIS_LISTEN_ADDR=0.0.0.0:50051
VOLUME ["/var/lib/aegis"]
EXPOSE 50051
USER 65532:65532
# The healthcheck is the binary itself (TCP connect to the listen port), so no
# shell, procps or curl is needed in the image.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["/usr/local/bin/aegis", "healthcheck"]
ENTRYPOINT ["/usr/local/bin/aegis"]
