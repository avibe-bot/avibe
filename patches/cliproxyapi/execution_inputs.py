"""Recipe input inventory: archive-verified Go, frozen inputs, measured trusted base."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import sys
import tarfile


@contextmanager
def regular_file(path: Path):
    """Consume one admitted inode; reject observed replacement or metadata drift.

    This is not a snapshot or a lock against concurrent outside writers. Input
    preparation/content custody and the read-only execution view remain required.
    """
    def identity(info):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"Expected a single-link regular file: {path.name}")
        return tuple(getattr(info, "st_" + field) for field in (
            "dev", "ino", "mode", "uid", "gid", "rdev", "nlink", "size", "mtime_ns", "ctime_ns",
        ))

    admitted = identity(path.lstat())
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        with os.fdopen(fd, "rb", closefd=False) as stream:
            def check():
                if identity(os.fstat(fd)) != admitted or identity(path.lstat()) != admitted:
                    raise ValueError(f"Execution input changed during its read: {path.name}")
            check()
            try:
                yield stream
            finally:
                check()
    finally:
        os.close(fd)


def file_sha256(path: Path) -> str:
    with regular_file(path) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_records(root: Path, *, python_links: bool = False) -> dict:
    root = root.resolve(strict=True)
    records = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        name = path.relative_to(root).as_posix()
        if stat.S_ISREG(info.st_mode):
            record = {"kind": "file", "mode": stat.S_IMODE(info.st_mode), "sha256": file_sha256(path)}
        elif stat.S_ISDIR(info.st_mode):
            record = {"kind": "dir", "mode": stat.S_IMODE(info.st_mode)}
        elif stat.S_ISLNK(info.st_mode):
            target = path.resolve(strict=True)
            if not target.is_relative_to(root):
                # uv's system-Python venv links are explicitly a trusted base,
                # not Go archive content or an archive-verified Python claim.
                if not (python_links and name in ("bin/python", "bin/python3", "bin/python3.12")
                        and target.parent == Path("/usr/bin") and target.name.startswith("python3")):
                    raise ValueError(f"Input link escapes its tree: {name}")
            record = {"kind": "link", "target": os.readlink(path)}
        else:
            raise ValueError(f"Unsupported execution input: {name}")
        records[name] = record
    return records


def records_sha256(records: dict) -> str:
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def go_archive_records(archive: Path, expected_sha256: str) -> dict:
    """Hash and inspect the SAME opened archive; never extract unverified content."""
    with regular_file(archive) as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256:
            raise RuntimeError("Wrong pinned Go archive SHA256.")
        stream.seek(0)
        records = {}
        with tarfile.open(fileobj=stream, mode="r:gz") as source:
            for member in source:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("go",):
                    raise ValueError("Go archive path leaves its extraction root.")
                name = path.relative_to("go").as_posix()
                if name == ".":
                    if not member.isdir():
                        raise ValueError("Invalid Go archive root.")
                    continue
                if name in records:
                    raise ValueError("Duplicate Go archive member.")
                if member.isfile():
                    with source.extractfile(member) as content:
                        record = {"kind": "file", "mode": member.mode & 0o777,
                                  "sha256": hashlib.file_digest(content, "sha256").hexdigest()}
                elif member.isdir():
                    record = {"kind": "dir"}
                elif member.issym():
                    target = PurePosixPath(member.linkname)
                    if target.is_absolute() or ".." in target.parts:
                        raise ValueError("Go archive link leaves its extraction root.")
                    record = {"kind": "link", "target": member.linkname}
                else:
                    raise ValueError("Unsupported Go archive member.")
                records[name] = record
        # Tar archives may omit directory entries. Normalize structural
        # ancestors, but do not invent a claim about their archived modes.
        for name in list(records):
            for parent in PurePosixPath(name).parents:
                if parent == PurePosixPath("."):
                    continue
                previous = records.setdefault(str(parent), {"kind": "dir"})
                if previous["kind"] != "dir":
                    raise ValueError("Go archive has a non-directory ancestor.")
        return records


def verify_go(toolchain: Path, archive: Path, selected: Path, prerequisite: dict) -> dict:
    """Bind the actual compiler/tools/runtime tree BEFORE invoking any Go code."""
    toolchain = toolchain.resolve(strict=True)
    selected = selected.resolve(strict=True)
    if sys.platform != "linux" or platform.machine() not in ("aarch64", "arm64"):
        raise RuntimeError("The pinned diagnostic toolchain is Linux arm64 only.")
    if selected != toolchain / "bin/go":
        raise RuntimeError("Selected Go executable is not the pinned extraction's bin/go.")
    expected = go_archive_records(archive, prerequisite["sha256"])
    actual = tree_records(toolchain)
    for record in actual.values():
        if record["kind"] == "dir":
            record.pop("mode")
    if actual != expected:
        raise RuntimeError("Go extraction differs from its verified archive (missing/extra/changed input).")
    with regular_file(selected) as stream:
        elf = stream.read(20)
    if len(elf) != 20 or elf[:6] != b"\x7fELF\x02\x01" or int.from_bytes(elf[18:20], "little") != 183:
        raise RuntimeError("Selected compiler is not a Linux arm64 ELF executable.")
    return {
        "archive_sha256": prerequisite["sha256"],
        "extracted_tree_sha256": records_sha256(actual),
        "selected_executable": str(selected), "selected_relative_path": "bin/go",
        "selected_sha256": actual["bin/go"]["sha256"],
        "target": "linux/arm64", "version": prerequisite["version"],
    }


def python_identity(venv: Path, fixture: Path) -> dict:
    """Measure the trusted task venv; do not mislabel a version/lock as byte proof."""
    venv = venv.resolve(strict=True)
    if Path(sys.prefix).resolve() != venv or Path(sys.executable).absolute().parent != venv / "bin":
        raise RuntimeError("Verifier must use the selected task venv, not an ambient interpreter.")
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Use the existing guest Python 3.12 trusted base.")
    return {
        "trust": "existing guest Python and frozen-lock setup; measured closure, not archive-attested Python",
        "executable": sys.executable, "resolved_executable": str(Path(sys.executable).resolve(strict=True)),
        "version": sys.version, "venv_tree_sha256": records_sha256(tree_records(venv, python_links=True)),
        "lock_sha256": file_sha256(fixture / "uv.lock"),
        "pyproject_sha256": file_sha256(fixture / "pyproject.toml"),
    }
