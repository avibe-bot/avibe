//! Runtime discovery, launch, and readiness logic for the Avibe desktop shell.
//!
//! The crate deliberately knows nothing about Tauri. Everything the desktop
//! shell decides — which origin is acceptable, whether a Runtime is already
//! serving it, whether to start one, when the WebView may navigate — is decided
//! here, behind traits that tests can substitute.
//!
//! ```no_run
//! # async fn example() -> Result<(), Box<dyn std::error::Error>> {
//! use avibe_runtime_host::{default_runtime_host, BootstrapPhase, DiscardStatus};
//!
//! let host = default_runtime_host()?;
//! let status = host.bootstrap(&DiscardStatus).await;
//! if status.phase == BootstrapPhase::Ready {
//!     // Only now may the WebView leave the bootstrap page.
//! }
//! # Ok(())
//! # }
//! ```

#![forbid(unsafe_code)]

pub mod bootstrap;
pub mod health;
pub mod launcher;
pub mod origin;
pub mod status;

use std::sync::Arc;

pub use bootstrap::{
    DiscardStatus, RuntimeHost, RuntimeHostSettings, StatusSink, DEFAULT_POLL_INTERVAL, DEFAULT_PROBE_TIMEOUT,
    DEFAULT_READY_TIMEOUT, ORIGIN_ENV, READY_TIMEOUT_ENV,
};
pub use health::{is_avibe_health_body, HealthProbe, HttpHealthProbe};
pub use launcher::{
    vibe_executable_candidates, InstalledVibeLauncher, LaunchError, LaunchWatch, LaunchedRuntime, RuntimeLauncher,
    INSTALL_COMMAND, VIBE_PATH_ENV,
};
pub use origin::{is_shell_ui_url, LoopbackOrigin, OriginError, DEFAULT_ORIGIN, DEV_SERVER_PORT};
pub use status::{BootstrapPhase, BootstrapStatus};

/// The host the shipped desktop shell uses: real HTTP probing, the installed
/// `vibe` executable, and settings from the process environment.
pub fn default_runtime_host() -> Result<RuntimeHost, reqwest::Error> {
    let settings = RuntimeHostSettings::from_env();
    let probe = HttpHealthProbe::new(settings.probe_timeout)?;
    Ok(RuntimeHost::new(
        Arc::new(probe),
        Arc::new(InstalledVibeLauncher::from_env()),
        settings,
    ))
}
