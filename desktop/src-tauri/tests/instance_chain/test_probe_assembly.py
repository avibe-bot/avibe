"""Source assembly must reach Cargo for the current native fixture contract.

Collection never enters build_native_probe.main(). Exercise its exact archive
adaptations, but stop at the build boundary: no native binary is built or run.
"""

import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

import build_native_probe


@pytest.mark.parametrize("shell_consumers", [False, True])
def test_current_native_probe_source_assembles_before_cargo(tmp_path, monkeypatch, shell_consumers):
    root = Path(__file__).resolve().parents[4]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    bundle = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        content = b'<meta name="avibe-desktop-client-version" content="1">'
        entry = tarfile.TarInfo("account/index.html")
        entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))
    arguments = [
        "build_native_probe.py", "--source", str(root), "--head", head,
        "--bundle", str(bundle), "--bundle-sha256", hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "--output", str(tmp_path / "assembly"), "--target-dir", str(tmp_path / "target"),
    ]
    if shell_consumers:
        arguments.append("--shell-consumers")
    monkeypatch.setattr(sys, "argv", arguments)
    run = subprocess.run

    class CargoReached(Exception):
        pass

    def before_build(command, **kwargs):
        if command[0] == "cargo":
            assert command[:2] == ["cargo", "build"]
            assert Path(kwargs["cwd"]).is_relative_to(tmp_path)
            raise CargoReached
        assert command[:2] in (["git", "rev-parse"], ["git", "archive"])
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", before_build)
    with pytest.raises(CargoReached):
        build_native_probe.main()
