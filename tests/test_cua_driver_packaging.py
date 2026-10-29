from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from runpy import run_path
import shutil
import subprocess
import tarfile

import pytest

from scripts import desktop_release


_SCRIPT = run_path(
    str(Path(__file__).parents[1] / "desktop/scripts/prepare-cua-driver.py")
)


def test_managed_policy_matches_the_pinned_tool_snapshot() -> None:
    """Release preparation rejects policy drift before any driver is packaged."""

    _SCRIPT["verify_managed_policy_surface"]()


def test_patched_source_manifest_hash_locks_the_divergence() -> None:
    manifest = _SCRIPT["load_manifest"]()
    driver = manifest["driver"]
    patch = Path("desktop/cua-driver") / driver["patch"]["file"]
    snapshot = json.loads(
        (Path("desktop/cua-driver") / manifest["tool_snapshot"]).read_text()
    )

    assert (
        driver["patch"]["contract"]
        == "click.click_mode=raw; embedded parent-liveness cleanup"
    )
    assert driver["patch"]["upstream_refs"]
    assert driver["patch"]["upgrade_plan"]
    assert _SCRIPT["digest"](patch) == driver["patch"]["sha256"]
    assert snapshot["patch_sha256"] == driver["patch"]["sha256"]


def test_built_contract_reads_dump_docs_input_schema_without_stderr_contamination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_check_output(command: list[str], **kwargs: object) -> str:
        calls.append(command)
        if command[1:] == ["--version"]:
            return "cua-driver 0.31.0\n"
        assert command[1:] == ["dump-docs", "--type", "mcp"]
        output = json.dumps(
            {
                "tools": [
                    {
                        "name": "click",
                        "input_schema": {
                            "properties": {
                                "click_mode": {"enum": ["auto", "raw"]},
                            }
                        },
                    }
                ]
            }
        )
        if kwargs.get("stderr") is _SCRIPT["subprocess"].STDOUT:
            return "Cua Driver sends content-free product telemetry.\n" + output
        return output

    monkeypatch.setattr(
        _SCRIPT["subprocess"], "check_output", fake_check_output
    )
    _SCRIPT["verify_built_contract"](tmp_path / "cua-driver")

    assert calls == [
        [str(tmp_path / "cua-driver"), "--version"],
        [str(tmp_path / "cua-driver"), "dump-docs", "--type", "mcp"],
    ]


def test_built_contract_rejects_snapshot_input_schema_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = json.loads(
        Path("desktop/cua-driver/tools-v0.31.0.json").read_text()
    )

    def fake_check_output(command: list[str], **_kwargs: object) -> str:
        if command[1:] == ["--version"]:
            return "cua-driver 0.31.0\n"
        assert command[1:] == ["dump-docs", "--type", "mcp"]
        return json.dumps(snapshot)

    monkeypatch.setattr(
        _SCRIPT["subprocess"], "check_output", fake_check_output
    )

    with pytest.raises(
        ValueError,
        match="does not expose click.click_mode",
    ):
        _SCRIPT["verify_built_contract"](tmp_path / "cua-driver")


@pytest.mark.skipif(
    shutil.which("codesign") is None,
    reason="codesign exists only on macOS, where the helper is prepared; nothing to sign elsewhere",
)
def test_prepared_helper_is_a_signed_code_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An x86_64 build leaves the linker's output unsigned; the prepared helper never is."""

    built = tmp_path / "build" / "cua-driver"
    built.parent.mkdir()
    shutil.copyfile("/usr/bin/true", built)
    built.chmod(0o755)
    subprocess.run(["codesign", "--remove-signature", str(built)], check=True)
    unsigned = subprocess.run(
        ["codesign", "-dv", str(built)], capture_output=True, text=True
    )
    assert "not signed at all" in unsigned.stderr

    contract_checked: list[str] = []

    def verify_contract(binary: Path) -> None:
        subprocess.run(["codesign", "--verify", "--strict", str(binary)], check=True)
        contract_checked.append(str(binary))

    script = _SCRIPT["prepare"].__globals__
    patch = Path("desktop/cua-driver") / _SCRIPT["load_manifest"]()["driver"]["patch"]["file"]
    monkeypatch.setitem(script, "extract_driver_source", lambda *_args: tmp_path / "source")
    monkeypatch.setitem(script, "apply_driver_patch", lambda *_args: patch.resolve())
    monkeypatch.setitem(script, "build_driver", lambda *_args: built)
    monkeypatch.setitem(script, "verify_built_contract", verify_contract)

    output = _SCRIPT["prepare"](
        "x86_64-apple-darwin",
        source_archive=tmp_path / "source.tar.gz",
        output_dir=tmp_path / "binaries",
    )

    assert contract_checked == [str(built)]
    subprocess.run(["codesign", "--verify", "--strict", str(output)], check=True)
    details = subprocess.run(
        ["codesign", "-dv", str(output)], capture_output=True, text=True, check=True
    )
    assert "Signature=adhoc" in details.stderr
    provenance = json.loads(
        output.with_name(f"{output.name}.provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["prepared_binary_sha256"] == _SCRIPT["digest"](output)
    desktop_release.driver_provenance(
        "x86_64-apple-darwin",
        output,
        output.with_name(f"{output.name}.provenance.json"),
    )


@pytest.mark.parametrize(
    "case",
    ["match", "binary-changed", "other-target", "other-patch", "contract-fails", "missing-provenance"],
)
def test_restored_helper_is_reused_only_when_it_passes_a_fresh_builds_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    """A cached helper ships only as the exact pinned build; anything else is rebuilt."""

    target = "x86_64-apple-darwin"
    manifest = _SCRIPT["load_manifest"]()
    patch = Path("desktop/cua-driver") / manifest["driver"]["patch"]["file"]
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    restored = binaries / f"cua-driver-{target}"
    restored.write_bytes(b"restored helper")
    provenance = _SCRIPT["provenance_record"](
        manifest,
        "aarch64-apple-darwin" if case == "other-target" else target,
        patch_sha256="0" * 64 if case == "other-patch" else _SCRIPT["digest"](patch),
        binary_sha256=_SCRIPT["digest"](restored),
    )
    if case != "missing-provenance":
        restored.with_name(f"{restored.name}.provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        )
    if case == "binary-changed":
        restored.write_bytes(b"tampered helper")

    built = tmp_path / "build" / "cua-driver"
    built.parent.mkdir()
    built.write_bytes(b"rebuilt helper")
    checked: list[tuple[str, bytes]] = []
    builds: list[str] = []

    def check(name: str):
        def run(binary: Path, *_args: object) -> None:
            checked.append((name, binary.read_bytes()))
            if case == "contract-fails" and name == "contract" and binary == restored:
                raise ValueError("patched Cua Driver reports an unexpected version")

        return run

    script = _SCRIPT["prepare"].__globals__
    monkeypatch.setitem(script, "verify_architecture", check("architecture"))
    monkeypatch.setitem(script, "verify_code_signature", check("signature"))
    monkeypatch.setitem(script, "verify_built_contract", check("contract"))
    monkeypatch.setitem(script, "sign_adhoc", check("sign"))
    monkeypatch.setitem(script, "extract_driver_source", lambda *_args: tmp_path / "source")
    monkeypatch.setitem(script, "apply_driver_patch", lambda *_args: patch.resolve())

    def build(*_args: object) -> Path:
        builds.append(target)
        return built

    monkeypatch.setitem(script, "build_driver", build)

    output = _SCRIPT["prepare"](
        target,
        source_archive=tmp_path / "source.tar.gz",
        output_dir=binaries,
        reuse=True,
    )

    assert output == restored.resolve()
    if case == "match":
        assert builds == []
        assert output.read_bytes() == b"restored helper"
        assert checked == [
            ("architecture", b"restored helper"),
            ("signature", b"restored helper"),
            ("contract", b"restored helper"),
        ]
    else:
        assert builds == [target]
        assert output.read_bytes() == b"rebuilt helper"
        assert ("contract", b"rebuilt helper") in checked
    # The release verifier accepts whichever helper was kept.
    desktop_release.driver_provenance(
        target,
        output,
        output.with_name(f"{output.name}.provenance.json"),
    )


def test_source_extraction_is_scoped_to_the_driver_subtree(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    root = "cua-fixture"
    with tarfile.open(archive, "w:gz") as output:
        for name, body in (
            (f"{root}/libs/cua-driver/rust/Cargo.toml", b"[workspace]\n"),
            (f"{root}/libs/cua-driver/rust/src/lib.rs", b"pub fn fixture() {}\n"),
            (f"{root}/owner-data.txt", b"must not be extracted\n"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(body)
            member.mode = 0o644
            output.addfile(member, io.BytesIO(body))
    source_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {
        "driver": {
            "source": {
                "root": root,
                "sha256": source_sha256,
            }
        }
    }

    extracted = _SCRIPT["extract_driver_source"](
        manifest,
        archive,
        tmp_path / "extracted",
    )

    assert (
        extracted / "libs/cua-driver/rust/Cargo.toml"
    ).read_text() == "[workspace]\n"
    assert not (extracted / "owner-data.txt").exists()


def test_source_extraction_rejects_non_regular_driver_members(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    root = "cua-fixture"
    with tarfile.open(archive, "w:gz") as output:
        cargo = tarfile.TarInfo(f"{root}/libs/cua-driver/rust/Cargo.toml")
        cargo.size = 0
        output.addfile(cargo, io.BytesIO())
        link = tarfile.TarInfo(f"{root}/libs/cua-driver/rust/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp/escape"
        output.addfile(link)
    manifest = {
        "driver": {
            "source": {
                "root": root,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
        }
    }

    with pytest.raises(ValueError, match="non-regular"):
        _SCRIPT["extract_driver_source"](
            manifest,
            archive,
            tmp_path / "extracted",
        )
