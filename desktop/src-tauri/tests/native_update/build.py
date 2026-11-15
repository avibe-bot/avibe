"""Archive exact HEAD, compile a fixture assembly, sign actual installable bytes.

Never edits the checkout. All adaptation is asserted and recorded. Product
installer/verifier and automatic scheduler bytes are retained unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile

from common import command, digest, hosted, save

BASE = "0.0.1-rc.1"
NEXT = "0.0.1-rc.2"
TAG = "gh-v0.0.1rc2"
NSIS_URL = "https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.11/nsis-3.11.zip"
NSIS_SHA256 = "c7d27f780ddb6cffb4730138cd1591e841f4b7edb155856901cdf5f214394fa1"
TARGETS = {
    "aarch64-apple-darwin": ("darwin-aarch64", ".app.tar.gz"),
    "x86_64-pc-windows-msvc": ("windows-x86_64", ".exe"),
}


def provision_nsis(root: Path) -> Path:
    """Use a hash-pinned portable compiler, never an assumed runner install."""
    with urllib.request.urlopen(NSIS_URL, timeout=60) as response:
        payload = response.read(8 * 1024 * 1024 + 1)
    if len(payload) > 8 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != NSIS_SHA256:
        raise RuntimeError("Fixture NSIS archive digest mismatch")
    destination = root / "tools"
    destination.mkdir(exist_ok=False)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(destination)
    executable = destination / "nsis-3.11/makensis.exe"
    if not executable.is_file():
        raise RuntimeError("Pinned NSIS archive lacks makensis.exe")
    return executable


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError("Fixture boundary moved; inspect instead of fuzzy patching: " + old[:90])
    return text.replace(old, new)


def validate_presentation_lifecycle(library: str, fixture: str) -> dict:
    """Fail before Cargo if the reviewed real show/focus path or ordering drifts."""
    def section(start: str, end: str) -> str:
        if library.count(start) != 1 or library.count(end) != 1:
            raise RuntimeError("Native presentation source boundary moved")
        return library[library.index(start):library.index(end, library.index(start))]

    def ordered(text: str, markers: list[str]) -> None:
        if any(text.count(marker) != 1 for marker in markers):
            raise RuntimeError("Native presentation lifecycle missing or duplicated")
        positions = [text.index(marker) for marker in markers]
        if positions != sorted(positions):
            raise RuntimeError("Native presentation lifecycle order changed")

    opener = section("fn open_desktop_updates(", "\nfn request_desktop_updates(")
    callers = [name for name in ("present", "present_guarded") if (
        f'if instance_client::{name}(app, "updates") {{\n'
        '            focus_or_restore_main_window(app);\n            return true;'
    ) in opener]
    if len(callers) != 1:
        raise RuntimeError("Production update presentation no longer pairs acceptance with show/focus")
    focus = section("fn focus_or_restore_main_window(", "\nfn main_document_finished(")
    ordered(focus, ["let created = app.get_webview_window(MAIN_WINDOW).is_none();",
                    "let Some(window) = ensure_main_window(app) else {", "window.unminimize();",
                    "window.show();", "window.set_focus();", "if created {"])
    ensure = section("fn ensure_main_window(", "\nfn build_main_window(")
    ordered(ensure, ["if let Some(window) = app.get_webview_window(MAIN_WINDOW) {",
                     "return Some(window);", "build_main_window(app, None, false, None)"])
    ordered(fixture, [".plugin(tauri_plugin_global_shortcut::Builder::new()",
                      'app.manage(pet::Pet::new(data.join("pet.json")));',
                      "let coordinator = instance_client::attach(",
                      ".visible(false).build()?;",
                      'assert!(instance_client::present(app.handle(), "updates"));',
                      "super::focus_or_restore_main_window(app.handle());",
                      'emit(json!({"event":"native_ready"'])
    if any(marker in fixture for marker in (".visible(true)", ".show()", ".set_focus()")):
        raise RuntimeError("Fixture must use the production visibility lifecycle")
    return {"production_present": callers[0], "library_sha256": hashlib.sha256(library.encode()).hexdigest(),
            "production_focus_sha256": hashlib.sha256(focus.encode()).hexdigest(),
            "fixture_sha256": hashlib.sha256(fixture.encode()).hexdigest()}


def adapt(source: Path) -> dict:
    desktop = source / "desktop/src-tauri"
    library = desktop / "src/lib.rs"
    fixture_path = desktop / "examples/support/fixture_native_update.rs"
    fixture_source = fixture_path.read_text(encoding="utf-8")
    presentation = validate_presentation_lifecycle(library.read_text(encoding="utf-8"), fixture_source)
    installer_source = (desktop / "tests/native_update/fixture-installer.nsi").read_text(encoding="utf-8")
    normalized_fixture = re.sub(r"\s+", "", fixture_source)
    if not all(marker in normalized_fixture for marker in (
        "letnative_installation=native_executable.parent().unwrap().to_path_buf();",
        'std::env::set_var("AVIBE_NATIVE_ACCEPTANCE_INSTALLATION",installation);',
        "installation.canonicalize()",
    )) or not all(marker in installer_source for marker in (
        'StrCmp $0 "" refused',
        'GetFullPathName $1 "$1"',
        'GetFullPathName $2 "$2"',
    )):
        raise RuntimeError("Fixture installer path ownership normalization drifted")
    updater = desktop / "src/updater.rs"
    original = updater.read_text(encoding="utf-8")
    start = original.index("fn client() -> Result<reqwest::Client, String> {")
    end = original.index("\nasync fn fetch(", start)
    body = original[start:end]
    # Expected transport-only replacement, no find_update/auto_update_loop patch.
    if "reqwest::Client::builder()" not in body or body.count("fn ") != 1:
        raise RuntimeError("Unexpected updater HTTP boundary")
    patched = original[:start] + "fn client() -> Result<reqwest::Client, String> {\n    crate::fixture_native_update::update_client()\n}\n" + original[end:]
    # Recognize only the two reviewed layouts; preserve the callback verbatim.
    # The five-argument owner rechecks the skipped exact version after idleness.
    callbacks = {
        "pre-skip-four-arguments": (
            "|| install_edge_current(&handle, generation, install_remote.as_ref(), automatic),",
            "|| { crate::fixture_native_update::before_commit()?; install_edge_current(&handle, generation, install_remote.as_ref(), automatic) },",
        ),
        "skip-aware-five-arguments": (
            """                        || {
                            install_edge_current(
                                &handle,
                                generation,
                                install_remote.as_ref(),
                                automatic,
                                &install_version,
                            )
                        },""",
            """                        || {
                            crate::fixture_native_update::before_commit()?;
                            install_edge_current(
                                &handle,
                                generation,
                                install_remote.as_ref(),
                                automatic,
                                &install_version,
                            )
                        },""",
        ),
    }
    matches = [(name, old, new) for name, (old, new) in callbacks.items() if patched.count(old)]
    if len(matches) != 1 or patched.count(matches[0][1]) != 1:
        raise RuntimeError("Unrecognized or duplicate final installer callback; re-audit assembly")
    callback_variant, old, new = matches[0]
    patched = replace_once(patched, old, new)
    updater.write_text(patched, encoding="utf-8")
    with library.open("a", encoding="utf-8") as stream:
        stream.write('\n#[path = "../examples/support/fixture_native_update.rs"]\npub mod fixture_native_update;\n')
    # Keep the normal binary target so Tauri's Windows version resources attach.
    (desktop / "src/main.rs").write_text("fn main() { avibe_desktop_lib::fixture_native_update::run(); }\n", encoding="utf-8")
    protected = ["desktop/src-tauri/src/update_install.rs", "desktop/src-tauri/src/update_windows.ps1",
                 "desktop/runtime-host/src/update.rs", "desktop/src-tauri/src/updater/activity.rs",
                 "core/update_activity.py", "core/internal_server.py", "vibe/internal_client.py"]
    return {"callback_variant": callback_variant, "presentation_lifecycle": presentation,
            "updater_before": hashlib.sha256(original.encode()).hexdigest(),
            "updater_fixture": digest(updater), "protected_sources": {path: digest(source / path) for path in protected},
            "adaptations": ["updater HTTP transport", "pause before actual final-edge callback", "fixture entrypoint/module"]}


def mac_app(binary: Path, destination: Path, version: str) -> Path:
    app = destination / "Avibe.app"
    executable = app / "Contents/MacOS/avibe-desktop"
    executable.parent.mkdir(parents=True)
    shutil.copy2(binary, executable)
    with (app / "Contents/Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleExecutable": "avibe-desktop", "CFBundleIdentifier": "bot.avibe.desktop",
                      "CFBundleName": "Avibe", "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
                      "CFBundleShortVersionString": version, "NSHighResolutionCapable": True}, stream)
    command(["/usr/bin/codesign", "--force", "--sign", "-", app], destination)
    command(["/usr/bin/codesign", "--verify", "--deep", "--strict", app], destination)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--target", choices=TARGETS, required=True)
    args = parser.parse_args()
    root = hosted(args.root)
    rustc = subprocess.check_output(["rustc", "-vV"], text=True)
    if f"host: {args.target}" not in rustc.splitlines():
        raise RuntimeError("Runner/compiler architecture differs from the acceptance target")
    checkout = args.source.resolve()
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip() != args.head:
        raise RuntimeError("Source SHA mismatch")
    if subprocess.check_output(["git", "diff", "--name-only", "HEAD"], cwd=checkout):
        raise RuntimeError("Tracked checkout is dirty")
    source = root / "source"
    source.mkdir(exist_ok=False)
    raw = subprocess.check_output(["git", "archive", args.head], cwd=checkout)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        archive.extractall(source, filter="data")
    evidence = root / "evidence"
    evidence.mkdir(exist_ok=True)
    log = evidence / "build.log"
    record = {"source_sha": args.head, "target": args.target, "adaptation": adapt(source),
              "base_version": BASE, "next_version": NEXT, "tag": TAG,
              "event_sha": os.environ["ACCEPTANCE_EVENT_SHA"],
              "workflow_sha": os.environ["ACCEPTANCE_WORKFLOW_SHA"],
              "workflow_ref": os.environ["ACCEPTANCE_WORKFLOW_REF"],
              "runner_image": os.environ.get("ImageVersion"), "runner_os": os.environ.get("RUNNER_OS"), "rustc": rustc}
    nsis = provision_nsis(root) if os.name == "nt" else None
    if nsis is not None:
        command([nsis, "/VERSION"], root, log=log)
        record["nsis"] = {"url": NSIS_URL, "archive_sha256": NSIS_SHA256,
                          "compiler_sha256": digest(nsis)}
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise RuntimeError("Node/npm missing")
    for subdir in ("ui", "desktop"):
        command([npm, "ci"], source / subdir, log=log)
    command([npm, "run", "build"], source / "desktop", log=log)
    ui_files = ["ui/src/components/update/UpdatePanel.tsx", "ui/src/desktop/AccountApp.tsx",
                "ui/src/components/settings/SettingsPrimitives.tsx", "ui/src/i18n/en.json"]
    record["ui_sources"] = {path: digest(source / path) for path in ui_files}
    record["ui_assets"] = {str(p.relative_to(source)): digest(p) for p in (source / "desktop/dist").rglob("*") if p.is_file()}
    cli = source / "desktop/node_modules/.bin" / ("tauri.cmd" if os.name == "nt" else "tauri")
    keys = root / "private-signing"
    keys.mkdir()
    secret = keys / "fixture.key"
    # Explicit output path prevents the CLI from printing private key material.
    subprocess.run([str(cli), "signer", "generate", "--ci", "--password", "", "--write-keys", str(secret)],
                   cwd=source / "desktop", check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    public = Path(str(secret) + ".pub")
    public_key = public.read_text(encoding="utf-8").strip()
    env = {key: value for key, value in os.environ.items() if not key.startswith(("TAURI_SIGNING_", "APPLE_"))}
    env.update(AVIBE_DESKTOP_UPDATER_PUBLIC_KEY=public_key, AVIBE_ACCEPTANCE_SOURCE_SHA=args.head,
               AVIBE_ACCEPTANCE_ROOT=str(root), AVIBE_ACCEPTANCE_RUN_ID=os.environ["GITHUB_RUN_ID"],
               CARGO_TARGET_DIR=str(root / "cargo-target"))
    config_path = source / "desktop/src-tauri/tauri.conf.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    outputs = {}
    version_check = root / "verify-pe-version.ps1"
    version_check.write_text('''param([string]$Path, [string]$Expected)
$ErrorActionPreference = 'Stop'
$info = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($Path)
if ($info.ProductName -ne 'Avibe' -or $info.ProductVersion -ne $Expected) {
    throw 'Fixture PE product/version differs from the real installer contract'
}
''', encoding="utf-8")
    for name, version in (("base", BASE), ("next", NEXT)):
        config["version"] = version
        save(config_path, config)
        command(["cargo", "build", "--locked", "-p", "avibe-desktop", "--bin", "avibe-desktop",
                 "--features", "tauri/custom-protocol"], source / "desktop", env, log)
        binary = root / "cargo-target/debug" / ("avibe-desktop.exe" if os.name == "nt" else "avibe-desktop")
        directory = root / "products" / name
        directory.mkdir(parents=True)
        if os.name == "nt":
            output = directory / binary.name
            shutil.copy2(binary, output)
            command(["powershell.exe", "-NoProfile", "-NonInteractive", "-File", version_check,
                     "-Path", output, "-Expected", version], source, log=log)
        else:
            output = mac_app(binary, directory, version)
        outputs[name] = str(output)
    platform_key, extension = TARGETS[args.target]
    payloads = root / "payloads"
    payloads.mkdir()
    artifact = payloads / f"Avibe_{NEXT}_{args.target}{extension}"
    if os.name == "nt":
        assert nsis is not None
        command([nsis, "/V2", f"/DOUTPUT={artifact}", f"/DNEXT_BINARY={outputs['next']}",
                 source / "desktop/src-tauri/tests/native_update/fixture-installer.nsi"], source, log=log)
        command(["powershell.exe", "-NoProfile", "-NonInteractive", "-File", version_check,
                 "-Path", artifact, "-Expected", NEXT], source, log=log)
    else:
        with tarfile.open(artifact, "w:gz") as archive:
            archive.add(outputs["next"], arcname="Avibe.app")

    def sign(path: Path) -> str:
        subprocess.run([str(cli), "signer", "sign", "--private-key-path", str(secret), "--password", "", str(path)],
                       cwd=source / "desktop", check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return Path(str(path) + ".sig").read_text(encoding="utf-8").strip()

    try:
        manifest = payloads / f"desktop-update-{args.target}.json"
        save(manifest, {"schema_version": 1, "repository": "avibe-bot/avibe", "tag": TAG,
                        "source_sha": args.head, "channel": "test", "version": NEXT, "target": args.target,
                        "platforms": {platform_key: {"url": f"https://github.com/avibe-bot/avibe/releases/download/{TAG}/{artifact.name}",
                                                     "signature": sign(artifact), "sha256": digest(artifact), "size": artifact.stat().st_size}}})
        sign(manifest)
        command(["cargo", "run", "--locked", "-p", "avibe-runtime-host", "--example", "verify_update", "--",
                 payloads, TAG, args.head, args.target, public], source / "desktop", env, log)
    finally:
        secret.unlink(missing_ok=True)
    record.update(outputs=outputs, payload=str(artifact), payload_sha256=digest(artifact), manifest=str(manifest),
                  fixture_public_key=public_key)
    record["binary_sha256"] = {name: digest(Path(path) / "Contents/MacOS/avibe-desktop" if os.name != "nt" else Path(path))
                                for name, path in outputs.items()}
    save(root / "build.json", record)
    save(evidence / "build-provenance.json", record)


if __name__ == "__main__":
    main()
