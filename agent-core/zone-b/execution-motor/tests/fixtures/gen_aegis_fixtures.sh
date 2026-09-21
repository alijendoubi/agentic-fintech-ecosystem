#!/bin/bash
# Regenerates aegis_attestations.json from the real Aegis crate (Git Bash, repo root).
# Prints the JSON on stdout. Only a scratch copy inside the container is touched.
set -euo pipefail
MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd -W):/src:ro" \
  -v afe-cargo-cache:/usr/local/cargo/registry -v afe-cargo-target-x2:/target \
  -e CARGO_TARGET_DIR=/target -w /work afe-rust-dev bash -c '
  cd /src && tar --exclude=target --exclude=.git -cf - agent-core | tar -xf - -C /work
  mkdir -p /work/agent-core/zone-b/aegis/examples
  cp /src/agent-core/zone-b/execution-motor/tests/fixtures/emit_aegis_fixtures.rs \
     /work/agent-core/zone-b/aegis/examples/
  cd /work/agent-core/zone-b/aegis && cargo run -q --example emit_aegis_fixtures'
