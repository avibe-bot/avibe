"""Archive/extraction/selected-executable consumers; no real compiler is run."""

import hashlib
import io
import os
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

import execution_inputs


@pytest.mark.parametrize("kind", ["directory", "fifo", "symlink", "hardlink"])
def test_regular_file_rejects_unadmitted_types_without_opening(tmp_path, monkeypatch, kind):
    target = tmp_path / "input"
    if kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        original = tmp_path / "original"
        original.write_bytes(b"task-only input")
        if kind == "symlink":
            target.symlink_to(original)
        else:
            os.link(original, target)
    monkeypatch.setattr(os, "open", lambda *_a, **_kw: pytest.fail("Rejected input must not be opened."))
    with pytest.raises(ValueError, match="single-link regular"):
        execution_inputs.file_sha256(target)


@pytest.mark.parametrize("when", ["admission", "completion"])
@pytest.mark.parametrize("mutation", ["replacement", "symlink", "fifo", "hardlink", "mode", "content"])
def test_regular_file_binds_actual_descriptor_and_closes_on_observed_drift(tmp_path, monkeypatch, when, mutation):
    target = tmp_path / "input"
    target.write_bytes(b"original task-only bytes")
    target.chmod(0o644)
    original_open, original_fstat = os.open, os.fstat
    opened = []

    def mutate():
        if mutation in ("replacement", "symlink", "fifo"):
            preserved = tmp_path / "preserved"
            target.rename(preserved)
            if mutation == "replacement":
                target.write_bytes(preserved.read_bytes())
            elif mutation == "symlink":
                target.symlink_to(preserved)
            else:
                os.mkfifo(target)
        elif mutation == "hardlink":
            os.link(target, tmp_path / "writable-alias")
        elif mutation == "mode":
            target.chmod(0o600)
        else:
            target.write_bytes(b"modified task-only bytes")

    def opening(path, flags, *args, **kwargs):
        assert Path(path) == target
        assert all(flags & flag for flag in (os.O_NOFOLLOW, os.O_NONBLOCK, os.O_CLOEXEC))
        if when == "admission":
            mutate()
        fd = original_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", opening)
    with pytest.raises((ValueError, OSError)):
        with execution_inputs.regular_file(target) as stream:
            assert when == "completion", "Admission drift reached the consuming body."
            assert stream.read() == b"original task-only bytes"
            mutate()
    for fd in opened:
        with pytest.raises(OSError):
            original_fstat(fd)


@pytest.mark.parametrize("failure", ["fdopen", "consumer", "none"])
def test_regular_file_closes_partial_and_successful_acquisitions(tmp_path, monkeypatch, failure):
    target = tmp_path / "input"
    target.write_bytes(b"stable task-only bytes")
    original_open, original_fstat = os.open, os.fstat
    opened = []

    def opening(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", opening)
    if failure == "fdopen":
        def refused(*_args, **_kwargs):
            raise RuntimeError("finite fdopen failure")
        monkeypatch.setattr(os, "fdopen", refused)
    if failure == "none":
        assert execution_inputs.file_sha256(target) == hashlib.sha256(b"stable task-only bytes").hexdigest()
    else:
        with pytest.raises(RuntimeError, match="finite"):
            with execution_inputs.regular_file(target) as stream:
                assert stream.read() == b"stable task-only bytes"
                raise RuntimeError("finite consumer failure")
    assert len(opened) == 1
    with pytest.raises(OSError):
        original_fstat(opened[0])


def fake_go(tmp_path, monkeypatch):
    root = tmp_path / "go"
    (root / "bin").mkdir(parents=True)
    (root / "pkg/tool/linux_arm64").mkdir(parents=True)
    (root / "src/runtime").mkdir(parents=True)
    elf = b"\x7fELF\x02\x01" + bytes(12) + (183).to_bytes(2, "little") + b"fake-only"
    for name, content in (
        ("bin/go", elf), ("pkg/tool/linux_arm64/compile", b"fake-compiler"),
        ("src/runtime/runtime.go", b"package runtime\n"),
    ):
        path = root / name
        path.write_bytes(content)
        path.chmod(0o755 if name != "src/runtime/runtime.go" else 0o644)
    archive = tmp_path / "go.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                # Exercise official-style archives with implicit directories.
                output.add(path, arcname="go/" + path.relative_to(root).as_posix())
    prerequisite = {"version": "go1.26.4", "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(execution_inputs.platform, "machine", lambda: "aarch64")
    return root, archive, prerequisite


def test_actual_go_tree_matches_one_verified_archive(tmp_path, monkeypatch):
    root, archive, pin = fake_go(tmp_path, monkeypatch)
    identity = execution_inputs.verify_go(root, archive, root / "bin/go", pin)
    assert identity["archive_sha256"] == pin["sha256"]
    assert identity["selected_relative_path"] == "bin/go"
    assert identity["selected_sha256"] == hashlib.sha256((root / "bin/go").read_bytes()).hexdigest()
    another = tmp_path / "another-path"
    shutil.copytree(root, another)
    second = execution_inputs.verify_go(another, archive, another / "bin/go", pin)
    assert second["extracted_tree_sha256"] == identity["extracted_tree_sha256"]
    assert second["selected_executable"] != identity["selected_executable"]


@pytest.mark.parametrize("mutation", [
    "compiler", "runtime", "tool", "extra", "extra-directory", "missing", "mode", "escape", "hardlink",
])
def test_valid_archive_does_not_authorize_changed_extraction(tmp_path, monkeypatch, mutation):
    root, archive, pin = fake_go(tmp_path, monkeypatch)
    target = root / "src/runtime/runtime.go"
    if mutation in ("compiler", "runtime", "tool"):
        name = {"compiler": "bin/go", "runtime": "src/runtime/runtime.go",
                "tool": "pkg/tool/linux_arm64/compile"}[mutation]
        (root / name).write_bytes(b"substitute claiming go1.26.4")
    elif mutation == "extra":
        (root / "extra").write_bytes(b"extra")
    elif mutation == "extra-directory":
        (root / "extra-dir").mkdir()
    elif mutation == "missing":
        target.unlink()
    elif mutation == "mode":
        (root / "bin/go").chmod(0o644)
    elif mutation == "escape":
        target.unlink()
        target.symlink_to(archive)
    else:
        os.link(target, tmp_path / "alias")
    with pytest.raises((RuntimeError, ValueError)):
        execution_inputs.verify_go(root, archive, root / "bin/go", pin)


@pytest.mark.parametrize("mutation", ["selected", "archive", "architecture"])
def test_go_selection_archive_and_host_are_independent_checks(tmp_path, monkeypatch, mutation):
    root, archive, pin = fake_go(tmp_path, monkeypatch)
    selected = root / "bin/go"
    if mutation == "selected":
        selected = tmp_path / "same-version-go"
        shutil.copyfile(root / "bin/go", selected)
    elif mutation == "archive":
        archive.write_bytes(b"another archive")
    else:
        monkeypatch.setattr(execution_inputs.platform, "machine", lambda: "x86_64")
    with pytest.raises(RuntimeError):
        execution_inputs.verify_go(root, archive, selected, pin)


def test_wrong_elf_architecture_refuses_even_with_a_matching_test_archive(tmp_path, monkeypatch):
    root, archive, pin = fake_go(tmp_path, monkeypatch)
    wrong = bytearray((root / "bin/go").read_bytes())
    wrong[18:20] = (62).to_bytes(2, "little")
    (root / "bin/go").write_bytes(wrong)
    with tarfile.open(archive, "w:gz") as output:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                output.add(path, arcname="go/" + path.relative_to(root).as_posix())
    pin["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="ELF"):
        execution_inputs.verify_go(root, archive, root / "bin/go", pin)


@pytest.mark.parametrize("name", ["/go/escape", "go/../escape", "../escape", "wrong-root/file"])
def test_bad_archive_members_refuse_without_extraction(tmp_path, name):
    archive = tmp_path / "archive.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo(name)
        member.size = 1
        output.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        execution_inputs.go_archive_records(archive, execution_inputs.file_sha256(archive))
    assert list(tmp_path.iterdir()) == [archive]


def test_python_receipt_measures_venv_and_lock_without_archive_claim(tmp_path, monkeypatch):
    venv, fixture = tmp_path / "venv", tmp_path / "fixture"
    (venv / "bin").mkdir(parents=True)
    fixture.mkdir()
    (venv / "bin/python").write_bytes(b"fake trusted Python, never executed")
    (fixture / "uv.lock").write_bytes(b"frozen lock")
    (fixture / "pyproject.toml").write_bytes(b"frozen project")
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "executable", str(venv / "bin/python"))
    monkeypatch.setattr(sys, "version_info", (3, 12, 3))
    before = execution_inputs.python_identity(venv, fixture)
    assert "not archive-attested" in before["trust"]
    (venv / "package.py").write_bytes(b"mutated")
    after = execution_inputs.python_identity(venv, fixture)
    assert before["venv_tree_sha256"] != after["venv_tree_sha256"]
    assert before["lock_sha256"] == after["lock_sha256"]
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "ambient"))
    with pytest.raises(RuntimeError, match="task venv"):
        execution_inputs.python_identity(venv, fixture)
