"""Controller-owned background native model discovery. Readers never join these tasks."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
import time

from config.v2_compat import runtime_agent_cli_path
from config.v2_config import V2Config, is_model_hub_enabled
from core.handlers.direct_inventory_identity import inventory_identity
from core.handlers.direct_model_menu import publish_inventory
from modules.agents.direct_inventory import discover

logger = logging.getLogger(__name__)
_BACKENDS = ("codex", "claude", "antigravity", "opencode")
_POLL = 60
_INTERVAL = 3600


def _source(config, backend, directory):
    mode = config.model_hub.agents.get(backend)
    if is_model_hub_enabled() and getattr(mode, "mode", None) == "hub":
        return None
    cwd = str(Path(directory or config.runtime.default_cwd).expanduser().resolve())
    identity = inventory_identity(backend, config, cwd=cwd)
    if identity is None:
        return None
    settings = getattr(config.agents, backend)
    binary = runtime_agent_cli_path(
        settings.cli_path, "agy" if backend == "antigravity" else backend, resolve_agent_paths=True
    )
    if backend == "claude" and binary == "claude":
        import claude_agent_sdk
        import os

        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / ("claude.exe" if os.name == "nt" else "claude")
        if bundled.is_file():
            binary = str(bundled)
    path = Path(shutil.which(str(Path(binary).expanduser())) or binary).expanduser().resolve()
    stat = path.stat()
    return identity, str(path), (stat.st_ino, stat.st_mtime_ns, stat.st_size), cwd


@dataclass
class _Attempt:
    revision: tuple
    retry_at: float
    failures: int = 0


class NativeInventoryRefresh:
    """One worker, bounded interest in OpenCode directories, last good in PR1's store."""

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
                config = await asyncio.to_thread(V2Config.load)
                work = [(backend, None) for backend in _BACKENDS]
                work.extend(("opencode", cwd) for cwd in tuple(self.directories) if cwd != config.runtime.default_cwd)
                self.attempts = {key: value for key, value in self.attempts.items() if key in work}
                for backend, cwd in work:
                    await self._refresh(config, backend, cwd)
            except Exception:
                logger.debug("Native inventory configuration unavailable")
            await asyncio.sleep(_POLL)

    async def _refresh(self, config, backend, cwd):
        slot = (backend, cwd)
        try:
            source = await asyncio.to_thread(_source, config, backend, cwd)
        except Exception:  # A failed scope read for one backend must not starve the others.
            source = None
        if source is None:
            self.attempts.pop(slot, None)
            return
        last = self.attempts.get(slot)
        now = time.monotonic()
        if last and last.revision == source and last.retry_at > now:
            return
        failures = last.failures if last and last.revision == source else 0
        identity, binary, _, directory = source
        try:
            rows = await asyncio.wait_for(
                discover(backend, getattr(config.agents, backend), binary, directory, self.controller), timeout=18
            )

            def commit_if_current():
                if _source(V2Config.load(), backend, cwd) == source:
                    publish_inventory(backend, identity, rows)

            await asyncio.to_thread(commit_if_current)
        except Exception:
            # Native diagnostics can contain account/provider data, so log only the backend.
            logger.debug("No complete %s native inventory", backend)
            failures = min(7, failures + 1)
            self.attempts[slot] = _Attempt(source, now + min(_INTERVAL, _POLL * 2 ** (failures - 1)), failures)
        else:
            self.attempts[slot] = _Attempt(source, now + _INTERVAL)
