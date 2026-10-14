"""Optional Harbor 0.24 installed-agent bridge to the full Avibe product.

No extracted product code, no alternate loop, no dependency on Harbor in the
product wheel. Install requirements-harbor.txt in the evaluation host only.
"""
from __future__ import annotations

import asyncio
from email.parser import Parser
import hashlib
from importlib.metadata import version
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
import zipfile

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template

from .profile import Profile


_WRAPPER_FILES = ("__init__.py", "profile.py", "control.py", "runner.py", "launch.py", "credential.py", "diagnostics.py")


async def _join_transfer(awaitable):
    """A cancelled upload must settle before remote credential cleanup."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                result = await task
                break
    if cancelled:
        raise asyncio.CancelledError
    return result


class FullAvibeAgent(BaseInstalledAgent):
    capabilities = AgentCapabilities(atif=False, resume=False)

    @staticmethod
    def name():
        return "avibe-full-runtime"

    def __init__(self, *args, wheel_path: str, wheel_sha256: str, profile: dict, **kwargs):
        if version("harbor") != "0.24.0":
            raise ValueError("this integration requires Harbor 0.24.0")
        self.profile = Profile.parse(profile)
        if kwargs.get("model_name") != self.profile.model:
            raise ValueError("explicit Harbor model_name must equal profile.model")
        self.wheel = Path(wheel_path).expanduser().resolve()
        if self.wheel.suffix != ".whl" or not self.wheel.is_file():
            raise ValueError("a trusted local Avibe wheel file is required")
        if not re.fullmatch(r"[0-9a-f]{64}", wheel_sha256):
            raise ValueError("wheel_sha256 must be an explicit lowercase SHA256")
        self.wheel_sha256 = wheel_sha256
        self.nonce = uuid.uuid4().hex
        self.root = PurePosixPath("/installed-agent") / ("avibe-full-" + self.nonce)
        self.python = self.root / "venv/bin/python"
        self.provenance = {}
        self._credential_claimed = False
        self.credential_path = self.root / "credential/provider.key"
        super().__init__(*args, **kwargs)
        if self.profile.credential_env in self.extra_env:
            raise ValueError("supply the named credential in the host environment, not Harbor agent-env/config")
        self.execution_dir = self.environment_logs_dir / ("full-runtime-" + self.nonce)

    def _snapshot(self, temporary: Path):
        # Hash the immutable COPY that is uploaded, not an earlier observation
        # of a file somebody might replace during container setup.
        wheel = temporary / self.wheel.name
        shutil.copyfile(self.wheel, wheel)
        with wheel.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != self.wheel_sha256:
                raise ValueError("Avibe wheel SHA256 mismatch")
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise ValueError("invalid wheel metadata")
            metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
        if metadata.get("Name", "").lower().replace("_", "-") != "avibe-os":
            raise ValueError("the pinned wheel must be avibe-os")
        source = Path(__file__).resolve().parent
        repo = source.parents[1]
        staged = temporary / "source/scripts/terminal_bench"
        staged.mkdir(parents=True)
        digest = hashlib.sha256()
        for name in _WRAPPER_FILES:
            relative = (source / name).relative_to(repo).as_posix()
            check = subprocess.run(["git", "ls-files", "--error-unmatch", "--", relative],
                                   cwd=repo, capture_output=True, timeout=10)
            if check.returncode:
                raise ValueError("wrapper upload requires tracked source: " + relative)
            content = (source / name).read_bytes()
            (staged / name).write_bytes(content)
            digest.update(relative.encode() + b"\0" + content + b"\0")
        (staged.parent / "__init__.py").write_text("", encoding="utf-8")
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True, timeout=10).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True, timeout=10)
        self.provenance = {
            "agent": self.name(), "harbor_version": version("harbor"), "atif": False,
            "wheel_filename": wheel.name, "wheel_sha256": self.wheel_sha256,
            "avibe_version": metadata["Version"], "wrapper_commit": commit,
            "wrapper_source_sha256": digest.hexdigest(), "wrapper_source_dirty": bool(dirty.strip()),
        }
        return wheel, temporary / "source"

    async def install(self, environment):
        with tempfile.TemporaryDirectory(prefix="avibe-full-source-") as temp:
            wheel, source = await asyncio.to_thread(self._snapshot, Path(temp))
            # Wheel/provenance checks occur before any mutation of the task.
            uid = await environment.exec(command="id -u")
            if uid.return_code or (uid.stdout or "").strip() != "0":
                raise RuntimeError("first full-runtime Harbor pilot requires a root task user")
            await self.ensure_system_dependencies(environment, ("python3", "python_venv"))
            await self.exec_as_root(environment, "mkdir -m 700 " + shlex.quote(str(self.root)))
            await environment.upload_file(wheel, str(self.root / self.wheel.name))
            await environment.upload_dir(source, str(self.root / "source"))
        # Installed distributions, not editable source. A .pth installs ONLY
        # this small wrapper for -I imports; task PYTHONPATH remains unmodified.
        pth = "import pathlib,sysconfig; pathlib.Path(sysconfig.get_path('purelib'),'avibe_benchmark.pth').write_text(" + repr(str(self.root / "source") + "\n") + ")"
        command = (
            "set -eu; python3 -c 'import sys; assert sys.version_info >= (3, 10)' && "
            f"python3 -m venv {shlex.quote(str(self.root / 'venv'))} && "
            f"{shlex.quote(str(self.python))} -m pip install --disable-pip-version-check --no-input "
            f"{shlex.quote(str(self.root / self.wheel.name))} && "
            f"{shlex.quote(str(self.python))} -c {shlex.quote(pth)}"
        )
        await self.exec_as_root(environment, command, timeout_sec=int(self.profile.setup_seconds))
        # Add ONLY a private vibe shim to PATH during execution, not the venv's
        # Python binaries: task Python/conda/java resolution must stay unchanged.
        await self.exec_as_root(environment,
            "mkdir -p " + shlex.quote(str(self.root / "bin")) + "; ln -s "
            + shlex.quote(str(self.root / "venv/bin/vibe")) + " " + shlex.quote(str(self.root / "bin/vibe")))
    async def _stage_credential(self, environment, key):
        result = await environment.exec(command="mkdir -m 700 " + shlex.quote(str(self.credential_path.parent)), user="root")
        if result.return_code:
            raise RuntimeError("could not exclusively claim credential directory")
        self._credential_claimed = True
        with tempfile.TemporaryDirectory(prefix="avibe-full-key-") as temp:
            path = Path(temp) / "provider.key"
            # The directory is 0700 already; explicitly make the file 0600
            # before writing and use upload_file rather than any shell literal.
            import os
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(key)
            await _join_transfer(environment.upload_file(path, str(self.credential_path)))
        checked = await environment.exec(command=shlex.join([
            str(self.python), "-I", "-m", "scripts.terminal_bench.credential", "prepare", str(self.credential_path),
        ]), user="root")
        if checked.return_code:
            raise RuntimeError("credential metadata refused")
        metadata = json.loads(checked.stdout)
        if metadata.get("regular") is not True or metadata.get("mode") != "0600" or metadata.get("directory_mode") != "0700":
            raise RuntimeError("credential metadata refused")

    async def _clear_credential(self, environment):
        if not self._credential_claimed:
            return
        result = await environment.exec(command=shlex.join([
            str(self.python), "-I", "-m", "scripts.terminal_bench.credential", "clean", str(self.credential_path),
        ]), user="root", timeout_sec=15)
        if result.return_code or json.loads(result.stdout).get("removed") is not True:
            raise RuntimeError("credential cleanup unconfirmed; destroy task container before grading")
        self._credential_claimed = False

    async def _cleanup(self, environment):
        command = shlex.join([
            str(self.python), "-I", "-m", "scripts.terminal_bench.launch", "stop",
            "--root", str(self.root / "launch"), "--nonce", self.nonce,
            "--receipt", str(self.execution_dir / "execution.json"),
            "--timeout", str(self.profile.cleanup_seconds + 60),
        ])
        try:
            result = await environment.exec(command=command, user="root", timeout_sec=int(self.profile.cleanup_seconds + 75))
        finally:
            await self._clear_credential(environment)
        if result.return_code != 0:
            raise RuntimeError("full-runtime cleanup unconfirmed; destroy task container before grading")
        try:
            receipt = json.loads(result.stdout)
            if receipt.get("quiescent") is not True:
                raise ValueError
        except (ValueError, TypeError):
            raise RuntimeError("full-runtime cleanup receipt invalid; destroy task container before grading") from None
        return receipt

    @with_prompt_template
    async def run(self, instruction, environment, context):
        if not self.provenance:
            raise RuntimeError("verified installation is required before execution")
        cwd_result = await environment.exec(command="pwd -P")
        workdir = (cwd_result.stdout or "").strip()
        if cwd_result.return_code or not workdir.startswith("/") or "\n" in workdir:
            raise RuntimeError("task cwd could not be determined")
        for private in (self.root, self.execution_dir):
            task = PurePosixPath(workdir)
            if task == private or task in private.parents or private in task.parents:
                raise RuntimeError("task and framework directories must be disjoint")
        key = self._get_env(self.profile.credential_env)
        if not key:
            raise ValueError("the named benchmark credential is missing")
        with tempfile.TemporaryDirectory(prefix="avibe-full-config-") as temp:
            for name, text in (("profile.json", json.dumps(self.profile.payload())), ("instruction.txt", instruction)):
                path = Path(temp) / name
                path.write_text(text, encoding="utf-8")
                await environment.upload_file(path, str(self.root / name))
        command = shlex.join([
            str(self.python), "-I", "-m", "scripts.terminal_bench.launch", "run",
            "--root", str(self.root / "launch"), "--nonce", self.nonce, "--",
            "--workdir", workdir, "--state", str(self.root / "state"),
            "--output", str(self.execution_dir), "--profile", str(self.root / "profile.json"),
            "--instruction", str(self.root / "instruction.txt"), "--sandbox-environment",
            "--credential-file", str(self.credential_path),
        ])
        command = "export PATH=" + shlex.quote(str(self.root / "bin")) + ":\"$PATH\"; exec " + command
        try:
            await self._stage_credential(environment, key)
            # Model setup is part of actual full-product execution, hence the
            # outer exec allowance includes it. Harbor's task timeout remains
            # authoritative and may cancel this earlier; cleanup is mandatory.
            allowance = int(self.profile.setup_seconds * 4 + self.profile.wall_seconds
                            + self.profile.cleanup_seconds * 2 + 90)
            result = await environment.exec(command=command, cwd=str(self.root), timeout_sec=allowance)
            if result.return_code:
                raise RuntimeError("full-runtime wrapper infrastructure failure; inspect its structured report")
        finally:
            # Shield cleanup from the cancellation that interrupted docker exec,
            # but JOIN it: verification must never race an orphan writer.
            cleanup = asyncio.create_task(self._cleanup(environment))
            while True:
                try:
                    cleanup_receipt = await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    if cleanup.done():
                        cleanup_receipt = await cleanup
                        break
            if cleanup_receipt.get("started") is not False:
                await self._collect(environment, context)

    async def _collect(self, environment, context):
        local = self.logs_dir / "full-runtime"
        local.mkdir(parents=True, exist_ok=True)
        report_path = local / "execution.json"
        await environment.download_file(str(self.execution_dir / "execution.json"), report_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("cleanup", {}).get("owned_processes_stopped") is not True:
            raise RuntimeError("full-runtime report did not confirm shutdown")
        totals = (report.get("usage") or {}).get("totals", {})
        # A missing token report is unknown, not zero-cost inference.
        if totals.get("token_reports", 0) > 0:
            context.n_input_tokens = totals.get("input_tokens")
            context.n_cache_tokens = totals.get("cached_input_tokens")
            context.n_output_tokens = totals.get("output_tokens")
        context.cost_usd = None
        context.metadata = {**self.provenance, "execution_outcome": report.get("outcome"),
                            "accounting_complete": report.get("accounting_complete", False),
                            "run_id": report.get("run_id"), "session_id": report.get("session_id"),
                            "verifier_reward": None}
        (local / "adapter-provenance.json").write_text(json.dumps(context.metadata, indent=2), encoding="utf-8")
        if (report.get("outcome") not in {"succeeded", "failed", "canceled", "cancelled", "time_limit"}
                or not isinstance(report.get("run_id"), str) or not report["run_id"]
                or not isinstance(report.get("session_id"), str) or not report["session_id"]):
            raise RuntimeError("full-runtime execution was not admitted; not an Agent failure score")
