"""Controller-owned background native inventory discovery. Readers never join this task."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import time

from config.v2_compat import runtime_agent_cli_path
from config.v2_config import V2Config, is_model_hub_enabled
from core.backend_restart import NativeCredentialLease, finish_native_operation
from core.handlers.direct_inventory_identity import NativeScope, inventory_scope
from core.handlers.direct_model_menu import publish_inventory, read_inventory
from modules.agents.catalog import NATIVE_CLI_BACKENDS, default_cli_for_backend
from modules.agents.direct_inventory import InventoryNotReady, discover

logger = logging.getLogger(__name__)
_POLL = 60
_INTERVAL = 3600


@dataclass(frozen=True)
class _Source:
    scope: NativeScope
    binary: str
    build: tuple
    directory: str

    @property
    def trigger(self):
        return self.scope.identity, self.binary, self.build, self.directory


def _source(config, backend, directory):
    mode = config.model_hub.agents.get(backend)
    if is_model_hub_enabled() and getattr(mode, "mode", None) == "hub":
        return None
    cwd = str(Path(directory or config.runtime.default_cwd).expanduser().resolve())
    scope = inventory_scope(backend, config, cwd=cwd)
    if scope is None:
        return None
    binary = runtime_agent_cli_path(
        getattr(config.agents, backend).cli_path, default_cli_for_backend(backend), resolve_agent_paths=True
    )
    if backend == "claude" and binary == "claude":
        # Same resolution as connect(): embedded SDK binary first, then its
        # platform-aware search. Resolve only; don't open an SDK transport.
        from claude_agent_sdk import ClaudeAgentOptions
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

        binary = SubprocessCLITransport("", ClaudeAgentOptions())._find_cli()
    binary = os.path.expanduser(binary)
    if not os.path.isabs(binary):
        if os.path.dirname(binary):
            return None
        binary = shutil.which(binary)
    if not binary or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        return None
    path = Path(binary).resolve()
    stat = path.stat()
    return _Source(scope, str(path), (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size), cwd)


@dataclass
class _Attempt:
    revision: tuple
    retry_at: float
    failures: int = 0


class NativeInventoryRefresh:
    def __init__(self, controller):
        self.controller = controller
        self.directories = OrderedDict()
        self.attempts = {}
        self.task = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="native-model-inventory")

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def remember_opencode_cwd(self, cwd: str):
        self.directories[cwd] = None
        self.directories.move_to_end(cwd)
        if len(self.directories) > 8:
            self.directories.popitem(last=False)

    async def _run(self):
        while True:
            try:
                config = await asyncio.to_thread(V2Config.load, persist_migrations=False)
                # Antigravity publishes at the trusted ACP observation; there
                # is no separately attributable CLI discovery for it today.
                work = [(backend, None) for backend in NATIVE_CLI_BACKENDS if backend != "antigravity"]
                work.extend(("opencode", cwd) for cwd in tuple(self.directories) if cwd != config.runtime.default_cwd)
                self.attempts = {k: v for k, v in self.attempts.items() if k in work}
                for backend, cwd in work:
                    await self._refresh(config, backend, cwd)
            except Exception:
                logger.debug("Native inventory configuration unavailable")
            await asyncio.sleep(_POLL)

    async def _refresh(self, config, backend, cwd):
        slot = (backend, cwd)
        try:
            source = await asyncio.to_thread(_source, config, backend, cwd)
        except Exception:
            source = None
        if source is None:
            self.attempts.pop(slot, None)
            return
        last = self.attempts.get(slot)
        now = time.monotonic()
        if last and last.revision == source.trigger and last.retry_at > now:
            return
        failures = last.failures if last and last.revision == source.trigger else 0
        try:
            service = self.controller.agent_service
            if not service.is_backend_ready(backend):
                return
            # Cross-process, nonblocking lease. Auth cutover, native takeover
            # and CLI replacement own the same lease. Never start a probe if
            # they have admission, and never release it before cleanup ends.
            with NativeCredentialLease((backend,)) as lease:
                await asyncio.to_thread(lease.assert_auth_custody, backend)
                if not service.is_backend_ready(backend):
                    return
                rows = await asyncio.wait_for(
                    discover(
                        backend,
                        getattr(config.agents, backend),
                        source.binary,
                        source.directory,
                        source.scope,
                        self.controller,
                    ),
                    timeout=18,
                )

                def commit_if_current():
                    if _source(V2Config.load(persist_migrations=False), backend, cwd) != source:
                        return False
                    identity = source.scope.identity
                    if backend == "codex":
                        # A successful model-list command can silently fall back
                        # to its bundled table after a refresh failure. Without
                        # authoritative withdrawal evidence, never replace an
                        # account's last good reply with a subset or a union.
                        previous = read_inventory(backend, identity)
                        if not {r.id for r in previous.models}.issubset({r.id for r in rows}):
                            raise ValueError("Codex withdrawal not confirmed")
                    publish_inventory(backend, identity, rows)
                    return True

                if not await finish_native_operation(asyncio.to_thread(commit_if_current)):
                    self.attempts.pop(slot, None)
                    return
        except InventoryNotReady:
            # No native read occurred. An ordinary turn may warm this cwd before the next poll.
            self.attempts.pop(slot, None)
            return
        except Exception:
            logger.debug("No complete %s native inventory", backend)
            failures = min(7, failures + 1)
            self.attempts[slot] = _Attempt(source.trigger, now + min(_INTERVAL, _POLL * 2 ** (failures - 1)), failures)
        else:
            self.attempts[slot] = _Attempt(source.trigger, now + _INTERVAL)
