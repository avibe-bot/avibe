"""The App's Open-source licenses data: published notices only, verified, fail closed.

Offline fixtures shaped like `IrohLib-support.zip` (packaging/iroh-apple/release.py).
They live here only; the App never reads them.
"""
from __future__ import annotations

import hashlib
import io
import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ios" / "scripts"))
import app_licenses as licenses  # noqa: E402

TAG = "iroh-apple-v1.2.0-1"
COMMIT = "05b45b110f652d58d94c653992aeb3adc3a094f4"
UNIFFI = ["uniffi", "uniffi_bindgen", "uniffi_core"]
MIT_BODY = "Permission is hereby granted, free of charge, to any\nperson obtaining a copy of this software.\n"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def registry(name, version):
    return f"https://static.crates.io/crates/{name}/{name}-{version}.crate"


def mpl_source(name, version, upstream):
    return {"name": name, "version": version, "registry_url": registry(name, version), "repository": "x",
            "upstream_commit": COMMIT, "upstream_url": upstream, "crate_sha256": "a" * 64,
            "modification_status": "unmodified"}


def support_members():
    uniffi_upstream = f"https://github.com/mozilla/uniffi-rs/tree/{COMMIT}"
    atto_upstream = f"https://github.com/sbstp/attohttpc/tree/{'b' * 40}"
    packages, files = [], {}

    def package(name, version, license, notices, **extra):
        paths = []
        for file, text in notices.items():
            path = f"LICENSES/{name}-{version}/{file}"
            files[path] = text.encode()
            paths.append(path)
        packages.append({"name": name, "version": version, "license": license,
                         "selected_license": extra.pop("selected", license), "authors": extra.pop("authors", []),
                         "repository": None, "crate_sha256": "a" * 64, "class": extra.pop("klass", "bundled"),
                         "files": sorted(paths), "supplemental_sources": []})

    package("iroh-ffi", "1.2.0", "MIT OR Apache-2.0", {"LICENSE-MIT": "Copyright (c) n0\n\n" + MIT_BODY},
            selected="MIT")
    package("anyhow", "1.0.98", "MIT OR Apache-2.0", {"LICENSE-MIT": MIT_BODY, "LICENSE-APACHE": "Apache License\nVersion 2.0\n"},
            selected="MIT")
    package("itoa", "1.0.15", "MIT OR Apache-2.0", {"LICENSE-MIT": MIT_BODY.replace("\n", "\r\n")}, selected="MIT")
    package("bytes", "1.10.1", "MIT", {"LICENSE": "Copyright (c) 2018 Carl Lerche\n\n" + MIT_BODY})
    package("subtle", "2.6.1", "BSD-3-Clause", {"LICENSE": "Copyright (c) 2016-2017 Isis Agora Lovecruft\n\nRedistribution...\n",
                                                 "NOTICE": "Third-party notice kept.\n"})
    package("objc2", "0.6.5", "MIT", {"supplemental/MIT.txt": "MIT License\n\nCopyright (c) <year> <copyright holders>\n\n" + MIT_BODY},
            klass="standard", authors=["Mads Marquart <mads@marquart.dk>"])
    files["LICENSES/objc2-0.6.5/Cargo.toml"] = b'[package]\nname = "objc2"\n'
    packages[-1]["files"].append("LICENSES/objc2-0.6.5/Cargo.toml")
    mpl = "Mozilla Public License Version 2.0\n==================================\n\n1. Definitions\n--------------\n"
    package("attohttpc", "0.30.1", "MPL-2.0", {"LICENSE": mpl})
    for name in UNIFFI:
        package(name, "0.31.2", "MPL-2.0", {"LICENSE": mpl})
    sources = [mpl_source("attohttpc", "0.30.1", atto_upstream)] + [mpl_source(n, "0.31.2", uniffi_upstream) for n in UNIFFI]
    notice = "# Redistribution notices\n\n## MPL-2.0 covered source\n"
    for item in sources:
        notice += f"\n- {item['name']} {item['version']} (unmodified)\n  Registry source: {item['registry_url']}\n  Upstream source: {item['upstream_url']}\n"
    notice += f"\n## {licenses.BINDING_STATEMENT}\n\nGenerated using unmodified uniffi_bindgen 0.31.2 (MPL-2.0).\n"
    files.update({
        "LICENSES/packages.json": json.dumps(packages).encode(),
        "LICENSES/provenance.json": json.dumps({"packages": packages, "mpl_sources": sources,
                                                "policy_sha256": digest(licenses.POLICY.read_bytes())}).encode(),
        "LICENSES/NOTICE.md": notice.encode(),
        "IrohLib.swift": b"// generated\n",
        "LICENSES/iroh-ffi/LICENSE-MIT": ("Copyright (c) n0\n\n" + MIT_BODY).encode(),
        "LICENSES/iroh-ffi/LICENSE-APACHE": b"Apache License\nVersion 2.0\n",
        "LICENSES/rust-1.91.0/COPYRIGHT.html": b"<html><head><style>p{}</style></head><body><h1>Short version</h1>"
                                               b"<p>Rust is licensed under MIT or Apache-2.0.</p><ul><li>one</li></ul></body></html>",
        "LICENSES/rust-1.91.0/licenses/MIT.txt": MIT_BODY.encode(),
        "LICENSES/rust-1.91.0/licenses/Apache-2.0.txt": b"Apache License\nVersion 2.0\n",
    })
    return files


def archive(members, symlink=None, extra=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zip_file:
        for name, data in members.items():
            zip_file.writestr(name, data)
        for name, data in extra:
            zip_file.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zip_file.writestr(info, "../../etc/passwd")
    return buffer.getvalue()


def published_pin(data: bytes, **asset_changes):
    asset = {"url": f"https://github.com/avibe-bot/avibe/releases/download/{TAG}/{licenses.SUPPORT_NAME}",
             "sha256": digest(data), "size": len(data), **asset_changes}
    return {"schema_version": 1, "release_state": "published", "release_tag": TAG, "sha256": "c" * 64,
            "license_policy_sha256": digest(licenses.POLICY.read_bytes()),
            "notice_collector_sha256": digest(licenses.COLLECTOR.read_bytes()),
            "assets": {licenses.SUPPORT_NAME: asset}}


def test_a_realistically_large_rust_notice_fits_the_bounds(tmp_path):
    # The qualified archive's COPYRIGHT.html is about 13 MB.
    members = support_members()
    members["LICENSES/rust-1.91.0/COPYRIGHT.html"] = b"<p>Rust</p>" + b"<p>notice</p>" * 1_000_000
    pin, path = rebuilt(tmp_path, members)
    assert licenses.generate(pin, path, WEB)["groups"]


REACT_MIT = "MIT License\n\nCopyright (c) Meta Platforms, Inc. and affiliates.\n\n" + MIT_BODY
WEB = json.dumps({"schema_version": 1, "local_modules": 3, "virtual_modules": {}, "packages": [
    {"name": "react", "version": "19.3.1", "license": "MIT", "modules": 4, "files": [{"name": "LICENSE", "text": REACT_MIT}]},
    {"name": "react-dom", "version": "19.3.1", "license": "MIT", "modules": 4, "files": [{"name": "LICENSE", "text": REACT_MIT}]},
    {"name": "clsx", "version": "2.1.1", "license": "MIT", "modules": 1, "files": [{"name": "license", "text": MIT_BODY}]},
]}).encode()


def pending_pin():
    return {"schema_version": 1, "release_state": "pending", "release_tag": TAG, "sha256": None, "assets": {}}


@pytest.fixture
def published(tmp_path):
    data = archive(support_members())
    path = tmp_path / licenses.SUPPORT_NAME
    path.write_bytes(data)
    return published_pin(data), path


def generate(pin, path, web=WEB):
    return licenses.generate(pin, path, web)


def group_of(result, name, title=None):
    return [g for g in result["groups"] if any(c["name"] == name for c in g["components"])
            and (title is None or g["title"] == title)]


def test_repository_pin_is_pending_and_yields_only_the_bundled_javascript(monkeypatch):
    # The checked-in pin is pending: nothing is downloaded and nothing claims a qualified binary.
    monkeypatch.setattr(licenses, "download", lambda asset: pytest.fail("pending pin must not be consumed"))
    pin = licenses.load_pin()
    if pin["release_state"] != "pending":
        pytest.skip("pin promoted; covered by the published tests")
    result = licenses.generate(pin, None, WEB)
    assert result["native"] == {"state": "pending", "release_tag": pin["release_tag"]}
    assert result["sources"] == [] and result["components"] == 3
    assert result["web"] == {"sha256": digest(WEB), "packages": 3}
    assert {c["origin"] for g in result["groups"] for c in g["components"]} == {"web"}
    # React and react-dom share a text; clsx's has no copyright line, so it is a separate group.
    (react,) = [g for g in result["groups"] if "Meta Platforms" in g["text"]]
    assert [c["name"] for c in react["components"]] == ["react", "react-dom"] and react["title"] == "MIT"


def test_pending_pin_refuses_a_support_archive(tmp_path):
    path = tmp_path / "support.zip"
    path.write_bytes(archive(support_members()))
    with pytest.raises(ValueError, match="pending pin cannot consume"):
        licenses.generate(pending_pin(), path, WEB)


def test_groups_follow_text_identity_not_license_labels(published):
    result = generate(*published)
    mit_plain = [g for g in result["groups"] if g["text"] == MIT_BODY.strip()]
    assert len(mit_plain) == 1
    # CRLF and LF copies of the same text are one group; Rust's MIT.txt and the bundled clsx are too.
    assert {(c["name"], c["version"], c["origin"]) for c in mit_plain[0]["components"]} == {
        ("anyhow", "1.0.98", "native"), ("itoa", "1.0.15", "native"), ("Rust", "1.91.0", "native"),
        ("clsx", "2.1.1", "web")}
    # Different copyright lines stay separate groups even though both are MIT.
    bytes_group = group_of(result, "bytes")
    root_group = [g for g in result["groups"] if g["text"].startswith("Copyright (c) n0")]
    assert len(bytes_group) == 1 and "Carl Lerche" in bytes_group[0]["text"]
    assert len(root_group) == 1 and bytes_group[0] is not root_group[0]
    assert bytes_group[0]["title"] == root_group[0]["title"] == "MIT"
    # Every file of a multi-file package is retained, including a NOTICE.
    assert {g["title"] for g in group_of(result, "subtle")} == {"BSD-3-Clause", "NOTICE"}
    assert any("Third-party notice kept." in g["text"] for g in group_of(result, "subtle"))


def test_standard_texts_keep_declared_authors_and_skip_manifests(published):
    result = generate(*published)
    (group,) = group_of(result, "objc2")
    (component,) = [c for c in group["components"] if c["name"] == "objc2"]
    assert component == {"name": "objc2", "version": "0.6.5", "origin": "native",
                         "authors": ["Mads Marquart <mads@marquart.dk>"]}
    assert all("[package]" not in g["text"] for g in result["groups"])


def test_source_offer_merges_shared_upstream_and_keeps_every_crate(published):
    result = generate(*published)
    assert [(s["version"], [c["name"] for c in s["crates"]]) for s in result["sources"]] == [
        ("0.30.1", ["attohttpc"]), ("0.31.2", ["uniffi", "uniffi_bindgen", "uniffi_core"])]
    uniffi = result["sources"][1]
    assert uniffi["upstream"] == f"https://github.com/mozilla/uniffi-rs/tree/{COMMIT}"
    assert uniffi["crates"][1]["registry"] == registry("uniffi_bindgen", "0.31.2")


def test_collection_notice_binding_and_rust_texts_are_retained(published):
    result = generate(*published)
    notice = [g for g in result["groups"] if licenses.BINDING_STATEMENT in g["text"]]
    assert len(notice) == 1 and "Registry source:" in notice[0]["text"]
    (rust_copyright,) = [g for g in result["groups"] if "Short version" in g["text"]]
    assert "Rust is licensed under MIT or Apache-2.0." in rust_copyright["text"]
    assert "p{}" not in rust_copyright["text"] and "- one" in rust_copyright["text"]
    assert result["native"] == {"state": "published", "release_tag": TAG, "binary_sha256": "c" * 64,
                                "support_sha256": published[0]["assets"][licenses.SUPPORT_NAME]["sha256"],
                                "license_policy_sha256": digest(licenses.POLICY.read_bytes()),
                                "notice_collector_sha256": digest(licenses.COLLECTOR.read_bytes())}
    assert result["components"] == 14


def test_generation_is_deterministic(published):
    assert licenses.encode(generate(*published)) == licenses.encode(generate(*published))


def test_downloaded_bytes_must_match_the_pin(published, monkeypatch):
    pin, path = published
    monkeypatch.setattr(licenses, "download", lambda asset: path.read_bytes())
    assert licenses.generate(pin, None, WEB)["groups"]
    monkeypatch.setattr(licenses, "download", lambda asset: path.read_bytes() + b"x")
    with pytest.raises(ValueError, match="size does not match"):
        licenses.generate(pin, None, WEB)


@pytest.mark.parametrize("change, message", [
    ({"sha256": "0" * 64}, "checksum does not match"),
    ({"url": "https://example.com/IrohLib-support.zip"}, "not the pinned release"),
    ({"size": 0}, "size is invalid"),
])
def test_asset_must_match_the_pin(published, change, message):
    pin, path = published
    pin["assets"][licenses.SUPPORT_NAME].update(change)
    with pytest.raises(ValueError, match=message):
        licenses.generate(pin, path, WEB)


def test_policy_and_collector_must_match_this_checkout(published):
    pin, path = published
    with pytest.raises(ValueError, match="license policy"):
        licenses.generate({**pin, "license_policy_sha256": "0" * 64}, path, WEB)
    with pytest.raises(ValueError, match="notice collector"):
        licenses.generate({**pin, "notice_collector_sha256": "0" * 64}, path, WEB)


def rebuilt(tmp_path, members=None, **kwargs):
    data = archive(members if members is not None else support_members(), **kwargs)
    path = tmp_path / "rebuilt.zip"
    path.write_bytes(data)
    return published_pin(data), path


@pytest.mark.parametrize("kwargs, message", [
    ({"extra": [("LICENSES/../escape", b"x")]}, "unsafe archive member"),
    ({"extra": [("/abs/LICENSE", b"x")]}, "unsafe archive member"),
    ({"symlink": "LICENSES/link"}, "symlink"),
    ({"extra": [("LICENSES/NOTICE.md", b"again")]}, "duplicate archive member"),
    ({"extra": [("LICENSES/stray/LICENSE", b"unreviewed")]}, "unaccounted archive members"),
])
def test_archive_members_are_safe_and_accounted_for(tmp_path, kwargs, message):
    with pytest.warns(UserWarning) if "duplicate" in message else _nothing():
        pin, path = rebuilt(tmp_path, **kwargs)
    with pytest.raises(ValueError, match=message):
        licenses.generate(pin, path, WEB)


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def edited(change):
    members = support_members()
    change(members)
    return members


def drop_offer(members):
    provenance = json.loads(members["LICENSES/provenance.json"])
    provenance["mpl_sources"] = [s for s in provenance["mpl_sources"] if s["name"] != "attohttpc"]
    members["LICENSES/provenance.json"] = json.dumps(provenance).encode()


def drop_notice_url(members):
    members["LICENSES/NOTICE.md"] = members["LICENSES/NOTICE.md"].replace(b"sbstp/attohttpc", b"sbstp/other")


def drop_file(members):
    del members["LICENSES/bytes-1.10.1/LICENSE"]


def drop_binding(members):
    members["LICENSES/NOTICE.md"] = members["LICENSES/NOTICE.md"].replace(licenses.BINDING_STATEMENT.encode(), b"Binding")


def binary_notice(members):
    members["LICENSES/bytes-1.10.1/LICENSE"] = b"\xff\xfe\x00"


def drop_rust(members):
    del members["LICENSES/rust-1.91.0/COPYRIGHT.html"]


def drop_rust_licenses(members):
    for name in [n for n in members if n.startswith("LICENSES/rust-1.91.0/licenses/")]:
        del members[name]


def drop_root_license(members):
    del members["LICENSES/iroh-ffi/LICENSE-APACHE"]


def skew_inventory(members):
    packages = json.loads(members["LICENSES/packages.json"])
    members["LICENSES/packages.json"] = json.dumps(packages[1:]).encode()


def compound_mpl(members):
    for name in ("LICENSES/packages.json",):
        packages = json.loads(members[name])
        packages[1]["selected_license"] = "MIT AND MPL-2.0"
        members[name] = json.dumps(packages).encode()
    provenance = json.loads(members["LICENSES/provenance.json"])
    provenance["packages"] = json.loads(members["LICENSES/packages.json"])
    members["LICENSES/provenance.json"] = json.dumps(provenance).encode()


@pytest.mark.parametrize("change, message", [
    (compound_mpl, "compound selection"),
    (drop_offer, "MPL source offer does not match"),
    (drop_notice_url, "omits the source offer"),
    (drop_file, "missing notice file"),
    (drop_binding, "binding source statement"),
    (binary_notice, "not UTF-8"),
    (drop_rust, "Rust distribution notice"),
    (drop_rust_licenses, "Rust distribution license"),
    (drop_root_license, "missing upstream license"),
    (skew_inventory, "inventory/provenance mismatch"),
])
def test_missing_or_mismatched_content_fails(tmp_path, change, message):
    pin, path = rebuilt(tmp_path, edited(change))
    with pytest.raises(ValueError, match=message):
        licenses.generate(pin, path, WEB)


def write(tmp_path, data, web=WEB):
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    (bundle / licenses.WEB_NAME).write_bytes(web)
    (bundle / licenses.OUTPUT_NAME).write_bytes(licenses.encode(data))
    return bundle


def test_verify_binds_bundled_data_to_the_current_pin_and_bundle(tmp_path, published):
    pin, path = published
    current = generate(pin, path)
    licenses.verify(pin, write(tmp_path, current))
    pending = generate(pending_pin(), None)
    licenses.verify(pending_pin(), write(tmp_path, pending))
    # Stale published data after the pin went back to pending.
    with pytest.raises(ValueError, match="different iroh pin"):
        licenses.verify(pending_pin(), write(tmp_path, current))
    # Pending data, or data from another support asset, with a published pin.
    with pytest.raises(ValueError, match="different iroh pin"):
        licenses.verify(pin, write(tmp_path, pending))
    other = {**current, "native": {**current["native"], "support_sha256": "0" * 64}}
    with pytest.raises(ValueError, match="different iroh pin"):
        licenses.verify(pin, write(tmp_path, other))
    # Data generated before the bundle was rebuilt.
    with pytest.raises(ValueError, match="different bundle"):
        licenses.verify(pin, write(tmp_path, current, web=WEB + b" "))
    # A pin whose binding changed after generation, or that generation itself would reject.
    for change in ({"license_policy_sha256": "0" * 64}, {"notice_collector_sha256": "0" * 64}):
        with pytest.raises(ValueError):
            licenses.verify({**pin, **change}, write(tmp_path, current))
    moved = json.loads(json.dumps(pin))
    moved["assets"][licenses.SUPPORT_NAME].update(url="https://example.com/IrohLib-support.zip", size=0)
    with pytest.raises(ValueError, match="not the pinned release"):
        licenses.verify(moved, write(tmp_path, current))
    # Pending data cannot carry native notices or a source offer.
    smuggled = {**pending, "groups": current["groups"]}
    with pytest.raises(ValueError, match="while the iroh pin is pending"):
        licenses.verify(pending_pin(), write(tmp_path, smuggled))
    with pytest.raises(ValueError, match="no notices for the bundled JavaScript"):
        licenses.verify(pending_pin(), write(tmp_path, {**pending, "groups": []}))


@pytest.mark.parametrize("web, message", [
    (b"not json", "not JSON"),
    (json.dumps({"schema_version": 2}).encode(), "unknown schema"),
    (json.dumps({"schema_version": 1, "packages": []}).encode(), "no bundled packages"),
    (json.dumps({"schema_version": 1, "packages": [{"name": "x", "version": "1", "files": []}]}).encode(), "has no notice"),
])
def test_web_notices_fail_closed(web, message):
    with pytest.raises(ValueError, match=message):
        licenses.generate(pending_pin(), None, web)


def test_cli_generates_and_verifies_the_pending_repository_pin(tmp_path, capsys):
    if licenses.load_pin()["release_state"] != "pending":
        pytest.skip("pin promoted")
    assert licenses.main(["generate", "--bundle", str(tmp_path)]) == 1
    assert "npm run build:mobile" in capsys.readouterr().err
    (tmp_path / licenses.WEB_NAME).write_bytes(WEB)
    assert licenses.main(["generate", "--bundle", str(tmp_path)]) == 0
    assert json.loads((tmp_path / licenses.OUTPUT_NAME).read_text())["native"]["state"] == "pending"
    assert licenses.main(["verify", "--bundle", str(tmp_path)]) == 0
    (tmp_path / licenses.OUTPUT_NAME).unlink()
    assert licenses.main(["verify", "--bundle", str(tmp_path)]) == 1
    assert "missing licenses.json" in capsys.readouterr().err
