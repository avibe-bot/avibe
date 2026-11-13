from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
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
    staging.mkdir()
    binding = b"// generated wrapper\n"
    (staging / "IrohLib.xcframework.zip").write_bytes(archive(BINARY_MEMBERS))
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
        "release_tag": "iroh-apple-v1.2.0-1",
        "binary_sha256": hashlib.sha256((staging / "IrohLib.xcframework.zip").read_bytes()).hexdigest(),
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


def test_xcode_module_boundary_keeps_iroh_out_of_the_notification_extension():
    project = (Path(__file__).parents[1] / "ios/Avibe.xcodeproj/project.pbxproj").read_text()
    assert "name = IrohLib;" in project
    assert "PRODUCT_MODULE_NAME = IrohLib;" in project
    assert "200000000000000000000131 /* Iroh.xcframework */" in project
    notification = project.split("500000000000000000000003 /* NotificationService */ = {", 1)[1].split(
        "500000000000000000000004 /* IrohLib */ = {", 1
    )[0]
    assert "IrohLib" not in notification
    assert "Iroh.xcframework" not in notification
    assert "NSLocalNetworkUsageDescription" not in (
        Path(__file__).parents[1] / "ios/Avibe/Resources/Info.plist"
    ).read_text()
