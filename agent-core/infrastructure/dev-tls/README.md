# Dev TLS bootstrap for Aegis — DEV ONLY, NOT FOR PRODUCTION

This directory generates throwaway mTLS material so `docker-compose.dev.yml`
can run Aegis with real TLS instead of `AEGIS_INSECURE_DEV=1`. Aegis now
refuses insecure mode unless it binds to loopback, which is unreachable from
other containers — so the dev override could not start Aegis at all until
this existed. **Nothing generated here is safe to reuse anywhere except a
developer's own machine.**

## What it generates

`generate-dev-certs.sh` creates one dev-only CA and, chained to it:

| Identity (CN)       | Kind   | Purpose                                              |
|----------------------|--------|-------------------------------------------------------|
| `aegis`               | server | Aegis's own TLS identity. SAN `DNS:aegis` (the in-compose hostname). |
| `cognitive-core`      | client | Submits signals — role `signal-submitter`.            |
| `execution-motor`     | client | Reports executions — role `execution-reporter`.       |
| `refdata-bridge`      | client | Pushes reference data — role `market-data-writer`.    |
| `aegis-supervisor`    | client | Liveness probe / kill-trigger — roles `state-reader` + `kill-trigger`. |

All certs are P-256 ECDSA, 30 days validity by default, and every generated
directory carries its own `README.md` stamped DEV ONLY plus a copy of `ca.pem`
so each identity's mount is self-contained.

Output layout (git-ignored, never committed — see `.gitignore` here and the
root `.gitignore`'s `*.pem` / `*.key` / `secrets/` patterns):

```
out/
├── aegis/               ca.pem  server.pem  server.key  README.md
├── cognitive-core/      ca.pem  client.pem  client.key  README.md
├── execution-motor/     ca.pem  client.pem  client.key  README.md
├── refdata-bridge/      ca.pem  client.pem  client.key  README.md
└── aegis-supervisor/    ca.pem  client.pem  client.key  README.md
```

`identities.json` (checked in — it is a template, not a secret) is the
matching **dev-only** `AEGIS_IDENTITIES_FILE`. It is NOT the production
identities file: production's stays owner-supplied and out of this repo,
mounted from `AEGIS_CONFIG_DIR` as documented in
`agent-core/zone-b/aegis/README.md`.

## Running it

```bash
cd agent-core/infrastructure/dev-tls
./generate-dev-certs.sh --yes-i-know-this-is-dev-only
# Windows Git Bash: run it via `sh generate-dev-certs.sh --yes-i-know-this-is-dev-only`
```

The `--yes-i-know-this-is-dev-only` flag is mandatory — the script refuses to
run without it, specifically so it can never be copy-pasted into a production
runbook and executed unattended.

Every run **wipes `out/` and regenerates everything from a brand-new CA**
(idempotent, but not additive): it prints the new CA's SHA-256 fingerprint so
you can tell when certs have rotated. Re-run it any time to rotate; restart
any running containers afterwards since certs signed by the old CA stop
verifying.

Optional flags: `--days N` (default 30), `--out DIR` (default `./out`).

## Wiring `.env` for the dev compose stack

From `agent-core/infrastructure/`:

```bash
cp .env.example .env   # fill in the other required secrets; see .env.example
```

`docker-compose.dev.yml` mounts the generated per-identity directories
directly (no `.env` changes needed for TLS itself — the dev override
hardcodes the `dev-tls/out/...` paths as bind-mount sources, same idea as the
`AEGIS_TLS_DIR` / `AEGIS_SUPERVISOR_TLS_DIR` variables the base
`docker-compose.yml` uses for production):

* `aegis` gets `dev-tls/out/aegis` mounted at `/tls` (`AEGIS_TLS_CERT=/tls/server.pem`,
  `AEGIS_TLS_KEY=/tls/server.key`, `AEGIS_TLS_CLIENT_CA=/tls/ca.pem`), and
  `AEGIS_IDENTITIES_FILE` still points into `${AEGIS_CONFIG_DIR:-./secrets/aegis-config}/identities.json`
  — copy this directory's `identities.json` there (or symlink it), alongside
  your own owner-supplied `limits.json`. Nothing here invents risk limits.
* `aegis-supervisor` gets `dev-tls/out/aegis-supervisor` mounted at `/tls`.
* `cognitive-core` gets `dev-tls/out/cognitive-core` mounted at `/tls` and sets
  `AEGIS_CLIENT_TLS_CA` / `AEGIS_CLIENT_TLS_CERT` / `AEGIS_CLIENT_TLS_KEY` at
  the mounted paths (matching `agent-core/zone-a/cognitive-core/sinks.py`).
* `execution-motor` and `refdata-bridge` have no runnable service/Dockerfile in
  this tree yet, so their compose blocks are commented out in
  `docker-compose.dev.yml` with the same mount/env-var pattern ready to
  uncomment once those services exist.

You still need to fill in the other `${VAR:?}` secrets in `.env` (Bedrock
region, `MODEL_HMAC_KEY`, `POLYGON_API_KEY`, audit DB passwords, HITL secret,
etc. — see `.env.example`); TLS is the only piece this directory solves.

## Do not

* Do not commit anything under `out/` (git-ignored; verify with
  `git status --porcelain agent-core/infrastructure/dev-tls/out` — it should
  print nothing after generating).
* Do not point `AEGIS_CONFIG_DIR` / `AEGIS_TLS_DIR` at this directory's output
  from a production or staging compose file.
* Do not reuse these certificates, or this identities.json, outside a single
  developer's own machine.
