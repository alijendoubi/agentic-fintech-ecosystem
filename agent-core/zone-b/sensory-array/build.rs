fn main() -> Result<(), Box<dyn std::error::Error>> {
    tonic_build::configure()
        .build_server(false)
        .build_client(true)
        .compile(
            &[
                "../../shared/proto/market_snapshot.proto",
                "../../shared/proto/trade_signal.proto",
            ],
            &["../../shared/proto"],
        )?;
    Ok(())
}
