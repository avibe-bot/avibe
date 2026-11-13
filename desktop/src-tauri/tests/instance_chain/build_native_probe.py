"""Build an immutable native fixture without changing product signing settings.

The updater HTTP client's socket/root trust and compile-time public test key
are fixture-owned. Automatic OS notification/popover nudges are recorded in
the fixture process; explicit UI CTA, native dialogs, mailbox dispatcher,
signature validation, generation checks and installer owner remain actual.
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
    parser.add_argument("--shell-consumers", action="store_true")
    parser.add_argument("--shell-fixture", type=Path)
    args = parser.parse_args()
    assert not any(part.endswith(".app") for part in args.output.resolve().parts), "native fixture must stay outside an application bundle"
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
    patched = original.replace(needle, replacement)
    peripheral_calls = [
        "super::request_and_open_desktop_updates(&app);",
        "notify_remote_request(&app);",
    ]
    for call in peripheral_calls:
        assert patched.count(call) == 1
        patched = patched.replace(call, 'eprintln!("fixture: automatic update notification deferred");')
    updater.write_text(patched)
    shell_adaptations = {}
    if args.shell_consumers:
        support = source / "desktop/src-tauri/examples/support/fixture_shell.rs"
        if args.shell_fixture:
            shutil.copy2(args.shell_fixture, support)
        shell_adaptations["support_sha256"] = hashlib.sha256(support.read_bytes()).hexdigest()
        library = source / "desktop/src-tauri/src/lib.rs"
        library_source = library.read_text()
        shell_adaptations["library_original_sha256"] = hashlib.sha256(library.read_bytes()).hexdigest()
        if "fn main_document_finished(" not in library_source:
            # Frozen pre-fix red assembly: mechanically extract the actual
            # callback bytes, exactly as in the production refactor.
            start = library_source.index("                        // The native frame is independent of the Runtime UI version;")
            end = library_source.index("\n                    }\n                })\n                .on_navigation", start)
            body = library_source[start:end]
            helper = "fn main_document_finished(webview: &tauri::Webview) {\n" + "".join(
                line[20:] + "\n" for line in body.splitlines()
            ) + "}\n\n"
            library_source = library_source[:start] + "                        main_document_finished(webview);" + library_source[end:]
            library_source = library_source.replace("fn receive_native_deep_link(", helper + "fn receive_native_deep_link(", 1)
            shell_adaptations["extracted_frozen_finished_callback"] = True
        library.write_text(library_source + '\n#[path = "../examples/support/fixture_shell.rs"]\npub mod fixture_shell;\n')
        native_client = source / "desktop/src-tauri/src/instance_client.rs"
        client_source = native_client.read_text()
        shell_adaptations["instance_client_original_sha256"] = hashlib.sha256(native_client.read_bytes()).hexdigest()
        start = client_source.index("pub(crate) fn return_local_for_link(")
        end = client_source.index("pub fn page_loaded", start)
        return_source = client_source[start:end]
        needle = "    tauri::async_runtime::spawn(async move {\n"
        assert return_source.count(needle) == 1
        return_source = return_source.replace(
            needle, needle + "        crate::fixture_shell::before_link_return(&app).await;\n",
        )
        client_source = client_source[:start] + return_source + client_source[end:]
        needle = "        crate::apply_pending_deep_link(&app);\n    });\n}\npub fn page_loaded"
        assert client_source.count(needle) == 1
        client_source = client_source.replace(
            needle, '        println!("{}", serde_json::json!({"event":"link_return_finished"}));\n' + needle,
        )
        native_client.write_text(client_source)
        shell_adaptations["instance_client_fixture_sha256"] = hashlib.sha256(native_client.read_bytes()).hexdigest()
        shell_adaptations["link_admission_instrumentation"] = "bounded pause after the actual claim, before the actual coordinator call"
        example = source / "desktop/src-tauri/examples/instance_client_probe.rs"
        example_source = example.read_text()
        replacements = {
            "avibe_desktop_lib::updater::init(app.handle())?;":
                "avibe_desktop_lib::fixture_shell::prepare(app.handle(), &setup.root);\n            avibe_desktop_lib::updater::init(app.handle())?;",
            "let local_origin=Arc::new(Mutex::new(Some(local.clone())));":
                "let local_origin=avibe_desktop_lib::fixture_shell::attach(app.handle(), local.clone(), &setup.root);",
            "instance_client::page_loaded(view.app_handle());":
                "avibe_desktop_lib::fixture_shell::finished(view);",
            'instance_client::present(app.handle(),"account");':
                'instance_client::present(app.handle(),"account");\n            avibe_desktop_lib::fixture_shell::start(app.handle());',
            '                        "state"=>emit(':
                '''                        "native_link"=> avibe_desktop_lib::fixture_shell::receive(&handle, command["url"].as_str().unwrap().into()),
                        "shell_control"=> avibe_desktop_lib::fixture_shell::control(&handle, command.clone()),
                        "shell_status"=> emit(avibe_desktop_lib::fixture_shell::status(&handle)),
                        "state"=>emit(''',
        }
        for needle, replacement in replacements.items():
            assert example_source.count(needle) == 1, needle
            example_source = example_source.replace(needle, replacement)
        example.write_text(example_source)
        # Keep the production output gates. Only the final OS effects are
        # recorded; no notification, badge or global shortcut reaches the user.
        notifications = source / "desktop/src-tauri/src/notifications.rs"
        notification_source = notifications.read_text()
        notification_source = notification_source.replace("use tauri_plugin_notification::NotificationExt;\n", "")
        needle = """            if sink
                .app
                .notification()
                .builder()
                .title(copy.title)
                .body(copy.body)
                .show()
                .is_err()
            {
                eprintln!("The desktop notification could not be submitted to the operating system.");
            }"""
        assert notification_source.count(needle) == 1
        notification_source = notification_source.replace(needle, '''            println!("{}", serde_json::json!({"event":"notification_output","title":copy.title,"body":copy.body}));''')
        needle = "let _ = window.set_badge_count(badge_count(total));"
        assert notification_source.count(needle) == 1
        notification_source = notification_source.replace(needle, '''let _ = window;\n        println!("{}", serde_json::json!({"event":"badge_output","count":badge_count(total)}));''')
        needle = "                    poll_sink.desktop_update_requested();"
        assert notification_source.count(needle) == 1
        notification_source = notification_source.replace(needle, '''                    println!("{}", serde_json::json!({"event":"mailbox_poll"}));
                    poll_sink.desktop_update_requested();''')
        notifications.write_text(notification_source)
        stream = source / "desktop/runtime-host/src/notifications.rs"
        stream_source = stream.read_text()
        needle = '                    if frame.event == "desktop.update.requested" {\n                        sink.desktop_update_requested();'
        assert stream_source.count(needle) == 1
        stream.write_text(stream_source.replace(needle, '''                    if frame.event == "desktop.update.requested" {
                        println!("{}", serde_json::json!({"event":"sse_update_nudge"}));
                        sink.desktop_update_requested();'''))
        pet = source / "desktop/src-tauri/src/pet.rs"
        pet_source = pet.read_text()
        needle = "        apply_shortcuts(&handle);\n        reconcile_now(&handle);"
        assert pet_source.count(needle) == 1
        pet.write_text(pet_source.replace(needle, "        reconcile_now(&handle);"))
        shell_adaptations["recorded_peripheral_calls"] = ["notification.show", "window.set_badge_count", "pet.apply_shortcuts"]
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
    assert not any(part.endswith(".app") for part in executable.resolve().parts)
    record = {
        "head": args.head, "bundle_sha256": args.bundle_sha256, "command": command,
        "updater_original_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "updater_fixture_sha256": hashlib.sha256(updater.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "binary": str(executable), "source": str(source),
        "fixture_public_key": "desktop/runtime-host/tests/fixtures/updater/public-key.txt",
        "recorded_peripheral_calls": peripheral_calls,
        "shell_adaptations": shell_adaptations,
    }
    (args.output / "provenance.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
