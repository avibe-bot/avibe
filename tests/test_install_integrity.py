import base64
import hashlib
import subprocess

from core import install_integrity
from core.install_integrity import verify_site_packages


def _write_record(root, relative: str, content: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
    record = root / "demo_pkg-1.0.dist-info" / "RECORD"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(f"{relative},sha256={digest},{len(content)}\n", encoding="utf-8")


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


def test_verify_python_environment_import_probe_does_not_use_caller_cwd(monkeypatch, tmp_path):
    site_packages = tmp_path / "lib" / "python3.12" / "site-packages"
    _write_record(site_packages, "demo_pkg/__init__.py", b"value = 1\n")
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(install_integrity.subprocess, "run", fake_run)

    result = install_integrity.verify_python_environment(executable, required_imports=("demo_pkg",))

    assert result.ok is True
    assert calls[0]["cwd"] == install_integrity.tempfile.gettempdir()
    assert calls[0]["cwd"] != str(tmp_path)


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
