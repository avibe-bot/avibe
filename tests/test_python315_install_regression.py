"""Safety/marker contracts for the real 3.15 install acceptance driver.

The live CI acceptance owns package resolution. These tests protect host isolation
and keep its independent marker-verification child executable on every platform.
"""
from pathlib import Path
import os
import sys

from scripts import python315_install_regression as acceptance


def test_acceptance_isolates_old_cli_launcher_and_package_manager_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "owner-bin"))
    for key in ("AVIBE_UPGRADE_PACKAGE_SPEC", "VIBE_HOME", "UV_CONSTRAINT", "UV_PYTHON",
                "PIP_CONSTRAINT", "PYTHONPATH", "VIRTUAL_ENV"):
        monkeypatch.setenv(key, "must-not-leak")
    root = tmp_path / "fixture"
    uv = str(tmp_path / "uv-bin" / "uv")
    env = acceptance.isolated_env(root, uv)
    for key in ("AVIBE_UPGRADE_PACKAGE_SPEC", "VIBE_HOME", "UV_CONSTRAINT", "UV_PYTHON",
                "PIP_CONSTRAINT", "PYTHONPATH", "VIRTUAL_ENV"):
        assert key not in env
    assert env["PATH"].split(os.pathsep)[:3] == [str(root / "bin"), str(Path(sys.executable).parent), str(Path(uv).parent)]
    for key in ("HOME", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_PYTHON_INSTALL_DIR", "AVIBE_HOME"):
        assert Path(env[key]).is_relative_to(root)
    assert env["UV_PYTHON_DOWNLOADS"] == "never"


def test_constraint_verifier_child_is_valid_python():
    compile(acceptance.VERIFY, "<target-interpreter-verifier>", "exec")


def test_active_interpreter_follows_current_launcher_not_retained_old_tool(tmp_path):
    name = "vibe.exe" if os.name == "nt" else "vibe"
    python_name = "python.exe" if os.name == "nt" else "python"
    stable = tmp_path / "bin" / name
    stable.parent.mkdir()
    stable.write_bytes(b"candidate launcher with candidate interpreter path")
    for directory, payload in (("old", b"old launcher"), ("new", stable.read_bytes())):
        bin_dir = tmp_path / directory / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / name).write_bytes(payload)
        (bin_dir / python_name).write_bytes(b"interpreter fixture")
    assert acceptance.active_python(tmp_path) == tmp_path / "new" / "bin" / python_name


def test_deferred_wait_does_not_execute_or_open_the_busy_launcher(tmp_path, monkeypatch):
    stable = acceptance.launcher(tmp_path)
    stable.parent.mkdir()
    marker = stable.parent / f".{stable.name}.avibe-generation"
    generation = tmp_path / "generation"
    generation.mkdir()
    original_read = Path.read_text

    def read_marker(path, *args, **kwargs):
        assert path == marker  # Reading the destination itself can block replace on Windows.
        return original_read(path, *args, **kwargs)

    def publish(_):
        marker.write_text(str(generation), encoding="utf-8")

    def no_execution(*args, **kwargs):
        raise AssertionError("Must not start the destination executable before activation")

    monkeypatch.setattr(Path, "read_text", read_marker)
    monkeypatch.setattr(acceptance.time, "sleep", publish)
    monkeypatch.setattr(acceptance, "run", no_execution)
    acceptance.wait_for_activation(tmp_path)
