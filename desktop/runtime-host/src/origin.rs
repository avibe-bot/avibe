//! Loopback origin resolution and validation.
//!
//! The shell navigates its only WebView to whatever this module accepts, so the
//! accepted set is deliberately tiny: plain `http` on the literal addresses
//! `127.0.0.1` or `[::1]`, with no credentials, path, query, or fragment.
//!
//! The hostname `localhost` is refused even though it is loopback. It resolves
//! to both `127.0.0.1` and `[::1]` on a normal machine, and the health probe and
//! the WebView resolve it independently — so a probe that succeeded over IPv4
//! could be followed by a navigation over IPv6 to nothing, since the Runtime
//! binds one literal address. The shell navigates to exactly the address it
//! proved healthy.

use std::fmt;
use std::net::{Ipv4Addr, Ipv6Addr};

use url::{Host, Url};

/// The Avibe default Web UI origin.
///
/// Mirrors `UiConfig.setup_host` / `UiConfig.setup_port` in `config/v2_config.py`.
/// `tests/default_origin.rs` fails if the two drift apart.
pub const DEFAULT_ORIGIN: &str = "http://127.0.0.1:5123";

/// Port of the Vite dev server that serves the bootstrap UI during `npm run tauri dev`.
/// Must match `desktop/vite.config.ts` and `build.devUrl` in `tauri.conf.json`.
pub const DEV_SERVER_PORT: u16 = 1420;

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum OriginError {
    #[error("The desktop origin is empty.")]
    Empty,
    #[error("The desktop origin is not a valid absolute URL.")]
    Malformed,
    #[error("The desktop origin must use http://; https and custom schemes are not accepted.")]
    UnsupportedScheme,
    #[error("The desktop origin must be a literal loopback address: 127.0.0.1 or [::1].")]
    NonLoopbackHost,
    #[error("The desktop origin must not carry a username or password.")]
    CredentialsPresent,
    #[error("The desktop origin must be a bare origin, without a path, query, or fragment.")]
    NotBareOrigin,
    #[error("The desktop origin must not be the port reserved for the shell's own UI.")]
    ShellUiOrigin,
}

/// A validated loopback origin. Constructing one is the only way to get a URL
/// the shell is willing to navigate to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoopbackOrigin {
    url: Url,
    origin: String,
}

impl LoopbackOrigin {
    pub fn parse(raw: &str) -> Result<Self, OriginError> {
        let trimmed = raw.trim();
        if trimmed.is_empty() {
            return Err(OriginError::Empty);
        }

        let url = Url::parse(trimmed).map_err(|_| OriginError::Malformed)?;

        if url.scheme() != "http" {
            return Err(OriginError::UnsupportedScheme);
        }
        if !url.username().is_empty() || url.password().is_some() {
            return Err(OriginError::CredentialsPresent);
        }
        if !matches!(url.path(), "" | "/") || url.query().is_some() || url.fragment().is_some() {
            return Err(OriginError::NotBareOrigin);
        }
        // `url` follows the WHATWG parser and canonicalizes legacy IPv4 forms:
        // `127.1`, `2130706433`, and `0x7f000001` all become `127.0.0.1`.
        // The desktop contract is deliberately narrower, so validate the raw
        // authority as well as the parsed address.
        if !has_literal_loopback_authority(trimmed) || !is_loopback_host(&url) {
            return Err(OriginError::NonLoopbackHost);
        }
        // Tauri decides a WebView's ACL origin with `is_local_url`, which counts
        // any URL relative to `devUrl` as *local*. A Workbench served on the dev
        // server's port would therefore keep matching the shell's `local: true`
        // capability after navigation, and the page would keep the bootstrap
        // commands. Nothing legitimate listens there — Vite owns the port under
        // `strictPort` — so the whole port is refused, in every build profile,
        // rather than only in the one where the hazard is reachable.
        if url.port() == Some(DEV_SERVER_PORT) {
            return Err(OriginError::ShellUiOrigin);
        }

        let origin = url.origin().ascii_serialization();
        Ok(Self { url, origin })
    }

    /// The canonical origin string handed to the bootstrap UI, e.g. `http://127.0.0.1:5123`.
    pub fn as_str(&self) -> &str {
        &self.origin
    }

    /// Where the shell navigates the WebView once the Runtime is ready.
    pub fn navigation_url(&self) -> Url {
        self.url.clone()
    }

    /// The Avibe readiness endpoint served by the Web UI server.
    pub fn health_url(&self) -> Url {
        self.url
            .join("/health")
            .expect("/health is a valid path on a validated origin")
    }
}

impl fmt::Display for LoopbackOrigin {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.origin)
    }
}

fn is_loopback_host(url: &Url) -> bool {
    match url.host() {
        Some(Host::Ipv4(address)) => address == Ipv4Addr::LOCALHOST,
        Some(Host::Ipv6(address)) => address == Ipv6Addr::LOCALHOST,
        // Including `localhost`: see the module comment.
        Some(Host::Domain(_)) | None => false,
    }
}

fn has_literal_loopback_authority(raw: &str) -> bool {
    let Some((_, remainder)) = raw.split_once("://") else {
        return false;
    };
    // A path, query, or fragment has already been rejected. Removing the one
    // permitted trailing slash leaves only the raw authority.
    let authority = remainder.strip_suffix('/').unwrap_or(remainder);
    authority_matches(authority, "127.0.0.1") || authority_matches(authority, "[::1]")
}

fn authority_matches(authority: &str, host: &str) -> bool {
    if authority == host {
        return true;
    }
    authority
        .strip_prefix(host)
        .and_then(|remainder| remainder.strip_prefix(':'))
        .is_some_and(|port| !port.is_empty() && port.bytes().all(|byte| byte.is_ascii_digit()))
}

/// Whether a WebView URL belongs to the shell's own bootstrap page.
///
/// Tauri's capability files are the enforcing boundary; this is the second
/// layer, applied inside every command so that a page loaded from the Workbench
/// origin cannot reach shell commands even if a capability is later widened by
/// mistake.
pub fn is_shell_ui_url(url: &Url) -> bool {
    let host = url.host();
    match url.scheme() {
        // macOS and Linux serve bundled app assets from tauri://localhost.
        "tauri" => matches!(host, Some(Host::Domain(domain)) if domain.eq_ignore_ascii_case("localhost")),
        "http" => match host {
            // Windows (WebView2) serves the same assets from http://tauri.localhost.
            Some(Host::Domain(domain)) if domain.eq_ignore_ascii_case("tauri.localhost") => true,
            // The Vite dev server, in development builds only.
            Some(Host::Domain(domain)) if cfg!(debug_assertions) && domain.eq_ignore_ascii_case("localhost") => {
                url.port() == Some(DEV_SERVER_PORT)
            }
            _ => false,
        },
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(raw: &str) -> Result<LoopbackOrigin, OriginError> {
        LoopbackOrigin::parse(raw)
    }

    #[test]
    fn accepts_the_avibe_default_origin() {
        let origin = parse(DEFAULT_ORIGIN).expect("the product default is accepted");
        assert_eq!(origin.as_str(), "http://127.0.0.1:5123");
        assert_eq!(origin.health_url().as_str(), "http://127.0.0.1:5123/health");
        assert_eq!(origin.navigation_url().as_str(), "http://127.0.0.1:5123/");
    }

    #[test]
    fn accepts_every_loopback_spelling_in_the_contract() {
        let cases = [
            ("http://127.0.0.1:5123", "http://127.0.0.1:5123"),
            ("http://[::1]:5123", "http://[::1]:5123"),
            ("http://127.0.0.1", "http://127.0.0.1"),
            ("http://127.0.0.1:5123/", "http://127.0.0.1:5123"),
            ("  http://127.0.0.1:5123  ", "http://127.0.0.1:5123"),
        ];
        for (raw, expected) in cases {
            let origin = parse(raw).unwrap_or_else(|error| panic!("{raw} should be accepted, got {error}"));
            assert_eq!(origin.as_str(), expected, "origin string for {raw}");
        }
    }

    /// `localhost` is loopback, and is still refused: it names two addresses, and
    /// the probe and the WebView would each pick one on their own. The shell
    /// navigates to the address it proved healthy, so it only ever handles
    /// literal ones.
    #[test]
    fn the_hostname_localhost_is_refused_however_it_is_spelled() {
        for raw in [
            "http://localhost:5123",
            "http://localhost",
            "http://LOCALHOST:5123",
            "http://localhost.:5123",
            "http://ip6-localhost:5123",
        ] {
            assert_eq!(parse(raw), Err(OriginError::NonLoopbackHost), "origin {raw:?}");
        }
    }

    #[test]
    fn canonicalized_loopback_spellings_are_not_literal_addresses() {
        for raw in [
            "http://127.1:5123",
            "http://2130706433:5123",
            "http://0x7f000001:5123",
            "http://017700000001:5123",
            "http://127.000.000.001:5123",
            "http://[0:0:0:0:0:0:0:1]:5123",
            "http://[::0001]:5123",
        ] {
            assert_eq!(parse(raw), Err(OriginError::NonLoopbackHost), "origin {raw:?}");
        }
    }

    #[test]
    fn rejects_everything_that_is_not_a_bare_loopback_http_origin() {
        let cases = [
            ("", OriginError::Empty),
            ("   ", OriginError::Empty),
            ("127.0.0.1:5123", OriginError::Malformed),
            ("not a url", OriginError::Malformed),
            ("https://127.0.0.1:5123", OriginError::UnsupportedScheme),
            ("file:///etc/passwd", OriginError::UnsupportedScheme),
            ("tauri://localhost", OriginError::UnsupportedScheme),
            ("ws://127.0.0.1:5123", OriginError::UnsupportedScheme),
            ("http://192.168.1.10:5123", OriginError::NonLoopbackHost),
            ("http://127.0.0.2:5123", OriginError::NonLoopbackHost),
            ("http://0.0.0.0:5123", OriginError::NonLoopbackHost),
            ("http://example.com", OriginError::NonLoopbackHost),
            ("http://localhost.example.com", OriginError::NonLoopbackHost),
            ("http://[::2]:5123", OriginError::NonLoopbackHost),
            ("http://attacker@127.0.0.1:5123", OriginError::CredentialsPresent),
            ("http://user:pass@127.0.0.1:5123", OriginError::CredentialsPresent),
            ("http://127.0.0.1:5123/admin", OriginError::NotBareOrigin),
            ("http://127.0.0.1:5123/?next=/x", OriginError::NotBareOrigin),
            ("http://127.0.0.1:5123/#/chat", OriginError::NotBareOrigin),
            ("http://127.0.0.1:1420", OriginError::ShellUiOrigin),
            ("http://[::1]:1420", OriginError::ShellUiOrigin),
        ];
        for (raw, expected) in cases {
            assert_eq!(parse(raw), Err(expected), "origin {raw:?}");
        }
    }

    #[test]
    fn rejected_origins_explain_themselves_without_leaking_the_value() {
        let error = parse("https://example.com").expect_err("https is rejected");
        let message = error.to_string();
        assert!(message.contains("http://"), "message should name the accepted scheme");
        assert!(
            !message.contains("example.com"),
            "message must not echo the rejected value"
        );
    }

    #[test]
    fn shell_ui_urls_are_the_bundled_app_and_dev_server_only() {
        let shell = [
            "tauri://localhost/index.html",
            "tauri://localhost",
            "http://tauri.localhost/index.html",
        ];
        for raw in shell {
            let url = Url::parse(raw).expect("test url parses");
            assert!(is_shell_ui_url(&url), "{raw} is the shell's own page");
        }

        let not_shell = [
            // The Workbench origin: reachable in the same WebView after navigation.
            "http://127.0.0.1:5123/",
            "http://127.0.0.1:5123/show/abc/",
            "https://tauri.localhost/",
            "http://tauri.localhost.example.com/",
            "https://avibe.bot/",
            "file:///index.html",
        ];
        for raw in not_shell {
            let url = Url::parse(raw).expect("test url parses");
            assert!(!is_shell_ui_url(&url), "{raw} must not reach shell commands");
        }
    }

    #[test]
    fn the_vite_dev_server_is_shell_ui_only_in_development_builds() {
        let url = Url::parse(&format!("http://localhost:{DEV_SERVER_PORT}/")).expect("dev url parses");
        assert_eq!(is_shell_ui_url(&url), cfg!(debug_assertions));

        // A different loopback port is never the shell, in either build profile.
        let other = Url::parse("http://localhost:5123/").expect("test url parses");
        assert!(!is_shell_ui_url(&other));
    }

    /// The port the shell's own UI is served from is the one loopback port a
    /// Runtime must never be reached at: a page there would be classified as
    /// *local* by Tauri and would keep the bootstrap commands after navigation.
    /// The refusal is unconditional, so a release build cannot be the only thing
    /// standing between a misconfigured origin and a privileged Workbench.
    #[test]
    fn the_shells_own_port_is_refused_in_every_build_profile() {
        for host in ["127.0.0.1", "[::1]"] {
            let raw = format!("http://{host}:{DEV_SERVER_PORT}");
            assert_eq!(parse(&raw), Err(OriginError::ShellUiOrigin), "origin {raw:?}");

            // Neighbouring ports stay perfectly acceptable: it is one reserved
            // port, not a range.
            for port in [DEV_SERVER_PORT - 1, DEV_SERVER_PORT + 1] {
                let neighbour = format!("http://{host}:{port}");
                assert!(parse(&neighbour).is_ok(), "origin {neighbour:?} should be accepted");
            }
        }
    }
}
