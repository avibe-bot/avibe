from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile


SCRIPT = Path(__file__).parents[1] / "ios/scripts/prepare_iroh_app.py"
spec = importlib.util.spec_from_file_location("prepare_iroh_app", SCRIPT)
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


BINARY_MEMBERS = {
    "Iroh.xcframework/Info.plist": b"plist",
    "Iroh.xcframework/ios-arm64/libiroh_ffi.a": b"device",
    "Iroh.xcframework/ios-arm64/Headers/Export.h": b"#include \"iroh_ffiFFI.h\"",
    "Iroh.xcframework/ios-arm64/Headers/iroh_ffiFFI.h": b"header",
    "Iroh.xcframework/ios-arm64/Headers/module.modulemap": b"module Iroh {}",
    "Iroh.xcframework/ios-arm64_x86_64-simulator/libiroh_ffi-ios-simulator.a": b"simulator",
    "Iroh.xcframework/ios-arm64_x86_64-simulator/Headers/Export.h": b"#include \"iroh_ffiFFI.h\"",
    "Iroh.xcframework/ios-arm64_x86_64-simulator/Headers/iroh_ffiFFI.h": b"header",
    "Iroh.xcframework/ios-arm64_x86_64-simulator/Headers/module.modulemap": b"module Iroh {}",
}


def archive(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as result:
        for name, content in members.items():
            result.writestr(name, content)
    return output.getvalue()


def pin_for(staging: Path, *, published: bool = True) -> dict:
    binary = (staging / "IrohLib.xcframework.zip").read_bytes()
    binding = (staging / "IrohLib.swift").read_bytes()
    support = (staging / "IrohLib-support.zip").read_bytes()
    assets = {}
    for name, data in (
        (app.BINARY_NAME, binary),
        (app.BINDING_NAME, binding),
        (app.SUPPORT_NAME, support),
        (app.MANIFEST_NAME, (staging / app.MANIFEST_NAME).read_bytes()),
        *[(name, (staging / name).read_bytes()) for name in app.GRAPH_NAMES],
    ):
        assets[name] = {
            "url": f"https://github.com/avibe-bot/avibe/releases/download/iroh-apple-v1.2.0-1/{name}",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
    return {
        "schema_version": 1,
        "release_state": "published" if published else "pending",
        "release_tag": "iroh-apple-v1.2.0-1",
        "sha256": hashlib.sha256(binary).hexdigest(),
        "upstream_commit": "commit",
        "iroh_core_version": "1.4.1",
        "deployment_target": "17.0",
        "assets": assets,
    }


def make_staging(tmp_path: Path) -> Path:
    staging = tmp_path / "IrohLib"
    staging.mkdir(parents=True)
    binding = b"// generated wrapper\n"
    binary = archive(BINARY_MEMBERS)
    (staging / "IrohLib.xcframework.zip").write_bytes(binary)
    for name, content in BINARY_MEMBERS.items():
        path = staging / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (staging / "IrohLib.swift").write_bytes(binding)
    (staging / "IrohLib-support.zip").write_bytes(archive({"IrohLib.swift": binding}))
    (staging / "manifest.json").write_text(json.dumps({
        "iroh_ffi_commit": "commit",
        "iroh_core_version": "1.4.1",
        "ios_deployment_target": "17.0",
        "features": ["metrics", "tls-ring"],
        "excluded_iroh_features": ["fast-apple-datapath", "portmapper"],
    }))
    for name in app.GRAPH_NAMES:
        (staging / name).write_text('iroh feature "metrics"\niroh feature "tls-ring"\n')
    (staging / "verified.json").write_text(json.dumps({
        "schema_version": 1,
        "release_tag": "iroh-apple-v1.2.0-1",
        "binary_sha256": hashlib.sha256((staging / "IrohLib.xcframework.zip").read_bytes()).hexdigest(),
        "binding_sha256": hashlib.sha256(binding).hexdigest(),
        "support_sha256": hashlib.sha256((staging / "IrohLib-support.zip").read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256((staging / "manifest.json").read_bytes()).hexdigest(),
        "graph_sha256": {
            name: hashlib.sha256((staging / name).read_bytes()).hexdigest()
            for name in app.GRAPH_NAMES
        },
    }))
    return staging


def test_pending_pin_is_rejected(tmp_path):
    staging = make_staging(tmp_path)
    pin = pin_for(staging, published=False)
    path = tmp_path / "release.json"
    path.write_text(json.dumps(pin))
    try:
        app.load_pin(path)
    except ValueError as error:
        assert "not published" in str(error)
    else:
        raise AssertionError("pending consumer pin was accepted")


def test_verify_local_requires_published_pin_and_matching_wrapper(tmp_path):
    staging = make_staging(tmp_path)
    pin = pin_for(staging)
    assert app.verify_local(staging, pin)["release_tag"] == pin["release_tag"]
    pending = {**pin, "release_state": "pending"}
    try:
        app.verify_local(staging, pending)
    except ValueError as error:
        assert "not published" in str(error)
    else:
        raise AssertionError("pending consumer pin was accepted")

    (staging / "IrohLib.swift").write_bytes(b"different")
    try:
        app.verify_local(staging, pin)
    except ValueError as error:
        assert "published pin" in str(error) or "support archive" in str(error)
    else:
        raise AssertionError("wrapper provenance mismatch was accepted")


def test_feature_graph_rejects_forbidden_discovery(tmp_path):
    staging = make_staging(tmp_path)
    (staging / app.GRAPH_NAMES[0]).write_text('iroh feature "metrics"\nmulticast\n')
    pin = pin_for(staging)
    try:
        app.verify_local(staging, pin)
    except ValueError as error:
        assert "local discovery" in str(error)
    else:
        raise AssertionError("forbidden feature graph was accepted")


def test_verify_local_rejects_extracted_framework_changes(tmp_path):
    staging = make_staging(tmp_path)
    pin = pin_for(staging)
    (staging / "Iroh.xcframework/ios-arm64/libiroh_ffi.a").write_bytes(b"tampered")
    try:
        app.verify_local(staging, pin)
    except ValueError as error:
        assert "staged xcframework file" in str(error)
    else:
        raise AssertionError("tampered extracted framework was accepted")

    staging = make_staging(tmp_path / "missing")
    (staging / "Iroh.xcframework/ios-arm64/Headers/module.modulemap").unlink()
    try:
        app.verify_local(staging, pin_for(staging))
    except ValueError as error:
        assert "staged xcframework files differ" in str(error)
    else:
        raise AssertionError("incomplete extracted framework was accepted")

    staging = make_staging(tmp_path / "extra")
    (staging / "Iroh.xcframework/extra.txt").write_bytes(b"unexpected")
    try:
        app.verify_local(staging, pin_for(staging))
    except ValueError as error:
        assert "unexpected staged xcframework file" in str(error)
    else:
        raise AssertionError("extra extracted framework file was accepted")


def test_verify_local_authenticates_manifest_and_provenance(tmp_path):
    staging = make_staging(tmp_path)
    pin = pin_for(staging)
    (staging / "manifest.json").write_text(json.dumps({"features": ["metrics"]}))
    try:
        app.verify_local(staging, pin)
    except ValueError as error:
        assert "staged manifest" in str(error)
    else:
        raise AssertionError("modified manifest was accepted")


def test_archive_expansion_bound_is_checked_before_decompression(monkeypatch):
    monkeypatch.setattr(app, "MAX_MEMBER_BYTES", 8)
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda _archive: (_ for _ in ()).throw(AssertionError("testzip ran")))
    try:
        app.archive_members(archive({"large": b"0123456789"}), label="fixture")
    except ValueError as error:
        assert "oversized fixture member" in str(error)
    else:
        raise AssertionError("oversized archive member was accepted")



def test_product_verifier_phase_propagates_pending_pin_failure(tmp_path):
    checkout = tmp_path / "checkout"
    scripts = checkout / "ios/scripts"
    checker = checkout / "packaging/iroh-apple/check-private-apple-datapath.sh"
    scripts.mkdir(parents=True)
    checker.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, scripts / "prepare_iroh_app.py")
    product_script = scripts / "verify_iroh_app_product.sh"
    shutil.copyfile(SCRIPT.parent / "verify_iroh_app_product.sh", product_script)
    product_script.chmod(0o755)

    shutil.copyfile(
        SCRIPT.parents[2] / "packaging/iroh-apple/check-private-apple-datapath.sh",
        checker,
    )
    checker.chmod(0o755)
    (checkout / "packaging/iroh-apple/release.json").write_text(json.dumps({
        "schema_version": 1,
        "release_state": "pending",
    }))
    result = subprocess.run(
        [
            str(scripts / "verify_iroh_app_product.sh"),
            str(tmp_path / "staging"),
            str(tmp_path / "Avibe"),
            "",
            "NO",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "not published" in result.stderr

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\nexit 0\n")
    fake_python.chmod(0o755)
    fake_env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    result = subprocess.run(
        [
            str(product_script),
            str(tmp_path / "staging"),
            str(tmp_path / "Avibe"),
            str(tmp_path / "missing-debug.dylib"),
            "YES",
        ],
        capture_output=True,
        text=True,
        env=fake_env,
        check=False,
    )
    assert result.returncode != 0
    assert "Debug dylib does not exist" in result.stderr

    result = subprocess.run(
        [
            str(product_script),
            str(tmp_path / "staging"),
            str(tmp_path / "Avibe"),
            "",
            "YES",
        ],
        capture_output=True,
        text=True,
        env=fake_env,
        check=False,
    )
    assert result.returncode != 0
    assert "has no configured Debug dylib input" in result.stderr

    result = subprocess.run(
        [
            str(product_script),
            str(tmp_path / "staging"),
            str(tmp_path / "Avibe"),
            str(tmp_path / "unexpected-debug.dylib"),
            "NO",
        ],
        capture_output=True,
        text=True,
        env=fake_env,
        check=False,
    )
    assert result.returncode != 0
    assert "incompatible Debug dylib input" in result.stderr

    project = (Path(__file__).parents[1] / "ios/Avibe.xcodeproj/project.pbxproj").read_text()
    assert "name = IrohLib;" in project
    assert "PRODUCT_MODULE_NAME = IrohLib;" in project
    group = project.split("700000000000000000000008 /* IrohLib */ = {", 1)[1].split(
        "700000000000000000000003 /* Resources */ = {", 1
    )[0]
    assert "path = IrohLib;" in group
    assert "200000000000000000000130 /* IrohLib.swift */" in group
    assert "200000000000000000000131 /* Iroh.xcframework */" in group
    assert "$(SRCROOT)/../packaging/iroh-apple/release.json" in project
    assert "$(SRCROOT)/../packaging/iroh-apple/check-private-apple-datapath.sh" in project
    assert "$(TARGET_BUILD_DIR)/$(EXECUTABLE_PATH)" in project
    debug_config = project.split("C00000000000000000000003 /* Debug */ = {", 1)[1].split(
        "C00000000000000000000004 /* Release */ = {", 1
    )[0]
    release_config = project.split("C00000000000000000000004 /* Release */ = {", 1)[1].split(
        "C00000000000000000000005 /* Debug */ = {", 1
    )[0]
    assert 'ENABLE_DEBUG_DYLIB = YES;' in debug_config
    assert 'IROH_DEBUG_DYLIB_INPUT = "$(TARGET_BUILD_DIR)/$(WRAPPER_NAME)/$(EXECUTABLE_NAME).debug.dylib";' in debug_config
    assert 'IROH_DEBUG_DYLIB_PATH = "$(TARGET_BUILD_DIR)/$(WRAPPER_NAME)/$(EXECUTABLE_NAME).debug.dylib";' in debug_config
    assert "ENABLE_DEBUG_DYLIB = NO;" in release_config
    assert "IROH_DEBUG_DYLIB_INPUT = /dev/null;" in release_config
    assert 'IROH_DEBUG_DYLIB_PATH = "";' in release_config
    final_phase = project.split("D00000000000000000000003 /* Verify linked Iroh artifact */ = {", 1)[1].split(
        "/* Begin PBXSourcesBuildPhase section */", 1
    )[0]
    assert "verify_iroh_app_product.sh" in final_phase
    assert "IROH_DEBUG_DYLIB_INPUT" in final_phase
    assert "IROH_DEBUG_DYLIB_PATH" in final_phase
    assert "$ENABLE_DEBUG_DYLIB" in final_phase

    notification = project.split("500000000000000000000003 /* NotificationService */ = {", 1)[1].split(
        "500000000000000000000004 /* IrohLib */ = {", 1
    )[0]
    assert "IrohLib" not in notification
    assert "Iroh.xcframework" not in notification
    assert "NSLocalNetworkUsageDescription" not in (
        Path(__file__).parents[1] / "ios/Avibe/Resources/Info.plist"
    ).read_text()
