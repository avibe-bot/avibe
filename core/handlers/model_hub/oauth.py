"""OAuth channel dispatch state for Model Hub."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Literal, Protocol

from .adapter import OAuthFlowState

OAuthChannel = Literal["native_cli", "hub"]


class OAuthAdapter(Protocol):
    async def start_oauth(self, source_id: str, vendor: str) -> OAuthFlowState: ...

    async def oauth_status(self, flow_id: str) -> OAuthFlowState: ...

    async def submit_oauth(self, flow_id: str, value: str) -> OAuthFlowState: ...

    async def cancel_oauth(self, flow_id: str) -> None: ...


class NativeOAuthUnavailableError(RuntimeError):
    pass


class UnavailableNativeOAuthAdapter:
    """L3 replaces this adapter when native CLI OAuth is wired."""

    async def start_oauth(self, source_id: str, vendor: str) -> OAuthFlowState:
        raise NativeOAuthUnavailableError

    async def oauth_status(self, flow_id: str) -> OAuthFlowState:
        raise NativeOAuthUnavailableError

    async def submit_oauth(self, flow_id: str, value: str) -> OAuthFlowState:
        raise NativeOAuthUnavailableError

    async def cancel_oauth(self, flow_id: str) -> None:
        raise NativeOAuthUnavailableError


class OAuthFlowRegistry:
    """Persist the non-secret channel associated with each in-flight flow."""

    def __init__(self, path: Path, *, max_entries: int = 100):
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.RLock()

    def _read(self) -> dict[str, OAuthChannel]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            flow_id: channel
            for flow_id, channel in payload.items()
            if isinstance(flow_id, str) and channel in {"native_cli", "hub"}
        }

    def _write(self, payload: dict[str, OAuthChannel]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        bounded = dict(list(payload.items())[-self.max_entries :])
        content = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_name = tmp.name
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, self.path)

    def remember(self, flow_id: str, channel: OAuthChannel) -> None:
        with self._lock:
            flows = self._read()
            flows.pop(flow_id, None)
            flows[flow_id] = channel
            self._write(flows)

    def channel(self, flow_id: str) -> OAuthChannel | None:
        with self._lock:
            return self._read().get(flow_id)

    def forget(self, flow_id: str) -> None:
        with self._lock:
            flows = self._read()
            if flow_id not in flows:
                return
            flows.pop(flow_id)
            self._write(flows)
