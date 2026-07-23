from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from core import managed_runtime
from core.managed_runtime import ManagedRuntimeManager, ManagedRuntimeSpec


class FixtureRuntimeManager(ManagedRuntimeManager):
    def _binary_version(self, binary: Path | None) -> str | None:
        if binary is None:
            return None
        return binary.read_text(encoding="utf-8").strip()


@pytest.mark.parametrize(
    ("include_direct_platform", "expected_platform"),
    [(False, "linux-amd64"), (True, "linux-x64")],
)
def test_list_asset_manifest_prefers_direct_platform_then_falls_back_to_alias(
    tmp_path: Path,
    monkeypatch,
    include_direct_platform: bool,
    expected_platform: str,
) -> None:
    archive = tmp_path / "fixture-linux_amd64.tar.gz"
    binary_payload = b"v1\n"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("fixture")
        member.mode = 0o755
        member.size = len(binary_payload)
        tar.addfile(member, io.BytesIO(binary_payload))

    assets = [
        {
            "platform": "linux-amd64",
            "url": archive.as_uri(),
            "size_bytes": archive.stat().st_size,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "binary_sha256": hashlib.sha256(binary_payload).hexdigest(),
        }
    ]
    if include_direct_platform:
        assets.append({**assets[0], "platform": "linux-x64"})
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "v1",
                "source": "example/fixture",
                "assets": assets,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: "linux-x64")
    manager = FixtureRuntimeManager(
        spec=ManagedRuntimeSpec(
            runtime_id="fixture",
            manifest_resource="unused.json",
            version_field="version",
            default_bin_path="fixture",
            archives_field="assets",
            archive_size_field="size_bytes",
            platform_aliases=(("linux-x64", "linux-amd64"),),
        ),
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest,
    )

    result = manager.ensure()

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["platform"] == expected_platform
    assert Path(result["path"]).read_bytes() == binary_payload
    assert manager.ensure()["changed"] is False
    assert manager.status()["installed"] is True
