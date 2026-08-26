import base64
import hashlib
import json
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

from config.platform_registry import platform_descriptors
from core import install_integrity
from core.install_integrity import verify_site_packages


def _write_record(root, relative: str, content: bytes, *, distribution: str = "demo_pkg") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
    record = root / f"{distribution}-1.0.dist-info" / "RECORD"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(f"{relative},sha256={digest},{len(content)}\n", encoding="utf-8")


def _runtime_probe_output(*records: Path) -> str:
    return f"{install_integrity.RUNTIME_PROBE_PREFIX}{json.dumps([str(record) for record in records])}\n"


def test_verify_site_packages_accepts_complete_record(tmp_path):
    _write_record(tmp_path, "demo_pkg/__init__.py", b"value = 1\n")

    result = verify_site_packages(tmp_path)

    assert result.ok is True
    assert result.checked_files == 1


def test_verify_site_packages_rejects_a_missing_file_with_valid_metadata(tmp_path):
    _write_record(tmp_path, "demo_pkg/__init__.py", b"value = 1\n")
    (tmp_path / "demo_pkg" / "__init__.py").unlink()

    result = verify_site_packages(tmp_path)

    assert result.ok is False
    assert "missing demo_pkg/__init__.py" in result.failures


def test_verify_site_packages_rejects_a_changed_file(tmp_path):
    _write_record(tmp_path, "demo_pkg/__init__.py", b"value = 1\n")
    (tmp_path / "demo_pkg" / "__init__.py").write_bytes(b"value = 2\n")

    result = verify_site_packages(tmp_path)

    assert result.ok is False
    assert "hash mismatch demo_pkg/__init__.py" in result.failures


def test_verify_python_environment_probes_use_private_empty_directories(monkeypatch, tmp_path):
    site_packages = tmp_path / "lib" / "python3.12" / "site-packages"
    _write_record(site_packages, "demo_pkg/__init__.py", b"value = 1\n")
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    calls = []

    def fake_run(*args, **kwargs):
        cwd = Path(kwargs["cwd"])
        calls.append(
            {
                **kwargs,
                "cwd_parent": cwd.parent,
                "cwd_mode": stat.S_IMODE(cwd.stat().st_mode),
                "cwd_entries": tuple(cwd.iterdir()),
            }
        )
        stdout = (
            f"{site_packages}\n"
            if len(calls) == 1
            else _runtime_probe_output(site_packages / "demo_pkg-1.0.dist-info" / "RECORD")
        )
        return subprocess.CompletedProcess(args[0], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(install_integrity.subprocess, "run", fake_run)

    result = install_integrity.verify_python_environment(executable, required_imports=("demo_pkg",))

    assert result.ok is True
    assert len(calls) == 2
    assert all(call["cwd_parent"] == Path(install_integrity.tempfile.gettempdir()) for call in calls)
    assert all(call["cwd_mode"] & 0o077 == 0 for call in calls)
    assert all(call["cwd_entries"] == () for call in calls)
    assert calls[0]["cwd"] != calls[1]["cwd"]


def test_candidate_probe_does_not_inherit_pythonpath(monkeypatch, tmp_path):
    site_packages = tmp_path / "lib" / "python3.12" / "site-packages"
    _write_record(site_packages, "demo_pkg/__init__.py", b"value = 1\n")
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return subprocess.CompletedProcess(args[0], 0, stdout=f"{site_packages}\n", stderr="")
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=_runtime_probe_output(site_packages / "demo_pkg-1.0.dist-info" / "RECORD"),
            stderr="",
        )

    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setattr(install_integrity.subprocess, "run", fake_run)

    result = install_integrity.verify_python_environment(executable, required_imports=("demo_pkg",))

    assert result.ok is True
    assert "PYTHONPATH" not in calls[0]["env"]
    assert "PYTHONPATH" not in calls[1]["env"]


def test_site_discovery_ignores_reported_prefix_without_record(monkeypatch, tmp_path):
    site_packages = tmp_path / "Lib" / "site-packages"
    _write_record(site_packages, "demo_pkg/__init__.py", b"value = 1\n")
    executable = tmp_path / "Scripts" / "python.exe"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=f"{tmp_path}\n{site_packages}\n",
            stderr="",
        )

    monkeypatch.setattr(install_integrity.subprocess, "run", fake_run)

    assert install_integrity.site_packages_for_python(executable) == [site_packages.resolve()]


def test_site_discovery_probe_respects_disabled_user_site(monkeypatch, tmp_path):
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0][2])
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(install_integrity.subprocess, "run", fake_run)

    install_integrity.site_packages_for_python(executable)

    assert "ENABLE_USER_SITE" in calls[0]


def test_site_discovery_ignores_other_python_versions_in_the_same_prefix(monkeypatch, tmp_path):
    selected = tmp_path / "lib" / "python3.12" / "site-packages"
    sibling = tmp_path / "lib" / "python3.11" / "site-packages"
    _write_record(selected, "demo_pkg/__init__.py", b"value = 1\n")
    _write_record(sibling, "demo_pkg/__init__.py", b"value = 1\n")
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        install_integrity.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=f"{selected}\n", stderr=""),
    )

    assert install_integrity.site_packages_for_python(executable) == [selected.resolve()]


def test_verify_site_packages_accepts_dist_packages_entry_point(tmp_path):
    site_packages = tmp_path / "lib" / "python3.12" / "dist-packages"
    entrypoint = tmp_path / "bin" / "vibe"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_bytes(b"#!/bin/sh\n")
    digest = base64.urlsafe_b64encode(hashlib.sha256(entrypoint.read_bytes()).digest()).decode().rstrip("=")
    record = site_packages / "demo_pkg-1.0.dist-info" / "RECORD"
    record.parent.mkdir(parents=True)
    record.write_text(f"../../../bin/vibe,sha256={digest},{entrypoint.stat().st_size}\n", encoding="utf-8")

    result = verify_site_packages(site_packages)

    assert result.ok is True
    assert result.checked_files == 1


def test_runtime_imports_cover_every_registered_platform_client():
    expected = {descriptor.client_module for descriptor in platform_descriptors()}

    assert expected <= set(install_integrity.runtime_import_modules())


def test_dependency_graph_rejects_a_wholly_missing_declared_distribution(monkeypatch):
    distributions = {
        "avibe-os": SimpleNamespace(
            metadata={"Name": "avibe-os"},
            version="1.0",
            requires=("slack-sdk>=3.26", "discord.py>=2.4"),
            files=(Path("avibe_os-1.0.dist-info/RECORD"),),
            locate_file=lambda path: Path("/site-packages") / path,
        ),
        "slack-sdk": SimpleNamespace(
            metadata={"Name": "slack-sdk"},
            version="3.30",
            requires=(),
            files=(Path("slack_sdk-3.30.dist-info/RECORD"),),
            locate_file=lambda path: Path("/site-packages") / path,
        ),
    }

    def distribution(name):
        try:
            return distributions[name]
        except KeyError as exc:
            raise install_integrity.importlib_metadata.PackageNotFoundError(name) from exc

    monkeypatch.setattr(install_integrity.importlib_metadata, "distribution", distribution)

    graph = install_integrity.inspect_dependency_graph()

    assert graph.distributions == ("avibe-os", "slack-sdk")
    assert graph.records == (
        "/site-packages/avibe_os-1.0.dist-info/RECORD",
        "/site-packages/slack_sdk-3.30.dist-info/RECORD",
    )
    assert graph.failures == ("missing dependency: discord.py",)


def test_verify_python_environment_ignores_unrelated_distributions(monkeypatch, tmp_path):
    site_packages = tmp_path / "lib" / "python3.12" / "site-packages"
    _write_record(site_packages, "demo_pkg/__init__.py", b"value = 1\n")
    unrelated = site_packages / "unrelated-1.0.dist-info" / "RECORD"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("unrelated/missing.py,,\n", encoding="utf-8")
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        stdout = (
            f"{site_packages}\n"
            if calls == 1
            else _runtime_probe_output(site_packages / "demo_pkg-1.0.dist-info" / "RECORD")
        )
        return subprocess.CompletedProcess(args[0], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(install_integrity.subprocess, "run", fake_run)

    result = install_integrity.verify_python_environment(executable, required_imports=("demo_pkg",))

    assert result.ok is True
    assert result.checked_files == 1
