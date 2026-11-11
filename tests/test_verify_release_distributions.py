"""The release runner must overlap installs without sharing process/home/temp state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("fail_install", [False, True])
def test_release_install_lanes_overlap_are_isolated_and_propagate_failure(tmp_path, fail_install):
    # A barrier in the three tiny fixture tests fails if the runner regresses to
    # serial execution. No package downloads or real application state involved.
    contract = tmp_path / "test_contract.py"
    contract.write_text(textwrap.dedent('''
        import json, os, time
        from pathlib import Path
        import pytest

        ROOT = Path(__file__).parent

        def test_built_wheel_and_sdist_contract():
            (ROOT / "metadata-checked").touch()

        def test_future_metadata_assertion():
            (ROOT / "future-checked").touch()

        def install(lane, tmp_path):
            assert (ROOT / "metadata-checked").exists()
            assert (ROOT / "future-checked").exists()
            home = Path.home()
            assert home.exists()
            assert not (home / "other-lane").exists()
            (home / "other-lane").touch()
            record = {"pid": os.getpid(), "home": str(home), "temp": os.environ["TMPDIR"], "pytest": str(tmp_path)}
            (ROOT / f"lane-{lane}.json").write_text(json.dumps(record))
            deadline = time.monotonic() + 10
            while len(list(ROOT.glob("lane-*.json"))) != 3:
                assert time.monotonic() < deadline, "install lanes did not overlap"
                time.sleep(0.02)
            if lane == 2 and os.environ["FAIL_INSTALL"] == "1":
                pytest.fail("fixture install failed")

        def test_installed_wheel_imports_and_cli_without_memory(tmp_path):
            install(0, tmp_path)

        def test_installed_wheel_mirrors_complete_builtin_skills_snapshot(tmp_path):
            install(1, tmp_path)

        def test_installed_sdist_mirrors_complete_builtin_skills_snapshot(tmp_path):
            install(2, tmp_path)
    '''))
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify_release_distributions.py"), str(contract)],
        cwd=tmp_path, env={**os.environ, "FAIL_INSTALL": str(int(fail_install))},
        capture_output=True, text=True, timeout=45,
    )
    assert (result.returncode != 0) is fail_install, result.stdout + result.stderr
    records = [json.loads(path.read_text()) for path in tmp_path.glob("lane-*.json")]
    assert len(records) == 3
    for key in ("pid", "home", "temp", "pytest"):
        assert len({record[key] for record in records}) == 3
    assert all(not Path(record["home"]).exists() for record in records)
    if fail_install:
        assert "fixture install failed" in result.stdout
