//! Generates the Aegis gRPC server + client stubs from the shared protos.
//!
//! Proto directory resolution (in order):
//!   1. `AEGIS_PROTO_DIR` environment variable (absolute or relative path)
//!   2. `<crate>/../../shared/proto`, which is valid both in the repo
//!      (`agent-core/zone-b/aegis` -> `agent-core/shared/proto`) and inside the
//!      Docker build, whose context is `agent-core/` and which recreates the
//!      same `zone-b/aegis` + `shared/proto` layout under `/build`.
use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR")?);
    let proto_dir = match std::env::var_os("AEGIS_PROTO_DIR") {
        Some(dir) => PathBuf::from(dir),
        None => manifest.join("../../shared/proto"),
    };
    let aegis = proto_dir.join("aegis.proto");
    if !aegis.is_file() {
        return Err(format!(
            "aegis.proto not found at {} (set AEGIS_PROTO_DIR or build with agent-core/ as context)",
            aegis.display()
        )
        .into());
    }
    println!("cargo:rerun-if-env-changed=AEGIS_PROTO_DIR");
    for name in ["aegis", "trade_signal", "order_request", "market_snapshot"] {
        println!(
            "cargo:rerun-if-changed={}",
            proto_dir.join(format!("{name}.proto")).display()
        );
    }
    tonic_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&[aegis], &[proto_dir])?;
    Ok(())
}
