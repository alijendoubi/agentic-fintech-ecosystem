# syntax=docker/dockerfile:1
# AFE HITL operator terminal. Multi-stage, pinned base, non-root runtime.
# Build context: agent-core/zone-c/hitl-interface (matches infrastructure/docker-compose.yml).
ARG NODE_IMAGE=node:22.21.1-alpine3.22

FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM ${NODE_IMAGE} AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# No secrets are needed or accepted at build time; all pages are rendered per request.
RUN npm run build

FROM ${NODE_IMAGE} AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN addgroup -S -g 10001 hitl && adduser -S -u 10001 -G hitl -h /app hitl
COPY --from=build --chown=hitl:hitl /app/.next/standalone ./
COPY --from=build --chown=hitl:hitl /app/.next/static ./.next/static
USER 10001:10001
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+process.env.PORT+'/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
# The app exits with code 1 at startup if HITL_JWT_SECRET is unset, short, or a placeholder.
CMD ["node", "server.js"]
