//! Drift guard: the shell's default origin is the Web UI's actual default.
//!
//! `DEFAULT_ORIGIN` is a copy of two values that live in Python. A copy that
//! silently goes stale would send the desktop shell to a port nothing serves,
//! so this test reads the Python source and fails when they diverge.

use std::path::{Path, PathBuf};

use avibe_runtime_host::{LoopbackOrigin, DEFAULT_ORIGIN};

fn v2_config_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("config")
        .join("v2_config.py")
}

/// Reads `<field>: <type> = <literal>` out of a Python dataclass body.
fn dataclass_default(source: &str, field: &str) -> Option<String> {
    source
        .lines()
        .map(str::trim)
        .find(|line| line.starts_with(&format!("{field}:")))
        .and_then(|line| line.split_once('=').map(|(_, value)| value))
        .map(|value| {
            value
                .split('#')
                .next()
                .unwrap_or_default()
                .trim()
                .trim_matches('"')
                .trim_matches('\'')
                .to_owned()
        })
}

/// The shipped default has to survive the shell's own validation. It would not
/// if `UiConfig.setup_host` ever became the hostname `localhost`, which the shell
/// refuses on purpose — so this is checked separately from the drift guard, and
/// without needing the Python source.
#[test]
fn the_default_origin_is_one_the_shell_will_navigate_to() {
    let origin = LoopbackOrigin::parse(DEFAULT_ORIGIN).expect("the shipped default origin is accepted");
    assert_eq!(origin.as_str(), DEFAULT_ORIGIN);
}

#[test]
fn the_default_origin_matches_the_web_ui_config_defaults() {
    let path = v2_config_path();
    let Ok(source) = std::fs::read_to_string(&path) else {
        // The crate is buildable outside a full Avibe checkout; there is nothing
        // to guard against there.
        eprintln!("skipping: {} is not available", path.display());
        return;
    };

    let host = dataclass_default(&source, "setup_host").expect("UiConfig.setup_host has a default");
    let port = dataclass_default(&source, "setup_port").expect("UiConfig.setup_port has a default");

    assert_eq!(
        DEFAULT_ORIGIN,
        format!("http://{host}:{port}"),
        "desktop DEFAULT_ORIGIN drifted from UiConfig in {}",
        path.display()
    );
}
