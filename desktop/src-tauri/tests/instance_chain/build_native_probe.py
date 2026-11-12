"""Build an immutable native fixture without changing product signing settings.

Only the updater HTTP client's socket/root trust and the compile-time public
test key differ in this test-only source copy. Actual discovery, signature
validation, CTA routing, dialog, generation checks and installer owner remain.
The checked-in artifact is inert test bytes, never an installable package.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.output / "source"
    source.mkdir()
    assert subprocess.check_output(["git", "rev-parse", args.head], cwd=args.source, text=True).strip() == args.head
    archive = subprocess.check_output(
        ["git", "archive", args.head, "desktop", "ui/src/i18n/en.json", "ui/src/i18n/zh.json"], cwd=args.source,
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(source, filter="data")
    bundle = args.bundle.read_bytes()
    assert hashlib.sha256(bundle).hexdigest() == args.bundle_sha256
    assets = source / "desktop/dist"
    assets.mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(bundle)) as stream:
        stream.extractall(assets, filter="data")
    # Fail if a source change moves the transport boundary; never fuzzy-patch
    # the discovery/decision/installation code.
    updater = source / "desktop/src-tauri/src/updater.rs"
    original = updater.read_text()
    needle = 'fn client() -> Result<reqwest::Client, String> {\n    reqwest::Client::builder()\n'
    assert original.count(needle) == 1
    replacement = '''fn client() -> Result<reqwest::Client, String> {
    let config: serde_json::Value = serde_json::from_slice(
        &std::fs::read(std::env::var("AVIBE_FIXTURE_UPDATE_CONFIG").expect("fixture config"))
            .expect("fixture config file")).expect("fixture config JSON");
    let certificate = reqwest::Certificate::from_pem(
        &std::fs::read(config["certificate"].as_str().expect("fixture certificate")).expect("certificate"))
        .expect("fixture certificate PEM");
    let proxy = reqwest::Proxy::all(config["proxy"].as_str().expect("fixture proxy")).expect("proxy");
    reqwest::Client::builder()
        .proxy(proxy)
        .tls_built_in_root_certs(false)
        .add_root_certificate(certificate)
        .redirect(reqwest::redirect::Policy::none())
'''
    updater.write_text(original.replace(needle, replacement))
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "CARGO_HOME", "RUSTUP_HOME") if key in os.environ}
    environment.setdefault("CARGO_HOME", str(Path.home() / ".cargo"))
    environment.setdefault("RUSTUP_HOME", str(Path.home() / ".rustup"))
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR"):
        path = args.output / key.lower()
        path.mkdir()
        environment[key] = str(path)
    environment["CARGO_TARGET_DIR"] = str(args.target_dir.resolve())
    environment["AVIBE_DESKTOP_UPDATER_PUBLIC_KEY"] = (
        source / "desktop/runtime-host/tests/fixtures/updater/public-key.txt"
    ).read_text().strip()
    command = [
        "cargo", "build", "--manifest-path", "desktop/Cargo.toml", "-p", "avibe-desktop",
        "--example", "instance_client_probe", "--locked", "--offline",
        "--features", "tauri/custom-protocol,tauri/macos-proxy",
    ]
    with (args.output / "build.log").open("w") as log:
        subprocess.run(command, cwd=source, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
    executable = args.output / "instance_client_probe"
    shutil.copy2(args.target_dir / "debug/examples/instance_client_probe", executable)
    record = {
        "head": args.head, "bundle_sha256": args.bundle_sha256, "command": command,
        "updater_original_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "updater_fixture_sha256": hashlib.sha256(updater.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "binary": str(executable), "source": str(source),
        "fixture_public_key": "desktop/runtime-host/tests/fixtures/updater/public-key.txt",
    }
    (args.output / "provenance.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
