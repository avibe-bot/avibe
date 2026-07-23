"""OAuth channel dispatch state for Model Hub."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Protocol

from .adapter import OAuthFlowState

OAuthChannel = Literal["native_cli", "hub"]


@dataclass(frozen=True)
class OAuthFlowBinding:
    channel: OAuthChannel
    source_id: Optional[str]
    vendor: Optional[str]


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
    """Persist the non-secret source identity associated with each in-flight flow."""

    def __init__(self, path: Path, *, max_entries: int = 100):
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.RLock()

    def _read(self) -> dict[str, OAuthFlowBinding]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        flows = {}
        for flow_id, value in payload.items():
            if not isinstance(flow_id, str):
                continue
            if isinstance(value, str) and value in {"native_cli", "hub"}:
                flows[flow_id] = OAuthFlowBinding(value, None, None)
                continue
            if not isinstance(value, dict):
                continue
            channel = value.get("channel")
            source_id = value.get("source_id")
            vendor = value.get("vendor")
            if (
                channel in {"native_cli", "hub"}
                and (source_id is None or (isinstance(source_id, str) and source_id))
                and (vendor is None or (isinstance(vendor, str) and vendor))
            ):
                flows[flow_id] = OAuthFlowBinding(channel, source_id, vendor)
        return flows

    def _write(self, payload: dict[str, OAuthFlowBinding]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        bounded = dict(list(payload.items())[-self.max_entries :])
        content = json.dumps(
            {
                flow_id: {
                    "channel": binding.channel,
                    "source_id": binding.source_id,
                    "vendor": binding.vendor,
                }
                for flow_id, binding in bounded.items()
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_name = tmp.name
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, self.path)

    def remember(self, flow_id: str, channel: OAuthChannel, source_id: str, vendor: str) -> None:
        with self._lock:
            flows = self._read()
            flows.pop(flow_id, None)
            flows[flow_id] = OAuthFlowBinding(channel, source_id, vendor)
            self._write(flows)

    def channel(self, flow_id: str) -> OAuthChannel | None:
        binding = self.binding(flow_id)
        return binding.channel if binding is not None else None

    def binding(self, flow_id: str) -> OAuthFlowBinding | None:
        with self._lock:
            return self._read().get(flow_id)

    def forget(self, flow_id: str) -> None:
        with self._lock:
            flows = self._read()
            if flow_id not in flows:
                return
            flows.pop(flow_id)
            self._write(flows)
