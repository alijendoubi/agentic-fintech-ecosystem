# ─────────────────────────────────────────────────────────────
# Stage 1: Build
# ─────────────────────────────────────────────────────────────
FROM rust:1.82-slim AS builder

WORKDIR /build

# Cache dependencies first
COPY Cargo.toml Cargo.lock* ./
RUN mkdir src && echo 'fn main(){}' > src/main.rs
RUN cargo build --release 2>/dev/null || true
RUN rm -rf src

# Copy proto files (needed by build.rs / tonic-build)
COPY ../../shared/proto /build/shared/proto

# Copy actual source
COPY build.rs ./
COPY src ./src

# Build release binary
RUN cargo build --release

# ─────────────────────────────────────────────────────────────
# Stage 2: Runtime
# ─────────────────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    ca-certificates \
    libssl3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /build/target/release/sensory-array ./sensory-array

# Non-root user
RUN useradd -r -s /bin/false sensory
USER sensory

ENV LOG_LEVEL=info \
    POLYGON_WS_URL=wss://socket.polygon.io/stocks \
    QUESTDB_ILP_HOST=questdb \
    QUESTDB_ILP_PORT=9009 \
    REDIS_URL=redis://redis:6379 \
    FRESHNESS_L2_MS=1 \
    FRESHNESS_PRINT_MS=5 \
    ROLLING_WINDOW=20 \
    ADV_WINDOW_DAYS=30 \
    MAX_RECONNECT_ATTEMPTS=5

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD pgrep sensory-array || exit 1

CMD ["./sensory-array"]
