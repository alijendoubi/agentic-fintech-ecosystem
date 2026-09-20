# syntax=docker/dockerfile:1
#
# Sensory Array (Zone B). BUILD CONTEXT = this directory (zone-b/sensory-array):
#   docker build -f Dockerfile -t afe-sensory-array agent-core/zone-b/sensory-array
# The crate is self-contained (no protobuf codegen), so nothing outside the
# context is needed.
#
# Optional secure QuestDB ILP (TLS + ECDSA auth):
#   docker build --build-arg CARGO_FEATURES=ilp-secure ...

# ─────────────────────────────────────────────────────────────
# Stage 1: build (pinned by tag AND digest)
# ─────────────────────────────────────────────────────────────
FROM rust:1.98.1-slim-bookworm@sha256:ff521445a372125ed4f76e1453a1f8098f2d05332d1601d30db1c1f62757e730 AS builder

# native-tls (tokio-tungstenite) needs OpenSSL headers + pkg-config.
RUN apt-get update \
    && apt-get install -y --no-install-recommends pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*

ARG CARGO_FEATURES=""
WORKDIR /build

# Dependency-cache layer: manifests + stub targets only, so editing src/ does
# not re-download and rebuild every dependency.
COPY Cargo.toml Cargo.lock ./
RUN mkdir src \
    && echo '' > src/lib.rs \
    && echo 'fn main() {}' > src/main.rs \
    && cargo build --release --locked --features "${CARGO_FEATURES}" \
    && rm -rf src

COPY src ./src
# Make sure cargo sees the real sources as newer than the stub build.
RUN touch src/lib.rs src/main.rs \
    && cargo build --release --locked --features "${CARGO_FEATURES}"

# ─────────────────────────────────────────────────────────────
# Stage 2: runtime
# ─────────────────────────────────────────────────────────────
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin sensory

WORKDIR /app
COPY --from=builder /build/target/release/sensory-array /app/sensory-array

USER sensory

# Non-secret defaults only. POLYGON_API_KEY (and QUESTDB_ILP_AUTH_*) must be
# injected at runtime; never bake them into the image.
ENV LOG_LEVEL=info \
    POLYGON_WS_URL=wss://socket.polygon.io/stocks \
    QUESTDB_ILP_HOST=questdb \
    QUESTDB_ILP_PORT=9009 \
    REDIS_URL=redis://redis:6379 \
    FRESHNESS_L2_MS=1 \
    FRESHNESS_PRINT_MS=5 \
    ROLLING_WINDOW=20 \
    ADV_WINDOW_DAYS=30 \
    MAX_RECONNECT_ATTEMPTS=5 \
    HEALTH_FILE=/tmp/sensory-array.health

# The binary checks its own heartbeat file (no procps/pgrep needed). The file
# is only refreshed while frames are flowing from the feed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/app/sensory-array", "--healthcheck"]

ENTRYPOINT ["/app/sensory-array"]
