from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import yaml

from config.atomic_io import write_atomic
from vibe.model_hub_runtime.api_key_vendors import official_api_key_base_url
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore, RuntimeSecrets, SourceRecord


def write_engine_config(
    path: Path,
    *,
    host: str,
    port: int,
    auth_dir: Path,
    runtime_secrets: RuntimeSecrets,
    sources: Iterable[SourceRecord],
    state_store: EngineStateStore,
) -> None:
    if host != "127.0.0.1":
        raise EngineStateError("model hub engine must bind to 127.0.0.1")
    payload: dict[str, Any] = {
        "host": host,
        "port": port,
        "tls": {"enable": False, "cert": "", "key": ""},
        "remote-management": {
            "allow-remote": False,
            "secret-key": runtime_secrets.management_key,
            "disable-control-panel": True,
            "disable-auto-update-panel": True,
        },
        "auth-dir": str(auth_dir),
        "api-keys": [runtime_secrets.gateway_token],
        "debug": False,
        "pprof": {"enable": False, "addr": "127.0.0.1:0"},
        "plugins": {"enabled": False, "dir": str(path.parent / "plugins"), "configs": {}},
        "commercial-mode": True,
        "logging-to-file": False,
        "request-log": False,
        "usage-statistics-enabled": False,
        "redis-usage-queue-retention-seconds": 60,
        "proxy-url": "",
        "force-model-prefix": True,
        "passthrough-headers": False,
        "request-retry": 0,
        "max-retry-credentials": 1,
        "max-retry-interval": 0,
        "disable-cooling": True,
        "save-cooldown-status": False,
        "transient-error-cooldown-seconds": -1,
        "quota-exceeded": {
            "switch-project": False,
            "switch-preview-model": False,
            "antigravity-credits": False,
        },
        "routing": {"strategy": "fill-first", "session-affinity": False},
        "ws-auth": True,
    }
    for source in sources:
        _append_source(payload, source, state_store)
    _secure_write_yaml(path, payload)


def _append_source(payload: dict[str, Any], source: SourceRecord, store: EngineStateStore) -> None:
    credential = store.credential_metadata(source.credential_ref)
    if credential["kind"] == "oauth":
        # OAuth credentials are engine auth files, not YAML credential values.
        return
    api_key = store.read_api_key(source.credential_ref)
    models = [{"name": model, "alias": model} for model in source.model_ids]
    if source.protocol == "anthropic":
        entry: dict[str, Any] = {"api-key": api_key, "prefix": source.prefix}
        if models:
            entry["models"] = models
        if source.base_url:
            entry["base-url"] = source.base_url
        payload.setdefault("claude-api-key", []).append(entry)
        return
    if source.protocol == "openai_responses":
        base_url = source.base_url
        if not base_url:
            base_url = official_api_key_base_url(source.vendor)
        if not base_url:
            raise EngineStateError("Responses API source requires a base URL")
        entry = {"api-key": api_key, "prefix": source.prefix, "base-url": base_url}
        if models:
            entry["models"] = models
        payload.setdefault("codex-api-key", []).append(entry)
        return
    if source.protocol == "openai_chat":
        base_url = source.base_url
        if not base_url:
            base_url = official_api_key_base_url(source.vendor)
        if not base_url:
            raise EngineStateError("OpenAI-compatible source requires a base URL")
        payload.setdefault("openai-compatibility", []).append(
            {
                "name": source.prefix,
                "prefix": source.prefix,
                "base-url": base_url,
                "api-key-entries": [{"api-key": api_key}],
                "models": models,
            }
        )
        return
    raise EngineStateError("unsupported source protocol")


def _secure_write_yaml(path: Path, payload: dict[str, Any]) -> None:
    # The file is 0600 by ``write_atomic``; the directory is this function's own
    # concern, because the config it holds names an upstream API key.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    write_atomic(path, yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))
