#!/usr/bin/env python3
"""Build one target-specific, offline Avibe desktop Runtime payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


DESKTOP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DESKTOP_DIR.parent
SOURCES_PATH = DESKTOP_DIR / "runtime-sources.json"
RUNTIME_NPM_DIR = DESKTOP_DIR / "runtime-bundle"
DEFAULT_OUTPUT = DESKTOP_DIR / "src-tauri" / "resources" / "runtime"
COPY_CHUNK = 1024 * 1024
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
TREE_HASH_DOMAIN = b"avibe-runtime-tree-v1\0"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(COPY_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, cwd: Path = REPO_ROOT, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def download(source: dict[str, str], cache_dir: Path) -> Path:
    url = source["url"]
    expected = source["sha256"]
    name = Path(urllib.parse.urlparse(url).path).name
    destination = cache_dir / name
    if destination.is_file() and sha256(destination) == expected:
        return destination

    partial = destination.with_suffix(destination.suffix + ".partial")
    partial.unlink(missing_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Avibe-Desktop-Builder/1"})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as output:
        shutil.copyfileobj(response, output, COPY_CHUNK)
    if sha256(partial) != expected:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"Downloaded asset failed SHA-256 verification: {url}")
    os.replace(partial, destination)
    return destination


def extract_source(archive: Path, destination: Path) -> Path:
    before = set(destination.iterdir()) if destination.exists() else set()
    destination.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                target = (destination / member.filename).resolve()
                if destination.resolve() not in target.parents and target != destination.resolve():
                    raise SystemExit(f"Unsafe source archive entry: {member.filename}")
            source.extractall(destination)
    else:
        with tarfile.open(archive, "r:gz") as source:
            source.extractall(destination, filter="data")
    created = [path for path in destination.iterdir() if path not in before]
    if len(created) != 1 or not created[0].is_dir():
        raise SystemExit(f"Expected one top-level directory in {archive.name}")
    return created[0]


def ensure_show_runtime_manifest(sources: dict[str, Any], cache_dir: Path) -> tuple[Path, bool]:
    destination = REPO_ROOT / "vibe" / "show_runtime_manifest.json"
    if destination.is_file():
        return destination, False
    source = download(sources["show_runtime_manifest"], cache_dir)
    destination.write_bytes(source.read_bytes())
    return destination, True


def build_wheel(work_dir: Path, private_python: Path, sources: dict[str, Any], cache_dir: Path) -> Path:
    if not (REPO_ROOT / "ui" / "dist" / "index.html").is_file():
        raise SystemExit("ui/dist is missing; build the Workbench before the private Runtime")

    generated_manifest, remove_manifest = ensure_show_runtime_manifest(sources, cache_dir)
    wheel_dir = work_dir / "wheel"
    wheel_dir.mkdir()
    try:
        run(
            [
                "uv",
                "build",
                "--wheel",
                "--out-dir",
                str(wheel_dir),
                "--no-create-gitignore",
                "--python",
                str(private_python),
            ]
        )
    finally:
        if remove_manifest:
            generated_manifest.unlink(missing_ok=True)
    wheels = list(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"Expected one Avibe wheel, found {len(wheels)}")
    return wheels[0]


def install_python_environment(
    private_python: Path,
    wheel: Path,
    work_dir: Path,
    sdist_build_allowlist: list[str],
) -> str:
    requirements = work_dir / "requirements.txt"
    run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str(requirements),
            "--python",
            str(private_python),
            "--quiet",
        ]
    )
    install_command = [
        "uv",
        "pip",
        "install",
        "--python",
        str(private_python),
        "--require-hashes",
        "--only-binary=:all:",
        "--no-cache",
        "--requirements",
        str(requirements),
    ]
    for package in sdist_build_allowlist:
        install_command.extend(["--no-binary", package])
    run(install_command)
    run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(private_python),
            "--no-deps",
            "--no-cache",
            str(wheel),
        ]
    )
    completed = subprocess.run(
        [
            str(private_python),
            "-I",
            "-c",
            "from importlib.metadata import version; print(version('avibe-os'))",
        ],
        cwd=work_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def install_tools(
    target: str,
    target_config: dict[str, Any],
    node_archive: Path,
    codex_license: Path,
    payload: Path,
    work_dir: Path,
) -> None:
    node_root = extract_source(node_archive, work_dir / "node-source")
    node_source = node_root / target_config["node_source"]
    node_destination = payload / target_config["node_entrypoint"]
    node_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(node_source, node_destination)

    run(["npm", "ci", "--ignore-scripts", "--omit=dev"], cwd=RUNTIME_NPM_DIR)
    package_name = target_config["codex_package"].split("/")[-1]
    candidates = [
        RUNTIME_NPM_DIR / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / package_name,
        RUNTIME_NPM_DIR / "node_modules" / "@openai" / package_name,
    ]
    codex_package = next((candidate for candidate in candidates if candidate.is_dir()), None)
    if codex_package is None:
        raise SystemExit(f"npm did not install {target_config['codex_package']} for {target}")
    codex_name = "codex.exe" if target_config["os"] == "windows" else "codex"
    codex_source = next(codex_package.glob(f"vendor/*/bin/{codex_name}"), None)
    if codex_source is None:
        raise SystemExit(f"{target_config['codex_package']} contains no native Codex executable")
    codex_destination = payload / target_config["codex_entrypoint"]
    shutil.copy2(codex_source, codex_destination)

    licenses = payload / "licenses"
    licenses.mkdir()
    for source, name in [
        (node_root / "LICENSE", "node-LICENSE"),
        (REPO_ROOT / "LICENSE", "avibe-LICENSE"),
        (codex_license, "codex-LICENSE"),
        (codex_package / "README.md", "codex-README.md"),
    ]:
        if source.is_file():
            shutil.copy2(source, licenses / name)

    if target_config["os"] != "windows":
        for executable in [node_destination, codex_destination]:
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_inventory(private_python: Path, payload: Path) -> None:
    program = """
import importlib.metadata
import json
items = sorted(
    ({"name": item.metadata["Name"], "version": item.version} for item in importlib.metadata.distributions()),
    key=lambda item: (item["name"].lower(), item["version"]),
)
print(json.dumps({"schema_version": 1, "python_packages": items}, indent=2, sort_keys=True))
"""
    completed = subprocess.run(
        [str(private_python), "-I", "-c", program],
        check=True,
        capture_output=True,
        text=True,
    )
    (payload / "runtime-packages.json").write_text(completed.stdout, encoding="utf-8")


def create_runtime_zip(payload: Path, archive: Path) -> tuple[int, int, str]:
    unpacked_size = 0
    entry_count = 0
    tree_hasher = hashlib.sha256(TREE_HASH_DOMAIN)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as output:
        for path in sorted(item for item in payload.rglob("*") if item.is_file()):
            relative = path.relative_to(payload).as_posix()
            relative_bytes = relative.encode("utf-8")
            file_size = path.stat().st_size
            tree_hasher.update(len(relative_bytes).to_bytes(8, "big"))
            tree_hasher.update(relative_bytes)
            tree_hasher.update(file_size.to_bytes(8, "big"))
            mode = 0o755 if os.access(path, os.X_OK) else 0o644
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | mode) << 16
            with path.open("rb") as source, output.open(info, "w", force_zip64=True) as destination:
                for chunk in iter(lambda: source.read(COPY_CHUNK), b""):
                    tree_hasher.update(chunk)
                    destination.write(chunk)
            unpacked_size += file_size
            entry_count += 1
    return unpacked_size, entry_count, tree_hasher.hexdigest()


def reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def verify_payload(target_config: dict[str, Any], payload: Path, work_dir: Path) -> None:
    # Keep AVIBE_HOME short on macOS: its absolute state path is embedded in the
    # AF_UNIX dispatch address, whose platform limit is much smaller than a
    # typical CI checkout path.
    with tempfile.TemporaryDirectory(prefix="avibe-probe-") as probe_home:
        _verify_payload_with_home(target_config, payload, work_dir, Path(probe_home))


def _verify_payload_with_home(
    target_config: dict[str, Any],
    payload: Path,
    work_dir: Path,
    probe_home: Path,
) -> None:
    python = payload / target_config["python_entrypoint"]
    node = payload / target_config["node_entrypoint"]
    codex = payload / target_config["codex_entrypoint"]
    config_dir = probe_home / "config"
    config_dir.mkdir(parents=True)
    port = reserve_loopback_port()
    config_path = config_dir / "config.json"
    inherited_path = os.environ.get("PATH", "")
    env = {
        **os.environ,
        "AVIBE_HOME": str(probe_home),
        "PATH": os.pathsep.join(part for part in (str(node.parent), inherited_path) if part),
        "VIBE_SHOW_RUNTIME_NODE_BIN": str(node),
        "AVIBE_DESKTOP_MANAGED_RUNTIME": "1",
        "VIBE_INSTALL_SKIP_SHOW_RUNTIME": "1",
        "VIBE_INSTALL_SKIP_ASKILL": "1",
        "VIBE_ASKILL_AUTO_UPDATE": "0",
        "VIBE_MODEL_HUB_ENABLED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [str(python), "-I", "-m", "vibe"]
    endpoint = subprocess.run(
        [*command, "desktop", "endpoint", "--json"],
        cwd=work_dir,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    descriptor = json.loads(endpoint.stdout)
    seeded = json.loads(config_path.read_text(encoding="utf-8"))
    if (
        descriptor.get("schema_version") != 1
        or seeded.get("setup_completed") is not False
        or (seeded.get("platforms") or {}).get("enabled") != []
        or (seeded.get("platforms") or {}).get("primary") != "avibe"
    ):
        raise SystemExit("Private Runtime did not seed a fresh Workbench onboarding config")

    # Keep the seeded first-run shape intact; only move this isolated probe off
    # the product's default port so a developer build cannot collide with an
    # Avibe Runtime already running on the host.
    seeded["ui"]["setup_host"] = "127.0.0.1"
    seeded["ui"]["setup_port"] = port
    seeded["ui"]["open_browser"] = False
    config_path.write_text(json.dumps(seeded), encoding="utf-8")
    endpoint = subprocess.run(
        [*command, "desktop", "endpoint", "--json"],
        cwd=work_dir,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    descriptor = json.loads(endpoint.stdout)
    if descriptor != {"schema_version": 1, "origin": f"http://127.0.0.1:{port}"}:
        raise SystemExit("Private Runtime did not honor the isolated desktop endpoint")

    run([str(node), "--version"], cwd=work_dir, env=env)
    run([str(codex), "--version"], cwd=work_dir, env=env)

    ready_url = f"http://127.0.0.1:{port}/ready"
    stop_result: subprocess.CompletedProcess[str] | None = None
    try:
        run([*command, "start", "--no-open-browser"], cwd=work_dir, env=env)
        deadline = time.monotonic() + 90
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                request = urllib.request.Request(ready_url, headers={"Host": f"127.0.0.1:{port}"})
                with urllib.request.urlopen(request, timeout=2) as response:
                    readiness = json.loads(response.read(4097))
                if (
                    readiness.get("schema_version") == 1
                    and readiness.get("product") == "avibe"
                    and readiness.get("ready") is True
                ):
                    break
            except Exception as error:
                last_error = error
            time.sleep(0.25)
        else:
            raise SystemExit(f"Private Runtime did not become ready: {last_error}")
    finally:
        stop_result = subprocess.run(
            [*command, "stop"],
            cwd=work_dir,
            env=env,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    if stop_result.returncode != 0:
        raise SystemExit("Private Runtime failed to stop cleanly")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(ready_url, timeout=1).close()
        except Exception:
            break
        time.sleep(0.25)
    else:
        raise SystemExit("Private Runtime remained reachable after stop")


def prune_payload(payload: Path) -> None:
    for directory in sorted(payload.rglob("__pycache__"), reverse=True):
        shutil.rmtree(directory)
    for file in payload.rglob("*.pyc"):
        file.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache", type=Path, default=DESKTOP_DIR / "target" / "runtime-cache")
    args = parser.parse_args()

    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    if sources.get("schema_version") != 1:
        raise SystemExit("Unsupported runtime-sources.json schema")
    try:
        target_config = sources["targets"][args.target]
    except KeyError:
        raise SystemExit(f"Unsupported target: {args.target}") from None

    args.cache.mkdir(parents=True, exist_ok=True)
    python_archive = download(target_config["python"], args.cache)
    node_archive = download(target_config["node"], args.cache)
    codex_license = download(sources["codex_license"], args.cache)

    work_parent = DESKTOP_DIR / "target"
    work_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"runtime-{args.target}-", dir=work_parent) as temporary:
        work_dir = Path(temporary)
        payload = work_dir / "payload"
        python_root = extract_source(python_archive, payload)
        if python_root.name != "python":
            raise SystemExit(f"Unexpected Python archive root: {python_root.name}")
        private_python = payload / target_config["python_entrypoint"]
        wheel = build_wheel(work_dir, private_python, sources, args.cache)
        runtime_version = install_python_environment(
            private_python,
            wheel,
            work_dir,
            sources.get("sdist_build_allowlist", []),
        )
        install_tools(args.target, target_config, node_archive, codex_license, payload, work_dir)
        write_inventory(private_python, payload)
        verify_payload(target_config, payload, work_dir)
        prune_payload(payload)

        output_parent = args.output.parent
        output_parent.mkdir(parents=True, exist_ok=True)
        staged_output = Path(tempfile.mkdtemp(prefix=".runtime-output-", dir=output_parent))
        try:
            archive = staged_output / "runtime.zip"
            unpacked_size, entry_count, tree_sha256 = create_runtime_zip(payload, archive)
            wheel_digest = sha256(wheel)
            manifest = {
                "schema_version": 1,
                "runtime_version": runtime_version,
                "os": target_config["os"],
                "arch": target_config["arch"],
                "archive": archive.name,
                "archive_sha256": sha256(archive),
                "archive_size": archive.stat().st_size,
                "unpacked_size": unpacked_size,
                "entry_count": entry_count,
                "tree_sha256": tree_sha256,
                "python_entrypoint": target_config["python_entrypoint"],
                "node_entrypoint": target_config["node_entrypoint"],
                "codex_entrypoint": target_config["codex_entrypoint"],
                "python_distribution": target_config["python"],
                "node_distribution": target_config["node"],
                "codex_version": sources["codex_version"],
                "avibe_wheel": {"name": wheel.name, "sha256": wheel_digest},
            }
            (staged_output / "runtime-manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            args.output.mkdir(parents=True, exist_ok=True)
            for name in ["runtime.zip", "runtime-manifest.json"]:
                os.replace(staged_output / name, args.output / name)
        finally:
            if staged_output.exists():
                shutil.rmtree(staged_output)

    print(json.dumps({"ok": True, "target": args.target, "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
