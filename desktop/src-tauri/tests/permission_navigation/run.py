"""Run the macOS permission-navigation owner without an AppKit application.

Copy the real controller queue, event loop, navigation/enable/activation methods,
and request dispatcher byte-for-byte into a test-only Cargo assembly. Only OS,
process and view adapters are replaced by recorders. The real runtime-host
lifecycle is a path dependency. No product hook, window, TCC call or driver runs.
Run against --source at the parent revision as well to establish the regression.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def block(source: str, signature: str, indent: int = 0) -> str:
    """Take a complete Rust item at its own indentation, not a rewritten body."""
    prefix = " " * indent
    matches = list(re.finditer(r"^" + re.escape(prefix + signature), source, re.M))
    if len(matches) != 1:
        raise ValueError(f"Expected one source item: {signature}")
    start = matches[0].start()
    end = re.search(r"^" + prefix + r"\}", source[matches[0].end():], re.M)
    if end is None:
        raise ValueError(f"Unclosed source item: {signature}")
    return source[start:matches[0].end() + end.end()]


def assemble(source_root: Path, output: Path, controller_source: Path | None = None) -> dict:
    source = (controller_source or source_root / "desktop/src-tauri/src/computer_use.rs").read_text()
    items: dict[str, str] = {}

    def take(signature: str, indent: int = 0) -> str:
        text = block(source, signature, indent)
        items[signature] = hashlib.sha256(text.encode()).hexdigest()
        return text

    declarations = "\n".join([
        "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
        take("pub(crate) enum PermissionPane"), take("impl PermissionPane"),
        take("enum Event"), take("pub(crate) struct Controller"),
        "#[derive(Clone, Debug, Eq, PartialEq)]",
        take("pub(crate) struct MenuView"), take("impl Default for MenuView"),
        "#[derive(Clone, Debug, Eq, PartialEq)]",
        take("struct PermissionRequestChecks"),
    ])
    controller = "impl Controller {\n" + "\n".join(take(sig, 4) for sig in [
        "pub(crate) fn toggle(", "pub(crate) fn turn_on(",
        "pub(crate) fn open_permission(", "fn request(", "pub(crate) fn activated(",
    ]) + "\n}"
    runtime = "impl RuntimeState {\n" + "\n".join(take(sig, 4) for sig in [
        "async fn run(", "async fn toggle(", "async fn open_permission(",
        "async fn activated(", "fn settle_setup_resume(",
    ]) + "\n}"
    helpers = "\n".join(take(sig) for sig in [
        "fn silent_grants(", "async fn run_permission_request<",
        "async fn request_permissions(", "fn open_missing_permission_settings(",
        "fn open_permission_settings(", "fn setup_resume_settled(",
        "fn unix_seconds(", "fn arm_setup_resume(",
    ])
    # The baseline has this wrapper; the fix removes its only consumer and it.
    if re.search(r"^async fn request_accessibility_grant\(", source, re.M):
        helpers += "\n" + take("async fn request_accessibility_grant(")
        helpers += "\nuse tokio::time::timeout;"
    fixture = Path(__file__).with_name("fixture.rs").read_text()
    generated = fixture.replace("// @SOURCE_DECLARATIONS@", declarations)
    generated = generated.replace("// @SOURCE_CONTROLLER@", controller)
    generated = generated.replace("// @SOURCE_RUNTIME@", runtime)
    generated = generated.replace("// @SOURCE_HELPERS@", helpers)
    output.mkdir(parents=True, exist_ok=True)
    (output / "src").mkdir(exist_ok=True)
    (output / "src/lib.rs").write_text(generated)
    host = str(source_root / "desktop/runtime-host")
    (output / "Cargo.toml").write_text(
        '[package]\nname = "computer-use-navigation-fixture"\n'
        'version = "0.0.0"\nedition = "2021"\n[workspace]\n'
        '[dependencies]\navibe-runtime-host = { path = ' + json.dumps(host) + ' }\n'
        'tokio = { version = "1", features = ["rt", "time", "sync", "macros", "test-util"] }\n'
        'tempfile = "3"\n'
    )
    (output / "Cargo.lock").write_text((source_root / "desktop/Cargo.lock").read_text())
    manifest = {
        "source": str(source_root),
        "controller_source": str(controller_source) if controller_source else None,
        "controller_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "unchanged_items": items,
        "fixture_sha256": hashlib.sha256(generated.encode()).hexdigest(),
        "os_adapters": "recorders only; no AppKit, TCC, capture or driver",
    }
    (output / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--controller-source", type=Path,
                        help="Unmodified controller file from the baseline for red/green comparison")
    args = parser.parse_args()
    assemble(args.source.resolve(), args.output.resolve(), args.controller_source)
    env = dict(os.environ, CARGO_TARGET_DIR=str(args.target_dir.resolve()),
               CARGO_BUILD_JOBS="1", CARGO_INCREMENTAL="0", CARGO_PROFILE_DEV_DEBUG="0")
    result = subprocess.run([
        "cargo", "test", "--offline", "--manifest-path", str(args.output / "Cargo.toml"),
        "--", "--nocapture", "--test-threads=1",
    ], env=env, check=False)
    (args.output / "result.json").write_text(json.dumps({"exit_code": result.returncode}) + "\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
