from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from runpy import run_path
import tarfile

import pytest


_SCRIPT = run_path(
    str(Path(__file__).parents[1] / "desktop/scripts/prepare-cua-driver.py")
)


def test_managed_policy_matches_the_pinned_28_tool_snapshot() -> None:
    """Release preparation rejects policy drift before any driver is packaged."""

    _SCRIPT["verify_managed_policy_surface"]()


def test_patched_source_manifest_hash_locks_the_divergence() -> None:
    manifest = _SCRIPT["load_manifest"]()
    driver = manifest["driver"]
    patch = Path("desktop/cua-driver") / driver["patch"]["file"]
    snapshot = json.loads(
        (Path("desktop/cua-driver") / manifest["tool_snapshot"]).read_text()
    )

    assert driver["patch"]["contract"] == "click.click_mode=raw"
    assert driver["patch"]["upstream_refs"]
    assert driver["patch"]["upgrade_plan"]
    assert _SCRIPT["digest"](patch) == driver["patch"]["sha256"]
    assert snapshot["patch_sha256"] == driver["patch"]["sha256"]


def test_built_contract_reads_mcp_input_schema_and_raw_click_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_check_output(command: list[str], **_: object) -> str:
        calls.append(command)
        if command[1:] == ["--version"]:
            return "cua-driver 0.31.0\n"
        assert command[1:] == ["dump-docs", "--type", "mcp"]
        return json.dumps(
            {
                "tools": [
                    {
                        "name": "click",
                        "inputSchema": {
                            "properties": {
                                "click_mode": {"enum": ["auto", "raw"]},
                            }
                        },
                    }
                ]
            }
        )

    monkeypatch.setattr(
        _SCRIPT["subprocess"], "check_output", fake_check_output
    )
    _SCRIPT["verify_built_contract"](tmp_path / "cua-driver")

    assert calls == [
        [str(tmp_path / "cua-driver"), "--version"],
        [str(tmp_path / "cua-driver"), "dump-docs", "--type", "mcp"],
    ]


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
