from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import yaml

from config.atomic_io import write_atomic
from config.v2_config import normalize_model_hub_base_url
from vibe.model_hub_runtime.api_key_vendors import (
    official_api_key_base_url,
    openai_compatible_endpoint,
    validate_api_key_auth_scheme,
)
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
    generation: str = "0",
) -> None:
    _secure_write_text(
        path,
        render_engine_config(
            path,
            host=host,
            port=port,
            auth_dir=auth_dir,
            runtime_secrets=runtime_secrets,
            sources=sources,
            state_store=state_store,
            generation=generation,
        ),
    )


def render_engine_config(
    path: Path,
    *,
    host: str,
    port: int,
    auth_dir: Path,
    runtime_secrets: RuntimeSecrets,
    sources: Iterable[SourceRecord],
    state_store: EngineStateStore,
    generation: str = "0",
) -> str:
    """Render the engine YAML that ``path`` holds, for a start or a hot reload.

    ``generation`` marks every configured model's display name, so a reload is
    observable as applied even when it leaves the routed model IDs unchanged.
    """
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
        "disable-claude-cloak-mode": True,
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
        _append_source(payload, source, state_store, generation)
    return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)


def _append_source(
    payload: dict[str, Any],
    source: SourceRecord,
    store: EngineStateStore,
    generation: str = "0",
) -> None:
    credential = store.credential_metadata(source.credential_ref)
    if credential["kind"] == "oauth":
        # OAuth credentials are engine auth files, not YAML credential values.
        return
    api_key = store.read_api_key(source.credential_ref)
    try:
        validate_api_key_auth_scheme(
            source.vendor, source.protocol, source.base_url, api_key, credential.get("auth_scheme"),
        )
    except ValueError:
        raise EngineStateError("unsupported API key authentication scheme") from None
    reasoning_by_model = dict(source.model_reasoning_efforts)
    # CLIProxyAPI reads input modalities only on openai-compatibility models,
    # matching an entry by name, then alias. For one declared text-only it
    # replaces tool-result images with a marker; any other list, or none,
    # keeps images, so text-only is the only declaration worth writing.
    # Its name match is looser than a routed ID (see _engine_model_name) and
    # takes the first entry that matches, so models it cannot tell apart are
    # declared text-only together or not at all.
    routed = tuple(dict.fromkeys((*source.model_ids, *source.route_model_ids)))
    marked = set(source.text_only_model_ids) if source.protocol == "openai_chat" else set()
    unmarked_names = {_engine_model_name(model) for model in routed if model not in marked}
    text_only = {model for model in marked if _engine_model_name(model) not in unmarked_names}
    models = []
    for model in routed:
        entry: dict[str, Any] = {
            "name": model,
            "alias": model,
            "display-name": reload_display_name(model, generation),
        }
        reasoning_efforts = reasoning_by_model.get(model, ())
        if reasoning_efforts:
            # CLIProxyAPI's measured model-registration shape is strongest-first.
            entry["thinking"] = {"levels": list(reversed(reasoning_efforts))}
        if model in text_only:
            entry["input-modalities"] = ["text"]
        models.append(entry)
    if source.protocol == "anthropic":
        base_url = source.base_url
        if not base_url:
            base_url = official_api_key_base_url(source.vendor)
        if not base_url:
            raise EngineStateError("Anthropic-compatible source requires a base URL")
        entry: dict[str, Any] = {
            "api-key": api_key,
            "prefix": source.prefix,
            "base-url": base_url,
            "cloak": {"mode": "never"},
            "rebuild-mid-system-message": False,
        }
        if models:
            entry["models"] = models
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
                "base-url": openai_compatible_endpoint(base_url),
                "api-key-entries": [{"api-key": api_key}],
                "models": models,
            }
        )
        return
    raise EngineStateError("unsupported source protocol")


def expected_model_names(
    sources: Iterable[SourceRecord],
    store: EngineStateStore,
    generation: str,
) -> dict[str, str]:
    """Routed ID to display name the rendered YAML registers; OAuth models come from auth files."""
    expected: dict[str, str] = {}
    for source in sources:
        metadata = store.credential_metadata_if_present(source.credential_ref)
        if metadata is None or metadata.get("kind") != "api_key":
            continue
        for model in (*source.model_ids, *source.route_model_ids):
            expected[f"{source.prefix}/{model}"] = reload_display_name(model, generation)
    return expected


def _engine_model_name(model: str) -> str:
    """The name CLIProxyAPI compares when it looks up a model's input modalities.

    It trims the name, drops a trailing ``(...)`` thinking suffix, trims again,
    and compares case-insensitively.
    """
    name = model.strip()
    suffix = name.rfind("(")
    if suffix != -1 and name.endswith(")"):
        name = name[:suffix]
    return name.strip().casefold()


def reload_display_name(model: str, generation: str) -> str:
    return f"{model} #{generation}"


def _secure_write_text(path: Path, text: str) -> None:
    # The file is 0600 by ``write_atomic``; the directory is this function's own
    # concern, because the config it holds names an upstream API key.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    write_atomic(path, text)
