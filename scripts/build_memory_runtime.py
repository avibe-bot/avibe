#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import platform as platform_module
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath


EVEROS_VERSION = "1.1.3"
PYTHON_VERSION = "3.12.12"
LOCK_SHA256 = "37ab1606edf1a6299a9d52b5a99d288a81218a5a0b1eb89d60644f3ace4255eb"
UV_VERSION = "0.9.18"
BIN_PATH = "bin/python"
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
EXPECTED_PLATFORMS = {
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
}
SMOKE_SCRIPT = (
    "from importlib.metadata import version\n"
    "import platform\n"
    "import everos\n"
    "import uvicorn\n"
    "from everos.entrypoints.api.app import create_app\n"
    f"assert version('everos') == '{EVEROS_VERSION}'\n"
    f"assert platform.python_version() == '{PYTHON_VERSION}'\n"
    "assert everos is not None and uvicorn is not None\n"
    "assert callable(create_app)\n"
    "print(version('everos'))\n"
    "print(platform.python_version())\n"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _platform_tag() -> str:
    raw = sysconfig.get_platform().lower()
    machine = raw.rsplit("-", 1)[-1]
    if machine == "universal2":
        machine = platform_module.machine().lower()
    if machine in {"amd64", "x86_64"}:
        arch = "x64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        raise SystemExit(f"Unsupported Memory Runtime architecture: {machine}")
    if raw.startswith("macosx"):
        system = "darwin"
    elif raw.startswith(("linux", "manylinux", "musllinux")):
        system = "linux"
    else:
        raise SystemExit(f"Unsupported Memory Runtime platform: {raw}")
    return f"{system}-{arch}"


def _validate_runtime_tree(runtime_root: Path, *, allow_internal_symlinks: bool = False) -> Path:
    runtime_root = runtime_root.resolve(strict=True)
    binary = runtime_root / BIN_PATH
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise SystemExit(f"Memory Runtime is missing executable {BIN_PATH}")
    for path in runtime_root.rglob("*"):
        if not path.is_symlink():
            continue
        try:
            target = path.resolve(strict=True)
        except OSError as exc:
            raise SystemExit(f"Memory Runtime contains a broken symlink: {path}") from exc
        if target != runtime_root and runtime_root not in target.parents:
            raise SystemExit(f"Memory Runtime symlink escapes the archive root: {path}")
        if not allow_internal_symlinks:
            raise SystemExit(f"Memory Runtime archive must not contain symlinks: {path}")
    return binary


def _normalize_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.pax_headers = {}
    return info


def create_archive(*, runtime_root: Path, output: Path, platform: str) -> dict[str, object]:
    if platform not in EXPECTED_PLATFORMS:
        raise SystemExit(f"Unsupported Memory Runtime platform: {platform}")
    binary = _validate_runtime_tree(runtime_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("wb") as raw_file:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_file, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    for child in sorted(runtime_root.iterdir(), key=lambda path: path.name):
                        archive.add(
                            child,
                            arcname=child.name,
                            recursive=True,
                            filter=_normalize_tar_info,
                        )
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    if output.stat().st_size > MAX_ARCHIVE_BYTES:
        output.unlink(missing_ok=True)
        raise SystemExit("Memory Runtime archive exceeds the 1 GiB release limit")
    return {
        "platform": platform,
        "everos_version": EVEROS_VERSION,
        "name": output.name,
        "sha256": _sha256(output),
        "binary_sha256": _sha256(binary),
        "size": output.stat().st_size,
        "bin_path": BIN_PATH,
    }


def verify_archive(archive_path: Path, *, binary_sha256: str) -> None:
    """Extract and smoke the final bytes in a new directory."""

    with tempfile.TemporaryDirectory(prefix="avibe-memory-runtime-verify-") as temporary_value:
        destination = Path(temporary_value) / "runtime"
        destination.mkdir()
        destination_resolved = destination.resolve()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                if not (member.isfile() or member.isdir()):
                    raise SystemExit(f"Memory Runtime archive member is unsupported: {member.name}")
                posix_path = PurePosixPath(member.name)
                windows_path = PureWindowsPath(member.name)
                if (
                    posix_path.is_absolute()
                    or windows_path.is_absolute()
                    or windows_path.drive
                    or ".." in posix_path.parts
                    or ".." in windows_path.parts
                ):
                    raise SystemExit(f"Memory Runtime archive path is unsafe: {member.name}")
                target = (destination / member.name).resolve()
                if target != destination_resolved and destination_resolved not in target.parents:
                    raise SystemExit(f"Memory Runtime archive path is unsafe: {member.name}")
            if sys.version_info >= (3, 12):
                archive.extractall(destination, filter="data")
            else:
                archive.extractall(destination)
        binary = _validate_runtime_tree(destination)
        if _sha256(binary) != binary_sha256:
            raise SystemExit("Memory Runtime extracted Python checksum mismatch")
        _smoke(binary, cwd=destination.parent)
        with tempfile.TemporaryDirectory(prefix="mrv-health-", dir="/tmp") as health_home:
            _sidecar_health_smoke(binary, effective_home=Path(health_home))


def prune_runtime(runtime_root: Path) -> None:
    """Remove build-path-dependent files that the owned sidecar never executes."""

    runtime_root = runtime_root.resolve(strict=True)
    bin_dir = runtime_root / "bin"
    for path in bin_dir.iterdir():
        if path.name == "python":
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    for path in runtime_root.rglob("*.pyc"):
        path.unlink()
    caches = sorted(runtime_root.rglob("__pycache__"), key=lambda path: len(path.parts), reverse=True)
    for path in caches:
        if path.is_dir():
            shutil.rmtree(path)

    for record in runtime_root.rglob("*.dist-info/RECORD"):
        site_packages = record.parent.parent
        with record.open(newline="", encoding="utf-8") as source:
            rows = list(csv.reader(source))
        retained: list[list[str]] = []
        for row in rows:
            if not row:
                continue
            relative = PurePosixPath(row[0])
            target = (site_packages / relative).resolve(strict=False)
            if target != runtime_root and runtime_root not in target.parents:
                raise SystemExit(f"Memory Runtime RECORD path escapes the archive root: {row[0]}")
            if target.exists():
                retained.append(row)
        with record.open("w", newline="", encoding="utf-8") as destination:
            writer = csv.writer(destination, lineterminator="\n")
            writer.writerows(retained)


def _run(command: list[str], *, cwd: Path, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture_output,
    )


def _smoke(python: Path, *, cwd: Path) -> None:
    result = _run([str(python), "-I", "-B", "-c", SMOKE_SCRIPT], cwd=cwd, capture_output=True)
    if result.stdout.splitlines() != [EVEROS_VERSION, PYTHON_VERSION]:
        raise SystemExit(f"Memory Runtime smoke returned unexpected identity: {result.stdout.strip()}")


def _sidecar_health_smoke(python: Path, *, effective_home: Path) -> None:
    """Launch the production child entry point and require a real UDS health probe."""

    repository = Path(__file__).resolve().parents[1]
    script = """
import asyncio
import os
import signal
import stat
import sys
from pathlib import Path
import psutil
from core.memory.everos import EverOSPort
from core.memory.process import EverOSProcess, EverOSProcessSettings

async def verify() -> None:
    process = EverOSProcess(
        python=Path(sys.argv[1]),
        effective_home=Path(sys.argv[2]),
        owner_id="runtime-build-verifier",
        settings=EverOSProcessSettings(
            llm_base_url="https://llm.invalid/v1",
            llm_model="unused",
            llm_api_key="unused",
            embedding_base_url="https://embedding.invalid/v1",
            embedding_model="unused",
            embedding_api_key="unused",
        ),
        startup_timeout_seconds=30,
        stop_timeout_seconds=10,
    )
    process._validate_launch_inputs()
    process._prepare_owned_directories()
    process._write_generated_config()
    process._remove_owned_socket()
    child = await asyncio.create_subprocess_exec(
        str(process._python),
        "-m",
        "core.memory.sidecar",
        "--uds",
        str(process.socket_path),
        "--owner-id",
        "runtime-build-verifier",
        cwd=str(process._memory_dir),
        env=process._child_environment(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        for _ in range(300):
            if child.returncode is not None or process.socket_path.exists():
                break
            await asyncio.sleep(0.1)
        if not process.socket_path.exists():
            stdout, stderr = await child.communicate()
            raise RuntimeError(
                "sidecar did not create its UDS: "
                + (stderr or stdout).decode("utf-8", errors="replace")[-4000:]
            )
        process._secure_socket()
        info = process.socket_path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("sidecar UDS is not an owner-only socket")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RuntimeError("sidecar UDS owner mismatch")
        if not await EverOSPort(process.socket_path, sidecar_timeout_seconds=5).health():
            raise RuntimeError("sidecar UDS health endpoint failed")
        inspected = [psutil.Process(child.pid), *psutil.Process(child.pid).children(recursive=True)]
        if any(
            connection.status == psutil.CONN_LISTEN
            for owned in inspected
            for connection in owned.net_connections(kind="inet")
        ):
            raise RuntimeError("sidecar opened a TCP listener")
    finally:
        if child.returncode is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(child.wait(), timeout=10)
            except asyncio.TimeoutError:
                os.killpg(child.pid, signal.SIGKILL)
                await child.wait()

asyncio.run(verify())
"""
    env = {
        "HOME": str(effective_home),
        "PATH": f"{python.parent}:/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(repository),
    }
    try:
        subprocess.run(
            [str(python), "-B", "-c", script, str(python), str(effective_home)],
            cwd=repository,
            env=env,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit("Memory Runtime sidecar UDS health verification failed") from exc


def build_runtime(
    *,
    output_dir: Path,
    lock_project: Path,
    uv_command: str = "uv",
    python_version: str = PYTHON_VERSION,
    platform: str | None = None,
) -> tuple[Path, dict[str, object]]:
    platform = platform or _platform_tag()
    if platform not in EXPECTED_PLATFORMS:
        raise SystemExit(f"Unsupported Memory Runtime platform: {platform}")
    lock_project = lock_project.resolve(strict=True)
    if not (lock_project / "uv.lock").is_file():
        raise SystemExit(f"Memory Runtime lock is missing: {lock_project / 'uv.lock'}")
    lock_sha256 = _sha256(lock_project / "uv.lock")
    if lock_sha256 != LOCK_SHA256:
        raise SystemExit("Memory Runtime lock digest does not match the pinned release input")
    if python_version != PYTHON_VERSION:
        raise SystemExit(f"Memory Runtime Python must be exactly {PYTHON_VERSION}")
    output_dir.mkdir(parents=True, exist_ok=True)
    uv_identity = _run([uv_command, "--version"], cwd=lock_project, capture_output=True).stdout.strip()
    if uv_identity.split()[:2] != ["uv", UV_VERSION]:
        raise SystemExit(f"Memory Runtime builder requires uv {UV_VERSION}")

    with tempfile.TemporaryDirectory(prefix="avibe-memory-runtime-") as temporary_value:
        temporary = Path(temporary_value)
        _run([uv_command, "python", "install", python_version], cwd=temporary)
        found = _run(
            [uv_command, "python", "find", "--no-project", "--managed-python", python_version],
            cwd=temporary,
            capture_output=True,
        )
        managed_python = Path(found.stdout.strip()).resolve(strict=True)
        managed_root = managed_python.parent.parent
        _validate_runtime_tree(managed_root, allow_internal_symlinks=True)
        runtime_root = temporary / "staging" / "runtime"
        # The shared managed-runtime extractor deliberately accepts only files
        # and directories. Dereference the already-validated internal links so
        # the resulting archive preserves that narrow extraction contract.
        shutil.copytree(managed_root, runtime_root, symlinks=False)

        requirements = temporary / "requirements.txt"
        _run(
            [
                uv_command,
                "export",
                "--project",
                str(lock_project),
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements.txt",
                "--output-file",
                str(requirements),
            ],
            cwd=temporary,
            capture_output=True,
        )
        _run(
            [
                uv_command,
                "pip",
                "sync",
                str(requirements),
                "--python",
                str(runtime_root / BIN_PATH),
                "--strict",
                "--break-system-packages",
                "--no-python-downloads",
            ],
            cwd=temporary,
        )
        _smoke(runtime_root / BIN_PATH, cwd=temporary)

        relocated_root = temporary / "relocated" / "runtime"
        relocated_root.parent.mkdir(parents=True)
        shutil.move(str(runtime_root), str(relocated_root))
        _smoke(relocated_root / BIN_PATH, cwd=temporary)
        prune_runtime(relocated_root)
        _smoke(relocated_root / BIN_PATH, cwd=temporary)

        archive = output_dir / f"memory-runtime-{EVEROS_VERSION}-{platform}.tar.gz"
        metadata = create_archive(runtime_root=relocated_root, output=archive, platform=platform)
        verify_archive(archive, binary_sha256=str(metadata["binary_sha256"]))
        metadata["python_version"] = _run(
            [str(relocated_root / BIN_PATH), "-c", "import platform; print(platform.python_version())"],
            cwd=temporary,
            capture_output=True,
        ).stdout.strip()
        metadata["lock_sha256"] = lock_sha256
        metadata["uv_version"] = UV_VERSION
    return archive, metadata


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build an Avibe-managed EverOS Memory Runtime archive.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--lock-project",
        type=Path,
        default=repository / "scripts" / "memory_poc" / "harness",
    )
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--python-version", default=PYTHON_VERSION)
    parser.add_argument("--platform", choices=sorted(EXPECTED_PLATFORMS))
    parser.add_argument("--metadata-output", type=Path)
    args = parser.parse_args()
    archive, metadata = build_runtime(
        output_dir=args.output_dir,
        lock_project=args.lock_project,
        uv_command=args.uv,
        python_version=args.python_version,
        platform=args.platform,
    )
    if args.metadata_output is not None:
        args.metadata_output.parent.mkdir(parents=True, exist_ok=True)
        args.metadata_output.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "archive": str(archive), **metadata}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
