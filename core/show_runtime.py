from __future__ import annotations

import atexit
import asyncio
import hashlib
import importlib.resources as package_resources
import json
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from sysconfig import get_platform
from typing import Any

import httpx

from config import paths
from core.show_pages import SHOW_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS
from core.process_isolation import KILL_SIGNAL, isolated_subprocess_kwargs, signal_process_tree


logger = logging.getLogger(__name__)
_RUNTIME_BIN = "avibe-show-runtime"
_RUNTIME_PACKAGE = "@avibe/show-runtime"
_RUNTIME_ARCHIVE_PREFIX = "vibe-show-runtime-node"
_RUNTIME_ARCHIVE_RELEASE_BASE_URL = "https://github.com/avibe-bot/vibe-show-runtime/releases/latest/download"
_RUNTIME_GITHUB_REPO = "https://github.com/avibe-bot/vibe-show-runtime.git"
_RUNTIME_GITHUB_REF = "main"
_RUNTIME_SOURCE_MANIFEST = "manifest-cache"
_RUNTIME_SOURCE_ARCHIVE = "archive"
_RUNTIME_SOURCE_GITHUB = "github"
_RUNTIME_SOURCE_NPM = "npm"
_RUNTIME_MANIFEST_RESOURCE = "show_runtime_manifest.json"
_FALSE_VALUES = {"0", "false", "no", "off"}
_PREWARM_IMPORT_RE = re.compile(r"""(?P<quote>["'])(?P<path>[^"']+)(?P=quote)""")
_PREWARM_MAX_ASSETS = 64
_PREWARM_MAX_DEPTH = 4


@dataclass(frozen=True)
class ShowRuntimeResult:
    available: bool
    base_url: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ShowRuntimeArchive:
    platform: str
    name: str
    url: str
    sha256: str
    size: int | None = None


@dataclass(frozen=True)
class ShowRuntimeManifest:
    schema_version: int
    runtime_version: str
    minimum_node: str | None
    archives: dict[str, ShowRuntimeArchive]
    digest: str
    source: str


class ShowRuntimeManager:
    def __init__(
        self,
        *,
        command: str | None = None,
        workspace_root: Path | None = None,
        runtime_dir: Path | None = None,
        auto_install: bool | None = None,
        package_spec: str | None = None,
        runtime_source: str | None = None,
        archive_path: Path | str | None = None,
        archive_url: str | None = None,
        github_repo: str | None = None,
        github_ref: str | None = None,
        manifest_path: Path | str | None = None,
        manifest_url: str | None = None,
        offline: bool | None = None,
        force_install: bool = False,
    ) -> None:
        configured_command = command or os.environ.get("VIBE_SHOW_RUNTIME_BIN")
        self.command = configured_command or _RUNTIME_BIN
        self._command_explicit = configured_command is not None
        self.workspace_root = workspace_root or paths.get_show_pages_dir()
        self.runtime_dir = runtime_dir or paths.get_runtime_dir() / "show-runtime"
        archive_path_value = archive_path or os.environ.get("VIBE_SHOW_RUNTIME_ARCHIVE_PATH")
        self.archive_path = Path(archive_path_value).expanduser() if archive_path_value else None
        archive_url_env = os.environ.get("VIBE_SHOW_RUNTIME_ARCHIVE_URL")
        manifest_path_value = manifest_path or os.environ.get("VIBE_SHOW_RUNTIME_MANIFEST_PATH")
        self.manifest_path = Path(manifest_path_value).expanduser() if manifest_path_value else None
        self.manifest_url = manifest_url if manifest_url is not None else os.environ.get("VIBE_SHOW_RUNTIME_MANIFEST_URL")
        source_value = runtime_source or os.environ.get("VIBE_SHOW_RUNTIME_SOURCE")
        if source_value is None and (archive_path_value or archive_url is not None or archive_url_env):
            source_value = _RUNTIME_SOURCE_ARCHIVE
        if (
            source_value is None
            and not self.manifest_path
            and not self.manifest_url
            and not _packaged_runtime_manifest_exists()
        ):
            source_value = _RUNTIME_SOURCE_ARCHIVE
        self.auto_install = _auto_install_enabled() if auto_install is None else auto_install
        self.package_spec = package_spec or os.environ.get("VIBE_SHOW_RUNTIME_PACKAGE_SPEC") or _RUNTIME_PACKAGE
        self.runtime_source = _normalize_runtime_source(source_value)
        self.archive_url = archive_url if archive_url is not None else os.environ.get(
            "VIBE_SHOW_RUNTIME_ARCHIVE_URL",
            _default_runtime_archive_url(),
        )
        self.github_repo = github_repo or os.environ.get("VIBE_SHOW_RUNTIME_GITHUB_REPO") or _RUNTIME_GITHUB_REPO
        self.github_ref = github_ref or os.environ.get("VIBE_SHOW_RUNTIME_GITHUB_REF") or _RUNTIME_GITHUB_REF
        self.offline = _env_flag_enabled("VIBE_SHOW_RUNTIME_OFFLINE", default=False) if offline is None else offline
        self.force_install = force_install
        self.stdout_path = self.runtime_dir / "stdout.log"
        self.stderr_path = self.runtime_dir / "stderr.log"
        self.install_log_path = self.runtime_dir / "install.log"
        self.cache_root = self.runtime_dir / "vite-cache"
        self._install_attempted = False
        self._install_reason: str | None = None
        self._managed_command: list[str] | None = None
        self._process: subprocess.Popen[str] | None = None
        self._base_url: str | None = None
        self._lock = asyncio.Lock()

    async def ensure(self) -> ShowRuntimeResult:
        if self._base_url and await self._healthy(self._base_url):
            return ShowRuntimeResult(True, self._base_url)
        async with self._lock:
            if self._base_url and await self._healthy(self._base_url):
                return ShowRuntimeResult(True, self._base_url)
            self.stop()
            command = _resolve_command(self.command) if self._command_explicit else None
            if not command:
                command = await self._resolve_managed_command()
            if not command:
                return ShowRuntimeResult(False, reason=self._install_reason or "runtime_command_missing")
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            self.cache_root.mkdir(parents=True, exist_ok=True)
            # Reap any orphaned runtime server still bound to this workspace root before
            # spawning ours, so there is a single writer (avibe#813). self.stop() above
            # already released our own tracked child; anything left is a stray from a
            # prior avibe instance that died without reaping it (SIGKILL / crash). Run it
            # off the event loop: the psutil scan + terminate/kill can block for seconds.
            await asyncio.to_thread(self._sweep_orphan_runtime_servers)
            with self.stdout_path.open("w", encoding="utf-8") as stdout, self.stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr:
                self._process = subprocess.Popen(
                    [
                        *command,
                        "--workspace-root",
                        str(self.workspace_root),
                        "--cache-root",
                        str(self.cache_root),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "0",
                        "--fallback-delay-seconds",
                        str(SHOW_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS),
                    ],
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    **isolated_subprocess_kwargs(),
                )
            base_url = await self._read_startup_url()
            if not base_url:
                self.stop()
                return ShowRuntimeResult(False, reason="runtime_start_failed")
            self._base_url = base_url
            return ShowRuntimeResult(True, base_url)

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> httpx.Response:
        ready = await self.ensure()
        if not ready.available or not ready.base_url:
            raise RuntimeError(ready.reason or "show runtime unavailable")
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            return await client.request(method, f"{ready.base_url}{path}", headers=headers, content=body)

    async def prewarm_session(self, session_id: str, *, base_path: str | None = None) -> ShowRuntimeResult:
        session_part = urllib.parse.quote(session_id, safe="")
        runtime_path = f"/sessions/{session_part}/app/"
        headers = {"x-vibe-show-base": base_path} if base_path else None
        try:
            response = await self.request("GET", runtime_path, headers=headers)
            if response.status_code >= 500:
                return ShowRuntimeResult(False, reason=f"session_prewarm_failed:{response.status_code}")
            result = await self._prewarm_session_module_graph(
                session_id,
                runtime_path=runtime_path,
                headers=headers,
                seed_responses=[(runtime_path, response)],
                base_path=base_path,
            )
            if not result.available:
                return result
            return ShowRuntimeResult(True, self._base_url)
        except Exception as exc:
            return ShowRuntimeResult(False, reason=f"session_prewarm_failed:{exc}")

    async def _prewarm_session_module_graph(
        self,
        session_id: str,
        *,
        runtime_path: str,
        headers: dict[str, str] | None,
        seed_responses: list[tuple[str, httpx.Response]],
        base_path: str | None,
    ) -> ShowRuntimeResult:
        pending: list[tuple[str, int]] = [(f"{runtime_path}src/main.tsx", 0)]
        visited: set[str] = {path for path, _response in seed_responses}
        for path, response in seed_responses:
            pending.extend(
                (import_path, 1)
                for import_path in _show_runtime_prewarm_import_paths(
                    response,
                    session_id=session_id,
                    runtime_path=runtime_path,
                    base_path=base_path,
                )
            )

        while pending and len(visited) < _PREWARM_MAX_ASSETS:
            path, depth = pending.pop(0)
            if path in visited or depth > _PREWARM_MAX_DEPTH:
                continue
            visited.add(path)
            response = await self.request("GET", path, headers=headers)
            if response.status_code >= 500:
                return ShowRuntimeResult(False, reason=f"session_prewarm_module_failed:{response.status_code}:{path}")
            if response.status_code >= 400:
                continue
            if depth >= _PREWARM_MAX_DEPTH:
                continue
            for import_path in _show_runtime_prewarm_import_paths(
                response,
                session_id=session_id,
                runtime_path=runtime_path,
                base_path=base_path,
            ):
                if import_path not in visited:
                    pending.append((import_path, depth + 1))
        return ShowRuntimeResult(True, self._base_url)

    async def websocket_url(self, path: str) -> str:
        ready = await self.ensure()
        if not ready.available or not ready.base_url:
            raise RuntimeError(ready.reason or "show runtime unavailable")
        return f"{ready.base_url.replace('http://', 'ws://', 1).replace('https://', 'wss://', 1)}{path}"

    async def _healthy(self, base_url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=0.5)) as client:
                response = await client.get(f"{base_url}/health")
            return response.status_code == 200
        except Exception:
            return False

    async def _read_startup_url(self) -> str | None:
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            if self._process and self._process.poll() is not None:
                return None
            try:
                text = self.stdout_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                text = ""
            for line in reversed(text.splitlines()):
                marker = "Vibe Show Runtime listening at "
                if marker in line:
                    return line.split(marker, 1)[1].strip()
            await asyncio.sleep(0.05)
        return None

    def stop(self) -> None:
        process = self._process
        self._process = None
        self._base_url = None
        if not process or process.poll() is not None:
            return
        signal_process_tree(process, signal.SIGTERM, logger, "show runtime")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            signal_process_tree(process, KILL_SIGNAL, logger, "show runtime")

    def _sweep_orphan_runtime_servers(self) -> None:
        """Best-effort reap of stray runtime servers bound to our workspace root."""
        keep_pid = self._process.pid if self._process else None
        try:
            sweep_orphan_show_runtime_servers(self.workspace_root, keep_pid=keep_pid)
        except Exception:  # pragma: no cover - defensive; sweeping must never block spawn
            logger.debug("Orphan show runtime sweep skipped", exc_info=True)

    async def _resolve_managed_command(self) -> list[str] | None:
        if self._command_explicit and self.command != _RUNTIME_BIN:
            self._install_reason = "runtime_command_missing"
            return None
        if self.runtime_source == _RUNTIME_SOURCE_MANIFEST:
            command = None if self.force_install else self._installed_manifest_runtime_command()
            if command:
                self._managed_command = command
                return command
            if self.auto_install and not self._install_attempted:
                self._install_attempted = True
                command = await asyncio.to_thread(self._install_managed_runtime)
                if command:
                    self._managed_command = command
                    return command
            command = self._installed_manifest_runtime_command()
            if command:
                self._managed_command = command
                return command
            if self._managed_command:
                return self._managed_command
            return None
        if self.runtime_source == _RUNTIME_SOURCE_ARCHIVE:
            if self.auto_install and not self._install_attempted:
                self._install_attempted = True
                command = await asyncio.to_thread(self._install_managed_runtime)
                if command:
                    self._managed_command = command
                    return command
            command = self._installed_archive_runtime_command()
            if command:
                self._managed_command = command
                return command
            if self._managed_command:
                return self._managed_command
        else:
            managed = self._managed_bin_path()
            resolved = _resolve_executable_path(managed)
            if resolved:
                return [resolved]
            if self._managed_command:
                return self._managed_command
        if self.runtime_source == _RUNTIME_SOURCE_GITHUB:
            command = self._installed_github_runtime_command()
            if command:
                self._managed_command = command
                return command
        if not self.auto_install:
            self._install_reason = "runtime_command_missing"
            return None
        if self._install_attempted:
            return None
        self._install_attempted = True
        command = await asyncio.to_thread(self._install_managed_runtime)
        if command:
            self._managed_command = command
        return command

    def _install_managed_runtime(self) -> list[str] | None:
        if self.runtime_source == _RUNTIME_SOURCE_MANIFEST:
            return self._install_manifest_runtime()
        if self.runtime_source == _RUNTIME_SOURCE_ARCHIVE:
            return self._install_archive_runtime()
        if self.runtime_source == _RUNTIME_SOURCE_GITHUB:
            return self._install_github_runtime()
        if self.runtime_source == _RUNTIME_SOURCE_NPM:
            return self._install_npm_runtime()
        self._install_reason = "runtime_source_unsupported"
        return None

    def status(self) -> dict[str, Any]:
        configured_command = _resolve_command(self.command) if self._command_explicit else None
        manifest = self._load_runtime_manifest() if self.runtime_source == _RUNTIME_SOURCE_MANIFEST else None
        platform_tag = _runtime_platform_tag()
        node = _resolve_node_command()
        node_version = _node_version(node) if node else None
        node_supported = _node_satisfies_requirement(node_version, manifest.minimum_node) if manifest else None
        installed_command: list[str] | None = configured_command
        installed_dir: Path | None = None
        archive: ShowRuntimeArchive | None = None
        installed_matches = False
        if not configured_command and manifest:
            archive = manifest.archives.get(platform_tag)
            if archive:
                installed_dir = self._manifest_install_dir(manifest, archive)
                installed_matches = self._manifest_install_matches(installed_dir, manifest, archive)
                if installed_matches and node and node_supported is not False:
                    installed_command = self._manifest_runtime_command(installed_dir, node)
        elif not configured_command and self.runtime_source == _RUNTIME_SOURCE_ARCHIVE:
            installed_dir = self._archive_install_dir()
            installed_command = self._archive_runtime_command(installed_dir, node or ["node"])
        elif not configured_command and self.runtime_source == _RUNTIME_SOURCE_GITHUB:
            installed_dir = self._github_source_dir()
            installed_command = self._github_runtime_command(installed_dir, node or ["node"])
        elif not configured_command and self.runtime_source == _RUNTIME_SOURCE_NPM:
            managed = _resolve_executable_path(self._managed_bin_path())
            installed_command = [managed] if managed else None
        return {
            "provider": self.runtime_source,
            "platform": platform_tag,
            "explicit_command": self.command if self._command_explicit else None,
            "node_available": node is not None,
            "node_version": _format_semver(node_version),
            "node_supported": node_supported,
            "manifest": _manifest_status_payload(manifest),
            "archive": _archive_status_payload(archive),
            "installed": installed_command is not None,
            "installed_matches_manifest": installed_matches,
            "install_dir": str(installed_dir) if installed_dir else None,
            "command": installed_command,
            "reason": self._install_reason,
        }

    def clean(self, *, keep_previous: int = 1) -> dict[str, Any]:
        removed: list[str] = []
        for pattern in ("prebuilt-*", "manifest-*"):
            for path in self.runtime_dir.glob(pattern):
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    removed.append(str(path))
        versions_dir = self.runtime_dir / "versions"
        if versions_dir.is_dir():
            current_install_dir: Path | None = None
            try:
                pointer = json.loads((self.runtime_dir / "current.json").read_text(encoding="utf-8"))
                pointer_install_dir = Path(str(pointer.get("install_dir") or "")).resolve()
                if versions_dir.resolve() in pointer_install_dir.parents:
                    current_install_dir = pointer_install_dir
            except Exception:
                current_install_dir = None
            install_dirs = {
                path.parent
                for pattern in ("*/*/.vibe-show-runtime.json", "*/*/*/.vibe-show-runtime.json")
                for path in versions_dir.glob(pattern)
                if path.parent.is_dir()
            }
            sorted_install_dirs = sorted(install_dirs, key=lambda path: path.stat().st_mtime, reverse=True)
            kept_previous = 0
            for path in sorted_install_dirs:
                path_resolved = path.resolve()
                if current_install_dir is not None and (
                    path_resolved == current_install_dir or path_resolved in current_install_dir.parents
                ):
                    continue
                if kept_previous < keep_previous:
                    kept_previous += 1
                    continue
                shutil.rmtree(path, ignore_errors=True)
                removed.append(str(path))
            for path in sorted(versions_dir.glob("*/*"), reverse=True):
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            for path in sorted(versions_dir.iterdir(), reverse=True):
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
        return {"ok": True, "removed": removed}

    def prepare(self, *, force: bool | None = None, offline: bool | None = None) -> dict[str, Any]:
        previous_force = self.force_install
        previous_offline = self.offline
        if force is not None:
            self.force_install = force
        if offline is not None:
            self.offline = offline
        try:
            if self._command_explicit:
                command = _resolve_command(self.command)
                self._install_reason = None if command else "runtime_command_missing"
            else:
                command = self._install_managed_runtime()
            return {
                "ok": command is not None,
                "provider": self.runtime_source,
                "platform": _runtime_platform_tag(),
                "command": command,
                "reason": None if command else self._install_reason,
                "status": self.status(),
            }
        finally:
            self.force_install = previous_force
            self.offline = previous_offline

    def _installed_manifest_runtime_command(self) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            return None
        manifest = self._load_runtime_manifest()
        if not manifest:
            return None
        if not self._manifest_node_supported(node, manifest):
            return None
        archive = self._manifest_archive_for_platform(manifest)
        if not archive:
            return None
        install_dir = self._manifest_install_dir(manifest, archive)
        command = self._verified_manifest_runtime_command(install_dir, manifest, archive, node)
        if command:
            return command
        return self._verified_manifest_runtime_command(self._legacy_manifest_install_dir(manifest, archive), manifest, archive, node)

    def _install_manifest_runtime(self) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            self._install_reason = "runtime_node_missing"
            return None
        manifest = self._load_runtime_manifest()
        if not manifest:
            return None
        if not self._manifest_node_supported(node, manifest):
            return None
        archive = self._manifest_archive_for_platform(manifest)
        if not archive:
            return None
        install_dir = self._manifest_install_dir(manifest, archive)
        verified_existing_command = self._verified_manifest_runtime_command(install_dir, manifest, archive, node)
        if not verified_existing_command:
            legacy_install_dir = self._legacy_manifest_install_dir(manifest, archive)
            verified_existing_command = self._verified_manifest_runtime_command(legacy_install_dir, manifest, archive, node)
        if verified_existing_command and not self.force_install:
            self._install_reason = None
            return verified_existing_command
        archive_path = self._resolve_manifest_archive(archive)
        if not archive_path:
            return self._reuse_existing_archive_runtime(verified_existing_command)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix="manifest-", dir=self.runtime_dir))
        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                _safe_extract_tar(tar, tmp_dir)
            command = self._manifest_runtime_command(tmp_dir, node)
            if not command:
                self._install_reason = "runtime_install_missing_bin"
                return self._reuse_existing_archive_runtime(verified_existing_command)
            if install_dir.exists():
                shutil.rmtree(install_dir)
            install_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_dir), str(install_dir))
            self._write_manifest_install_metadata(install_dir, manifest, archive)
            self._write_current_manifest_pointer(manifest, archive, install_dir)
            self._install_reason = None
            return self._manifest_runtime_command(install_dir, node)
        except Exception:
            logger.exception("Failed to install manifest Show Runtime")
            self._install_reason = "runtime_install_failed"
            return self._reuse_existing_archive_runtime(verified_existing_command)
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _load_runtime_manifest(self) -> ShowRuntimeManifest | None:
        payload: bytes | None = None
        source = ""
        if self.manifest_path:
            if not self.manifest_path.exists():
                self._install_reason = "runtime_manifest_missing"
                return None
            payload = self.manifest_path.read_bytes()
            source = str(self.manifest_path)
        elif self.manifest_url:
            if self.offline:
                self._install_reason = "runtime_manifest_unavailable_offline"
                return None
            try:
                with urllib.request.urlopen(self.manifest_url, timeout=30) as response:
                    payload = response.read()
                source = self.manifest_url
            except Exception:
                logger.exception("Failed to download Show Runtime manifest from %s", self.manifest_url)
                self._install_reason = "runtime_manifest_download_failed"
                return None
        else:
            try:
                resource = package_resources.files("vibe").joinpath(_RUNTIME_MANIFEST_RESOURCE)
            except Exception:
                resource = None
            if resource is None or not resource.is_file():
                self._install_reason = "runtime_manifest_missing"
                return None
            payload = resource.read_bytes()
            source = f"package:{_RUNTIME_MANIFEST_RESOURCE}"
        digest = hashlib.sha256(payload).hexdigest()
        try:
            data = json.loads(payload.decode("utf-8"))
            archives = {
                platform_tag: ShowRuntimeArchive(
                    platform=platform_tag,
                    name=str(item["name"]),
                    url=str(item["url"]),
                    sha256=str(item["sha256"]),
                    size=int(item["size"]) if item.get("size") is not None else None,
                )
                for platform_tag, item in (data.get("archives") or {}).items()
                if isinstance(item, dict)
            }
            manifest = ShowRuntimeManifest(
                schema_version=int(data.get("schema_version")),
                runtime_version=str(data.get("runtime_version") or ""),
                minimum_node=str(data.get("minimum_node") or "") or None,
                archives=archives,
                digest=digest,
                source=source,
            )
        except Exception:
            self._install_reason = "runtime_manifest_invalid"
            return None
        if manifest.schema_version != 1 or not manifest.runtime_version or not manifest.archives:
            self._install_reason = "runtime_manifest_invalid"
            return None
        return manifest

    def _manifest_node_supported(self, node: list[str], manifest: ShowRuntimeManifest) -> bool:
        if not manifest.minimum_node:
            return True
        version = _node_version(node)
        if _node_satisfies_requirement(version, manifest.minimum_node):
            return True
        self._install_reason = "runtime_node_unsupported"
        return False

    def _manifest_archive_for_platform(self, manifest: ShowRuntimeManifest) -> ShowRuntimeArchive | None:
        platform_tag = _runtime_platform_tag()
        archive = manifest.archives.get(platform_tag)
        if not archive:
            self._install_reason = "runtime_platform_unsupported"
            return None
        return archive

    def _resolve_manifest_archive(self, archive: ShowRuntimeArchive) -> Path | None:
        cached = self.runtime_dir / "downloads" / f"{archive.sha256}.tgz"
        if cached.exists() and self._downloaded_archive_matches(cached, archive):
            return cached
        if self.offline:
            self._install_reason = "runtime_archive_unavailable_offline"
            return None
        parsed = urllib.parse.urlparse(archive.url)
        if parsed.scheme not in {"https", "file"}:
            self._install_reason = "runtime_archive_url_unsupported"
            return None
        tmp_path = cached.with_suffix(".tmp")
        cached.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(archive.url, timeout=60) as response, tmp_path.open("wb") as destination:
                shutil.copyfileobj(response, destination)
            if not self._downloaded_archive_matches(tmp_path, archive):
                tmp_path.unlink(missing_ok=True)
                return None
            tmp_path.replace(cached)
            return cached
        except Exception:
            logger.exception("Failed to download Show Runtime archive from %s", archive.url)
            tmp_path.unlink(missing_ok=True)
            self._install_reason = "runtime_archive_download_failed"
            return None

    def _downloaded_archive_matches(self, path: Path, archive: ShowRuntimeArchive) -> bool:
        if archive.size is not None and path.stat().st_size != archive.size:
            self._install_reason = "runtime_archive_size_mismatch"
            return False
        if _file_sha256(path) != archive.sha256:
            self._install_reason = "runtime_archive_checksum_mismatch"
            return False
        return True

    def _manifest_install_dir(self, manifest: ShowRuntimeManifest, archive: ShowRuntimeArchive) -> Path:
        fingerprint = hashlib.sha256(f"{manifest.digest}:{archive.sha256}".encode("utf-8")).hexdigest()[:16]
        return (
            self.runtime_dir
            / "versions"
            / _safe_path_part(manifest.runtime_version)
            / _safe_path_part(archive.platform)
            / fingerprint
        )

    def _legacy_manifest_install_dir(self, manifest: ShowRuntimeManifest, archive: ShowRuntimeArchive) -> Path:
        return self.runtime_dir / "versions" / _safe_path_part(manifest.runtime_version) / _safe_path_part(archive.platform)

    def _manifest_metadata_path(self, install_dir: Path) -> Path:
        return install_dir / ".vibe-show-runtime.json"

    def _verified_manifest_runtime_command(
        self,
        install_dir: Path,
        manifest: ShowRuntimeManifest,
        archive: ShowRuntimeArchive,
        node: list[str],
    ) -> list[str] | None:
        command = self._manifest_runtime_command(install_dir, node)
        if command and self._manifest_install_matches(install_dir, manifest, archive):
            return command
        return None

    def _manifest_install_matches(self, install_dir: Path, manifest: ShowRuntimeManifest, archive: ShowRuntimeArchive) -> bool:
        try:
            payload = json.loads(self._manifest_metadata_path(install_dir).read_text(encoding="utf-8"))
        except Exception:
            return False
        return (
            payload.get("provider") == _RUNTIME_SOURCE_MANIFEST
            and payload.get("manifest_sha256") == manifest.digest
            and payload.get("runtime_version") == manifest.runtime_version
            and payload.get("platform") == archive.platform
            and payload.get("archive_sha256") == archive.sha256
        )

    def _write_manifest_install_metadata(self, install_dir: Path, manifest: ShowRuntimeManifest, archive: ShowRuntimeArchive) -> None:
        self._manifest_metadata_path(install_dir).write_text(
            json.dumps(
                {
                    "provider": _RUNTIME_SOURCE_MANIFEST,
                    "manifest_sha256": manifest.digest,
                    "runtime_version": manifest.runtime_version,
                    "platform": archive.platform,
                    "archive_name": archive.name,
                    "archive_sha256": archive.sha256,
                    "manifest_source": manifest.source,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_current_manifest_pointer(self, manifest: ShowRuntimeManifest, archive: ShowRuntimeArchive, install_dir: Path) -> None:
        pointer = self.runtime_dir / "current.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(
            json.dumps(
                {
                    "provider": _RUNTIME_SOURCE_MANIFEST,
                    "runtime_version": manifest.runtime_version,
                    "platform": archive.platform,
                    "install_dir": str(install_dir),
                    "manifest_sha256": manifest.digest,
                    "archive_sha256": archive.sha256,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _manifest_runtime_command(self, install_dir: Path, node: list[str]) -> list[str] | None:
        return self._archive_runtime_command(install_dir, node)

    def _installed_archive_runtime_command(self) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            return None
        return self._archive_runtime_command(self._archive_install_dir(), node)

    def _install_archive_runtime(self) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            self._install_reason = "runtime_node_missing"
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        install_dir = self._archive_install_dir()
        existing_command = self._archive_runtime_command(install_dir, node)
        archive = self._resolve_prebuilt_archive()
        if not archive:
            return self._reuse_existing_archive_runtime(existing_command)
        archive_digest = _file_sha256(archive)
        if existing_command and self._archive_manifest_matches(archive_digest):
            self._install_reason = None
            return existing_command
        tmp_dir = Path(tempfile.mkdtemp(prefix="prebuilt-", dir=self.runtime_dir))
        try:
            with tarfile.open(archive, "r:gz") as tar:
                _safe_extract_tar(tar, tmp_dir)
            command = self._archive_runtime_command(tmp_dir, node)
            if not command:
                self._install_reason = "runtime_install_missing_bin"
                return self._reuse_existing_archive_runtime(existing_command)
            if install_dir.exists():
                shutil.rmtree(install_dir)
            install_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_dir), str(install_dir))
            self._write_archive_manifest(archive_digest)
            self._install_reason = None
            return self._archive_runtime_command(install_dir, node)
        except Exception:
            logger.exception("Failed to install prebuilt Show Runtime")
            self._install_reason = "runtime_install_failed"
            return self._reuse_existing_archive_runtime(existing_command)
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _resolve_prebuilt_archive(self) -> Path | None:
        if self.archive_path:
            if self.archive_path.exists():
                return self.archive_path
            self._install_reason = "runtime_archive_missing"
            return None
        packaged = self._copy_packaged_runtime_archive()
        if packaged:
            return packaged
        if not self.archive_url:
            self._install_reason = "runtime_archive_missing"
            return None
        if self.offline:
            self._install_reason = "runtime_archive_unavailable_offline"
            return None
        return self._download_runtime_archive(self.archive_url)

    def _copy_packaged_runtime_archive(self) -> Path | None:
        try:
            resource = package_resources.files("vibe").joinpath("show_runtime", _runtime_archive_name())
        except Exception:
            return None
        if not resource.is_file():
            return None
        target = self.runtime_dir / "downloads" / _runtime_archive_name()
        target.parent.mkdir(parents=True, exist_ok=True)
        with resource.open("rb") as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)
        return target

    def _download_runtime_archive(self, archive_url: str) -> Path | None:
        target = self.runtime_dir / "downloads" / _runtime_archive_name()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(archive_url, timeout=60) as response, target.open("wb") as destination:
                shutil.copyfileobj(response, destination)
        except Exception:
            logger.exception("Failed to download prebuilt Show Runtime from %s", archive_url)
            self._install_reason = "runtime_archive_download_failed"
            return None
        return target

    def _archive_install_dir(self) -> Path:
        return self.runtime_dir / "prebuilt" / "current"

    def _archive_manifest_path(self) -> Path:
        return self._archive_install_dir() / ".vibe-show-runtime.json"

    def _archive_manifest_matches(self, archive_digest: str) -> bool:
        try:
            payload = json.loads(self._archive_manifest_path().read_text(encoding="utf-8"))
        except Exception:
            return False
        return payload.get("archive_name") == _runtime_archive_name() and payload.get("sha256") == archive_digest

    def _write_archive_manifest(self, archive_digest: str) -> None:
        self._archive_manifest_path().write_text(
            json.dumps(
                {
                    "archive_name": _runtime_archive_name(),
                    "sha256": archive_digest,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _archive_runtime_command(self, install_dir: Path, node: list[str]) -> list[str] | None:
        cli_path = install_dir / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
        if not cli_path.exists():
            return None
        return [*node, str(cli_path)]

    def _reuse_existing_archive_runtime(self, command: list[str] | None) -> list[str] | None:
        if command:
            self._install_reason = None
            return command
        return None

    def _installed_github_runtime_command(self) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            return None
        return self._github_runtime_command(self._github_source_dir(), node)

    def _install_github_runtime(self) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            self._install_reason = "runtime_node_missing"
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        source_dir = self._github_source_dir()
        existing_command = self._github_runtime_command(source_dir, node)
        git = _resolve_command("git")
        npm = _resolve_command("npm")
        if not git:
            if existing_command:
                self._install_reason = None
                return existing_command
            self._install_reason = "runtime_git_missing"
            return None
        if not npm:
            if existing_command:
                self._install_reason = None
                return existing_command
            self._install_reason = "runtime_npm_missing"
            return None
        if not source_dir.exists():
            source_dir.parent.mkdir(parents=True, exist_ok=True)
            if not self._run_install_command([*git, "clone", "--depth", "1", "--branch", self.github_ref, self.github_repo, str(source_dir)]):
                return None
        else:
            if not self._run_install_command([*git, "-C", str(source_dir), "fetch", "--depth", "1", "origin", self.github_ref]):
                return self._reuse_existing_github_runtime(existing_command)
            if not self._run_install_command([*git, "-C", str(source_dir), "checkout", "FETCH_HEAD"]):
                return self._reuse_existing_github_runtime(existing_command)
        if not self._run_install_command([*npm, "ci"], cwd=source_dir):
            return self._reuse_existing_github_runtime(existing_command)
        if not self._run_install_command([*npm, "run", "build"], cwd=source_dir):
            return self._reuse_existing_github_runtime(existing_command)
        command = self._github_runtime_command(source_dir, node)
        if not command:
            self._install_reason = "runtime_install_missing_bin"
            return None
        return command

    def _github_runtime_command(self, source_dir: Path, node: list[str]) -> list[str] | None:
        cli_path = source_dir / "packages" / "runtime" / "dist" / "cli.js"
        if not cli_path.exists():
            return None
        return [*node, str(cli_path)]

    def _reuse_existing_github_runtime(self, command: list[str] | None) -> list[str] | None:
        if command:
            self._install_reason = None
            return command
        return None

    def _install_npm_runtime(self) -> list[str] | None:
        npm = _resolve_command("npm")
        if not npm:
            self._install_reason = "runtime_npm_missing"
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        install_root = self.runtime_dir / "package"
        install_root.mkdir(parents=True, exist_ok=True)
        package_json = install_root / "package.json"
        if not package_json.exists():
            package_json.write_text('{"private":true,"type":"module"}\n', encoding="utf-8")
        with self.install_log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(
                [
                    *npm,
                    "install",
                    "--prefix",
                    str(install_root),
                    "--no-audit",
                    "--no-fund",
                    self.package_spec,
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=180,
                check=False,
                **isolated_subprocess_kwargs(),
            )
        if result.returncode != 0:
            self._install_reason = "runtime_install_failed"
            return None
        resolved = _resolve_executable_path(self._managed_bin_path())
        if not resolved:
            self._install_reason = "runtime_install_missing_bin"
            return None
        return [resolved]

    def _managed_bin_path(self) -> Path:
        suffix = ".cmd" if os.name == "nt" else ""
        return self.runtime_dir / "package" / "node_modules" / ".bin" / f"{_RUNTIME_BIN}{suffix}"

    def _github_source_dir(self) -> Path:
        repo_slug = self.github_repo.removesuffix(".git").rstrip("/").rsplit("/", 2)[-2:]
        repo_part = "_".join(repo_slug) if len(repo_slug) == 2 else "vibe-show-runtime"
        ref_part = _safe_path_part(self.github_ref)
        return self.runtime_dir / "source" / "github" / repo_part / ref_part

    def _run_install_command(self, command: list[str], *, cwd: Path | None = None) -> bool:
        with self.install_log_path.open("a", encoding="utf-8") as log:
            log.write(f"$ {' '.join(command)}\n")
            result = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=300,
                check=False,
                **isolated_subprocess_kwargs(),
            )
        if result.returncode != 0:
            self._install_reason = "runtime_install_failed"
            return False
        return True


_manager: ShowRuntimeManager | None = None


def get_show_runtime_manager() -> ShowRuntimeManager:
    global _manager
    if _manager is None:
        _manager = ShowRuntimeManager()
    return _manager


def stop_show_runtime_manager() -> None:
    if _manager is not None:
        _manager.stop()


def _is_runtime_server_cmdline(cmdline: list[str], workspace_root: str) -> bool:
    """True if ``cmdline`` is a Show Runtime server bound to ``workspace_root``.

    Requires the exact ``--workspace-root <workspace_root>`` arg pair AND a runtime
    signature (the always-present ``--fallback-delay-seconds`` flag, the ``cli.js``
    entrypoint, the managed bin, or a ``show-runtime`` path token), so an unrelated
    process that merely mentions the path is never matched.
    """
    if not cmdline:
        return False
    bound = any(
        token == "--workspace-root" and index + 1 < len(cmdline) and cmdline[index + 1] == workspace_root
        for index, token in enumerate(cmdline)
    )
    if not bound:
        return False
    return any(
        token == "--fallback-delay-seconds"
        or token.endswith("cli.js")
        or token.endswith(_RUNTIME_BIN)
        or "show-runtime" in token
        for token in cmdline
    )


def sweep_orphan_show_runtime_servers(
    workspace_root: Path | str | None = None,
    *,
    keep_pid: int | None = None,
) -> list[int]:
    """Terminate any Show Runtime server still bound to ``workspace_root``.

    A prior avibe instance that died without reaping its child (SIGKILL / crash —
    ``atexit`` does not run) leaves a Node ``cli.js`` orphan reparented to init, still
    listening on its old port and able to warm/mutate this workspace root with stale
    in-memory templates (avibe-bot/avibe#813). The single-service-instance lock makes
    this process the only legitimate owner of the root, so any *other* process bound to
    it is an orphan and safe to reap.

    Best-effort and spawn-agnostic: complements the runtime's own parent-death self-exit
    (vibe-show-runtime#30) by also clearing orphans from builds that predate that
    backstop. Returns the pids swept (for logging/tests).
    """
    root = str(workspace_root) if workspace_root is not None else str(paths.get_show_pages_dir())
    try:
        import psutil
    except Exception:  # pragma: no cover - psutil is a hard dependency in practice
        return []

    own_pid = os.getpid()
    swept: list[int] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            pid = proc.info.get("pid")
            if pid is None or pid == own_pid or (keep_pid is not None and pid == keep_pid):
                continue
            if not _is_runtime_server_cmdline(proc.info.get("cmdline") or [], root):
                continue
            victims = proc.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:  # pragma: no cover - never let a stray psutil error block callers
            logger.debug("Failed to inspect process while sweeping show runtime orphans", exc_info=True)
            continue
        victims.append(proc)
        logger.warning(
            "Sweeping orphaned show runtime server pid=%s bound to workspace_root=%s", pid, root
        )
        for victim in victims:
            try:
                victim.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        _gone, alive = psutil.wait_procs(victims, timeout=3)
        for victim in alive:
            try:
                victim.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        swept.append(pid)
    return swept


async def prewarm_show_runtime() -> ShowRuntimeResult:
    return await get_show_runtime_manager().ensure()


async def prewarm_show_page_session(session_id: str, *, base_path: str | None = None) -> ShowRuntimeResult:
    return await get_show_runtime_manager().prewarm_session(session_id, base_path=base_path)


def set_show_runtime_manager_for_tests(manager: ShowRuntimeManager | None) -> None:
    global _manager
    previous = _manager
    # Stop the manager we are replacing before dropping the reference. Serving-path
    # tests that never install a fake cause get_show_runtime_manager() to lazily
    # create the real manager, which spawns a Node cli.js + esbuild subprocess tree
    # when a runtime is installed locally. If a later test swaps the global without
    # stopping it first, the reference is lost, the atexit cleanup at process exit
    # can no longer reap it, and the subprocess tree leaks for the machine's lifetime.
    if previous is not None and previous is not manager:
        try:
            previous.stop()
        except Exception:  # pragma: no cover - defensive cleanup
            logger.debug("failed to stop previous show runtime manager", exc_info=True)
    _manager = manager


def _show_runtime_prewarm_import_paths(
    response: httpx.Response,
    *,
    session_id: str,
    runtime_path: str,
    base_path: str | None,
) -> list[str]:
    content_type = response.headers.get("content-type", "")
    if "javascript" not in content_type and "html" not in content_type and "css" not in content_type:
        return []
    try:
        text = response.text
    except UnicodeDecodeError:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for match in _PREWARM_IMPORT_RE.finditer(text):
        value = match.group("path")
        path = _show_runtime_prewarm_runtime_path(
            value,
            session_id=session_id,
            runtime_path=runtime_path,
            base_path=base_path,
        )
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _show_runtime_prewarm_runtime_path(
    value: str,
    *,
    session_id: str,
    runtime_path: str,
    base_path: str | None,
) -> str | None:
    if not value or value.startswith(("http://", "https://", "data:", "blob:", "#")):
        return None
    raw_path, separator, query = value.partition("?")
    if not _show_runtime_prewarm_asset_path_allowed(raw_path):
        return None
    session_prefixes = [f"/show/{session_id}/", f"/p/{session_id}/"]
    if base_path:
        session_prefixes.insert(0, base_path.rstrip("/") + "/")
    for prefix in session_prefixes:
        if raw_path.startswith(prefix):
            asset_path = raw_path[len(prefix):]
            return _join_show_runtime_prewarm_path(runtime_path, asset_path, separator, query)
    # The shared vendor bundle (`/_show-runtime/vendor/...`) is session-independent and
    # the runtime warms it itself, so it is intentionally not prewarmed per session here.
    if raw_path.startswith("/src/") or raw_path.startswith("/@") or raw_path.startswith("/node_modules/"):
        return _join_show_runtime_prewarm_path(runtime_path, raw_path.lstrip("/"), separator, query)
    if raw_path.startswith("./"):
        return _join_show_runtime_prewarm_path(runtime_path, raw_path[2:], separator, query)
    if raw_path.startswith(("src/", "@", "node_modules/")):
        return _join_show_runtime_prewarm_path(runtime_path, raw_path, separator, query)
    return None


def _show_runtime_prewarm_asset_path_allowed(path: str) -> bool:
    if not path:
        return False
    if path.startswith(("/home/", "/Users/", "/tmp/", "/var/", "/private/")):
        return False
    return path.endswith((".js", ".mjs", ".ts", ".tsx", ".css")) or path in {
        "/@vite/client",
        "/@react-refresh",
    }


def _join_show_runtime_prewarm_path(runtime_path: str, asset_path: str, separator: str, query: str) -> str:
    path = f"{runtime_path}{urllib.parse.quote(asset_path.lstrip('/'), safe='/@:-._~')}"
    if separator:
        path = f"{path}?{query}"
    return path


def _auto_install_enabled() -> bool:
    value = os.environ.get("VIBE_SHOW_RUNTIME_AUTO_INSTALL")
    return value is None or value.strip().lower() not in _FALSE_VALUES


def _env_flag_enabled(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in _FALSE_VALUES


def _packaged_runtime_manifest_exists() -> bool:
    try:
        resource = package_resources.files("vibe").joinpath(_RUNTIME_MANIFEST_RESOURCE)
    except Exception:
        return False
    return resource.is_file()


def _normalize_runtime_source(value: str | None) -> str:
    normalized = (value or _RUNTIME_SOURCE_MANIFEST).strip().lower()
    aliases = {
        "manifest": _RUNTIME_SOURCE_MANIFEST,
        "manifest-cache": _RUNTIME_SOURCE_MANIFEST,
        "archive": _RUNTIME_SOURCE_ARCHIVE,
        "prebuilt": _RUNTIME_SOURCE_ARCHIVE,
        "github": _RUNTIME_SOURCE_GITHUB,
        "github-source": _RUNTIME_SOURCE_GITHUB,
        "npm": _RUNTIME_SOURCE_NPM,
    }
    return aliases.get(normalized, normalized or _RUNTIME_SOURCE_MANIFEST)


def _manifest_status_payload(manifest: ShowRuntimeManifest | None) -> dict[str, Any] | None:
    if manifest is None:
        return None
    return {
        "schema_version": manifest.schema_version,
        "runtime_version": manifest.runtime_version,
        "minimum_node": manifest.minimum_node,
        "sha256": manifest.digest,
        "source": manifest.source,
        "platforms": sorted(manifest.archives),
    }


def _archive_status_payload(archive: ShowRuntimeArchive | None) -> dict[str, Any] | None:
    if archive is None:
        return None
    return {
        "platform": archive.platform,
        "name": archive.name,
        "url": archive.url,
        "sha256": archive.sha256,
        "size": archive.size,
    }


def _runtime_archive_name() -> str:
    return f"{_RUNTIME_ARCHIVE_PREFIX}-{_runtime_platform_tag()}.tgz"


def _default_runtime_archive_url() -> str:
    return f"{_RUNTIME_ARCHIVE_RELEASE_BASE_URL}/{_runtime_archive_name()}"


def _runtime_platform_tag() -> str:
    raw = get_platform().lower()
    machine = raw.rsplit("-", 1)[-1]
    if machine == "universal2":
        machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        arch = "x64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        arch = machine
    if raw.startswith("macosx"):
        os_name = "darwin"
    elif raw.startswith("linux"):
        os_name = "linux"
    elif raw.startswith("win"):
        os_name = "win32"
    else:
        os_name = os.name
    return f"{os_name}-{arch}"


def _safe_extract_tar(tar: tarfile.TarFile, destination: Path) -> None:
    destination_resolved = destination.resolve()
    for member in tar.getmembers():
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise ValueError(f"Unsafe archive member type: {member.name}")
        target = (destination / member.name).resolve()
        if target != destination_resolved and destination_resolved not in target.parents:
            raise ValueError(f"Unsafe archive member path: {member.name}")
        if member.issym():
            link_target = (destination / member.name).parent / member.linkname
            link_target_resolved = link_target.resolve()
            if link_target_resolved != destination_resolved and destination_resolved not in link_target_resolved.parents:
                raise ValueError(f"Unsafe archive link target: {member.name}")
        elif member.islnk():
            link_target = destination / member.linkname
            link_target_resolved = link_target.resolve()
            if link_target_resolved != destination_resolved and destination_resolved not in link_target_resolved.parents:
                raise ValueError(f"Unsafe archive link target: {member.name}")
    try:
        tar.extractall(destination, filter="data")
    except TypeError:
        tar.extractall(destination)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value.strip())
    return cleaned or "main"


def _node_version(node: list[str]) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [*node, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            **isolated_subprocess_kwargs(),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return _parse_semver(result.stdout.strip())


def _parse_semver(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _format_semver(version: tuple[int, int, int] | None) -> str | None:
    if version is None:
        return None
    return ".".join(str(part) for part in version)


def _node_satisfies_requirement(version: tuple[int, int, int] | None, requirement: str | None) -> bool | None:
    if not requirement:
        return None
    if version is None:
        return False
    return any(_node_satisfies_clause(version, clause.strip()) for clause in requirement.split("||") if clause.strip())


def _node_satisfies_clause(version: tuple[int, int, int], clause: str) -> bool:
    if clause.startswith(">="):
        minimum = _parse_semver(clause[2:].strip())
        return minimum is not None and version >= minimum
    if clause.startswith("^"):
        minimum = _parse_semver(clause[1:].strip())
        if minimum is None or version < minimum:
            return False
        major, minor, patch = minimum
        if major > 0:
            ceiling = (major + 1, 0, 0)
        elif minor > 0:
            ceiling = (major, minor + 1, 0)
        else:
            ceiling = (major, minor, patch + 1)
        return version < ceiling
    exact = _parse_semver(clause)
    return exact is not None and version == exact


def _resolve_command(command: str) -> list[str] | None:
    parts = shlex.split(command)
    if not parts:
        return None
    executable = parts[0]
    if os.path.sep in executable or (os.altsep is not None and os.altsep in executable):
        path = Path(executable).expanduser()
        resolved = str(path) if path.exists() and os.access(path, os.X_OK) else None
    else:
        resolved = shutil.which(executable)
    if not resolved:
        return None
    return [resolved, *parts[1:]]


def _resolve_node_command() -> list[str] | None:
    configured = os.environ.get("VIBE_SHOW_RUNTIME_NODE_BIN")
    if configured:
        return _resolve_command(configured)
    return _resolve_command("node")


def _resolve_executable_path(path: Path) -> str | None:
    expanded = path.expanduser()
    return str(expanded) if expanded.exists() and os.access(expanded, os.X_OK) else None


atexit.register(stop_show_runtime_manager)
