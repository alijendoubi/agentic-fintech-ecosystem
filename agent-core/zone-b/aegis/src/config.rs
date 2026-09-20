//! Process configuration from environment variables (12-factor). Everything
//! security-relevant is explicit and fails closed:
//!
//! * `AEGIS_ENV` unset => treated as `production`.
//! * No TLS material => refuse to start unless `AEGIS_INSECURE_DEV=1` AND the
//!   environment is not production.
//! * The dev software signer is refused in production.
//!
//! Parsing takes a lookup closure so tests never touch the process environment.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::time::Duration;

use crate::error::ConfigError;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Environment {
    Production,
    Staging,
    Development,
    Test,
}

impl Environment {
    pub fn is_production(self) -> bool {
        self == Environment::Production
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TlsPaths {
    pub cert: PathBuf,
    pub key: PathBuf,
    pub client_ca: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TlsMode {
    Mutual(TlsPaths),
    /// Plaintext, DEV ONLY (never in production).
    InsecureDev,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SignerKind {
    /// Software Ed25519. DEV ONLY. Optional 32-byte hex seed file; otherwise an
    /// ephemeral key is generated (attestations do not survive a restart).
    Dev {
        seed_file: Option<PathBuf>,
    },
    Pkcs11(Pkcs11Config),
}

/// PKCS#11 settings. The PIN is read from a file, never from the environment
/// value itself, and is not part of `Debug` output.
#[derive(Clone, PartialEq, Eq)]
pub struct Pkcs11Config {
    pub module: PathBuf,
    pub token_label: String,
    pub key_label: String,
    pub pin_file: PathBuf,
}

impl std::fmt::Debug for Pkcs11Config {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Pkcs11Config")
            .field("module", &self.module)
            .field("token_label", &self.token_label)
            .field("key_label", &self.key_label)
            .field("pin_file", &"<redacted path>")
            .finish()
    }
}

#[derive(Debug, Clone)]
pub struct RuntimeConfig {
    pub env: Environment,
    pub listen: SocketAddr,
    pub limits_file: PathBuf,
    pub identities_file: PathBuf,
    pub state_dir: PathBuf,
    pub tls: TlsMode,
    pub signer: SignerKind,
    pub max_concurrency: usize,
    /// Deadline for `SubmitSignal` (budget target is 50 ms, this is the cap).
    pub submit_timeout: Duration,
    /// Deadline for every other unary RPC.
    pub rpc_timeout: Duration,
}

fn required(
    get: &dyn Fn(&str) -> Option<String>,
    key: &'static str,
) -> Result<String, ConfigError> {
    match get(key) {
        Some(v) if !v.trim().is_empty() => Ok(v),
        _ => Err(ConfigError::Missing(key)),
    }
}

fn parse_env(get: &dyn Fn(&str) -> Option<String>) -> Result<Environment, ConfigError> {
    match get("AEGIS_ENV").as_deref().map(str::trim) {
        None | Some("") | Some("production") => Ok(Environment::Production),
        Some("staging") => Ok(Environment::Staging),
        Some("development") => Ok(Environment::Development),
        Some("test") => Ok(Environment::Test),
        Some(other) => Err(ConfigError::Invalid(format!("unknown AEGIS_ENV {other:?}"))),
    }
}

fn parse_tls(
    get: &dyn Fn(&str) -> Option<String>,
    env: Environment,
) -> Result<TlsMode, ConfigError> {
    let insecure = get("AEGIS_INSECURE_DEV").as_deref() == Some("1");
    if insecure {
        if env.is_production() {
            return Err(ConfigError::Invalid(
                "AEGIS_INSECURE_DEV=1 is refused when AEGIS_ENV is production (or unset)".into(),
            ));
        }
        return Ok(TlsMode::InsecureDev);
    }
    Ok(TlsMode::Mutual(TlsPaths {
        cert: required(get, "AEGIS_TLS_CERT")?.into(),
        key: required(get, "AEGIS_TLS_KEY")?.into(),
        client_ca: required(get, "AEGIS_TLS_CLIENT_CA")?.into(),
    }))
}

fn parse_signer(
    get: &dyn Fn(&str) -> Option<String>,
    env: Environment,
) -> Result<SignerKind, ConfigError> {
    match required(get, "AEGIS_SIGNER")?.trim() {
        "dev" => {
            if env.is_production() {
                return Err(ConfigError::Invalid(
                    "the dev software signer is refused when AEGIS_ENV is production (or unset)"
                        .into(),
                ));
            }
            Ok(SignerKind::Dev {
                seed_file: get("AEGIS_DEV_SIGNING_SEED_FILE")
                    .filter(|s| !s.is_empty())
                    .map(PathBuf::from),
            })
        }
        "pkcs11" => Ok(SignerKind::Pkcs11(Pkcs11Config {
            module: required(get, "AEGIS_PKCS11_MODULE")?.into(),
            token_label: required(get, "AEGIS_PKCS11_TOKEN_LABEL")?,
            key_label: required(get, "AEGIS_PKCS11_KEY_LABEL")?,
            pin_file: required(get, "AEGIS_PKCS11_PIN_FILE")?.into(),
        })),
        other => Err(ConfigError::Invalid(format!(
            "unknown AEGIS_SIGNER {other:?}"
        ))),
    }
}

fn parse_millis(
    get: &dyn Fn(&str) -> Option<String>,
    key: &'static str,
    default_ms: u64,
) -> Result<Duration, ConfigError> {
    let ms = match get(key) {
        None => default_ms,
        Some(v) => v
            .trim()
            .parse::<u64>()
            .map_err(|_| ConfigError::Invalid(format!("{key} must be an integer number of ms")))?,
    };
    if ms == 0 {
        return Err(ConfigError::Invalid(format!("{key} must be > 0")));
    }
    Ok(Duration::from_millis(ms))
}

impl RuntimeConfig {
    pub fn from_lookup(get: &dyn Fn(&str) -> Option<String>) -> Result<RuntimeConfig, ConfigError> {
        let env = parse_env(get)?;
        let listen = get("AEGIS_LISTEN_ADDR").unwrap_or_else(|| "0.0.0.0:50051".to_owned());
        let max_concurrency = match get("AEGIS_MAX_CONCURRENCY") {
            None => 64,
            Some(v) => v
                .trim()
                .parse::<usize>()
                .ok()
                .filter(|n| *n > 0)
                .ok_or_else(|| {
                    ConfigError::Invalid("AEGIS_MAX_CONCURRENCY must be an integer > 0".into())
                })?,
        };
        Ok(RuntimeConfig {
            env,
            listen: listen.parse().map_err(|_| {
                ConfigError::Invalid(format!("AEGIS_LISTEN_ADDR {listen:?} is not host:port"))
            })?,
            limits_file: required(get, "AEGIS_LIMITS_FILE")?.into(),
            identities_file: required(get, "AEGIS_IDENTITIES_FILE")?.into(),
            state_dir: required(get, "AEGIS_STATE_DIR")?.into(),
            tls: parse_tls(get, env)?,
            signer: parse_signer(get, env)?,
            max_concurrency,
            submit_timeout: parse_millis(get, "AEGIS_SUBMIT_TIMEOUT_MS", 100)?,
            rpc_timeout: parse_millis(get, "AEGIS_RPC_TIMEOUT_MS", 2_000)?,
        })
    }

    pub fn from_env() -> Result<RuntimeConfig, ConfigError> {
        RuntimeConfig::from_lookup(&|k| std::env::var(k).ok())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn base() -> HashMap<&'static str, &'static str> {
        HashMap::from([
            ("AEGIS_ENV", "development"),
            ("AEGIS_LIMITS_FILE", "/cfg/limits.json"),
            ("AEGIS_IDENTITIES_FILE", "/cfg/identities.json"),
            ("AEGIS_STATE_DIR", "/state"),
            ("AEGIS_TLS_CERT", "/tls/server.pem"),
            ("AEGIS_TLS_KEY", "/tls/server.key"),
            ("AEGIS_TLS_CLIENT_CA", "/tls/ca.pem"),
            ("AEGIS_SIGNER", "dev"),
        ])
    }

    fn parse(m: &HashMap<&'static str, &'static str>) -> Result<RuntimeConfig, ConfigError> {
        RuntimeConfig::from_lookup(&|k| m.get(k).map(|v| (*v).to_owned()))
    }

    #[test]
    fn valid_dev_config_parses() {
        let c = parse(&base()).unwrap();
        assert_eq!(c.env, Environment::Development);
        assert!(matches!(c.tls, TlsMode::Mutual(_)));
        assert_eq!(c.max_concurrency, 64);
    }

    #[test]
    fn each_required_setting_missing_is_refused() {
        for key in [
            "AEGIS_LIMITS_FILE",
            "AEGIS_IDENTITIES_FILE",
            "AEGIS_STATE_DIR",
            "AEGIS_TLS_CERT",
            "AEGIS_TLS_KEY",
            "AEGIS_TLS_CLIENT_CA",
            "AEGIS_SIGNER",
        ] {
            let mut m = base();
            m.remove(key);
            assert!(parse(&m).is_err(), "{key} missing must refuse start");
        }
    }

    #[test]
    fn unset_env_is_production_and_refuses_dev_signer_and_insecure() {
        let mut m = base();
        m.remove("AEGIS_ENV");
        assert!(
            parse(&m).is_err(),
            "dev signer must be refused in production"
        );
        m.insert("AEGIS_SIGNER", "pkcs11");
        m.insert("AEGIS_PKCS11_MODULE", "/lib/softhsm.so");
        m.insert("AEGIS_PKCS11_TOKEN_LABEL", "afe");
        m.insert("AEGIS_PKCS11_KEY_LABEL", "attest");
        m.insert("AEGIS_PKCS11_PIN_FILE", "/run/secrets/pin");
        let c = parse(&m).unwrap();
        assert!(c.env.is_production());
        m.insert("AEGIS_INSECURE_DEV", "1");
        assert!(
            parse(&m).is_err(),
            "insecure dev must be refused in production"
        );
        m.insert("AEGIS_ENV", "production");
        assert!(parse(&m).is_err());
    }

    #[test]
    fn insecure_dev_allowed_only_outside_production() {
        let mut m = base();
        m.remove("AEGIS_TLS_CERT");
        assert!(parse(&m).is_err());
        m.insert("AEGIS_INSECURE_DEV", "1");
        assert_eq!(parse(&m).unwrap().tls, TlsMode::InsecureDev);
        m.insert("AEGIS_INSECURE_DEV", "true");
        assert!(
            parse(&m).is_err(),
            "only the literal 1 enables insecure dev"
        );
    }

    #[test]
    fn unknown_env_signer_and_bad_numbers_are_refused() {
        let mut m = base();
        m.insert("AEGIS_ENV", "prod");
        assert!(parse(&m).is_err());
        let mut m = base();
        m.insert("AEGIS_SIGNER", "hsm");
        assert!(parse(&m).is_err());
        let mut m = base();
        m.insert("AEGIS_MAX_CONCURRENCY", "0");
        assert!(parse(&m).is_err());
        let mut m = base();
        m.insert("AEGIS_SUBMIT_TIMEOUT_MS", "abc");
        assert!(parse(&m).is_err());
        let mut m = base();
        m.insert("AEGIS_LISTEN_ADDR", "nonsense");
        assert!(parse(&m).is_err());
    }

    #[test]
    fn pkcs11_debug_does_not_print_pin_path() {
        let c = Pkcs11Config {
            module: "/m".into(),
            token_label: "t".into(),
            key_label: "k".into(),
            pin_file: "/run/secrets/super-secret-pin".into(),
        };
        assert!(!format!("{c:?}").contains("super-secret-pin"));
    }
}
