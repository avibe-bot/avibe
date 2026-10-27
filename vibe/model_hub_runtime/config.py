from __future__ import annotations

from pathlib import Path
from typing import Any, Final, Iterable
from urllib.parse import urlsplit

import yaml

from config.atomic_io import write_atomic
from core.agent_core.ai.claude_thinking import uses_adaptive_thinking
from config.v2_config import normalize_model_hub_base_url
from vibe.model_hub_runtime.api_key_vendors import (
    google_api_base_url,
    official_api_key_base_url,
    openai_compatible_endpoint,
    validate_api_key_auth_scheme,
)
from vibe.backend_model_catalog import IMAGE_PROTOCOLS, source_image_models
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore, RuntimeSecrets, SourceRecord

# The engine rotates main.log at 10 MB and deletes the oldest rotated files beyond this total.
ENGINE_LOGS_MAX_TOTAL_MB = 20


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
        # Commercial mode also skips the engine's request logging, so nothing it writes carries a body.
        "commercial-mode": True,
        # The engine's own log goes to a rotating logs/main.log in its instance directory instead of
        # nowhere, bounded in total, so an engine-side stream failure can be diagnosed afterwards.
        "logging-to-file": True,
        "logs-max-total-size-mb": ENGINE_LOGS_MAX_TOTAL_MB,
        "request-log": False,
        "usage-statistics-enabled": False,
        "redis-usage-queue-retention-seconds": 60,
        "proxy-url": "",
        "force-model-prefix": True,
        # Upstream response headers reach Avibe, which is how an upstream's Retry-After on an
        # error arrives at all (#2666). They stop at Avibe: the gateway builds every response
        # it sends from its own headers, and reads only ENGINE_RESPONSE_HEADER_ALLOW_LIST.
        "passthrough-headers": True,
        "request-retry": 0,
        "max-retry-credentials": 1,
        "max-retry-interval": 0,
        "disable-cooling": True,
        "disable-claude-cloak-mode": True,
        # Forward exactly the tools a client declares: never add the hosted image
        # tool, never strip one. Vibey declares it only when the user enabled it;
        # Codex CLI drops a hosted image call it did not ask for (vibey-hosted-tools.md §7.1).
        "disable-image-generation": "passthrough",
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


# How CLIProxyAPI's own catalog registers a Claude model that takes a token budget (registry/models/models.json).
_CLAUDE_BUDGET_THINKING: Final = {"min": 1024, "max": 128000, "zero-allowed": True}


def _thinking_support(source: SourceRecord, model: str, reasoning_efforts: tuple[str, ...]) -> dict[str, Any]:
    """The thinking a model is registered with in the engine, by the kind it takes upstream.

    CLIProxyAPI reads ``levels`` as adaptive thinking on its Claude path: a budget
    request to such a model goes up as ``adaptive`` with an effort, which Claude 4.5
    and older refuse. So a Claude model on the Anthropic protocol that takes a token
    budget registers a budget range, as the engine's own catalog does; every other
    model keeps its levels, strongest first (#2737).
    """

    if source.protocol == "anthropic" and "claude" in model.lower() and not uses_adaptive_thinking(model):
        return dict(_CLAUDE_BUDGET_THINKING)
    return {"levels": list(reversed(reasoning_efforts))}


def _append_source(
    payload: dict[str, Any],
    source: SourceRecord,
    store: EngineStateStore,
    generation: str = "0",
) -> None:
    if source.protocol == "search":
        # Tool-only Sources are owned by Model Hub's search adapter, never by
        # CLIProxyAPI. They must not cause a provider or model registration.
        return
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
    images = image_model_ids(source)
    # A Google Source's image models live on the internal OpenAI-compatible entry below, not the Gemini one.
    chat_images = () if source.protocol == "google" else images
    routed = tuple(dict.fromkeys((*source.model_ids, *source.route_model_ids, *chat_images)))
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
            entry["thinking"] = _thinking_support(source, model, reasoning_efforts)
        if model in text_only:
            entry["input-modalities"] = ["text"]
        if model in images and source.protocol == "openai_chat":
            # An OpenAI-compatible entry serves the images endpoints only for a model it flags
            # (CPA config_types.go OpenAICompatibilityModel.Image); gpt-image names pass unflagged.
            entry["image"] = True
        models.append(entry)
    if source.protocol == "google":
        base_url = source.base_url or official_api_key_base_url(source.vendor)
        if not base_url:
            raise EngineStateError("Google source requires a base URL")
        root = google_api_base_url(base_url)
        # CPA (MIT), internal/config/config_types.go: GeminiKey/GeminiModel,
        # pinned c404af96. The executor, not Avibe, owns protocol conversion.
        entry = {
            "api-key": api_key,
            "prefix": source.prefix,
            "base-url": root,
        }
        if models:
            # A Gemini Source names a model as Google lists it (``models/<id>``); the executor puts its name in the
            # upstream path itself, so the name is the bare id and the listed one is the alias it is routed by.
            entry["models"] = [{**model, "name": model["name"].removeprefix("models/")} for model in models]
        payload.setdefault("gemini-api-key", []).append(entry)
        if images:
            # The engine's images endpoints serve an image model only from an OpenAI-compatible entry that flags it
            # (CPA openai_images_handlers.go), so the same key also registers its image models on Google's
            # OpenAI-compatible root, under the Source's own prefix: still one Source and one credential.
            payload.setdefault("openai-compatibility", []).append(
                {
                    "name": f"{source.prefix}-images",
                    "prefix": source.prefix,
                    "base-url": f"{root}/v1beta/openai",
                    "api-key-entries": [{"api-key": api_key}],
                    "models": [
                        {"name": model, "alias": model, "display-name": reload_display_name(model, generation),
                         "image": True}
                        for model in images
                    ],
                }
            )
        return
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
        if models and source.vendor == "xai":
            # CPA (MIT) v8.0.23, internal/runtime/executor/openai_responses_signature.go: a replayed
            # reasoning item keeps an encrypted_content without OpenAI's Fernet envelope, and its id,
            # only for an is-compat model. xAI's ciphertext has no envelope, so without the flag the
            # engine strips both and xAI loses its reasoning on every replay. The flag also replays
            # reasoning content as xAI returned it and drops Codex multi-agent fields xAI never takes.
            models = [{**model, "is-compat": True} for model in models]
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


def registered_model_ids(source: SourceRecord) -> tuple[str, ...]:
    """The model ids an API-key Source registers in the rendered YAML, each routed as ``prefix/model``."""
    return tuple(dict.fromkeys((*source.model_ids, *source.route_model_ids, *image_model_ids(source))))


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
        for model in registered_model_ids(source):
            expected[f"{source.prefix}/{model}"] = reload_display_name(model, generation)
    return expected


def image_model_ids(source: SourceRecord) -> tuple[str, ...]:
    """The vendor's image models an API-key Source's engine entry registers, so ``image_generate`` can route them.

    Only models whose route was verified on API keys, on a protocol whose engine
    entry serves the images endpoints; on a ``custom`` Source, only those it lists.
    """

    if source.protocol not in IMAGE_PROTOCOLS:
        return ()
    return tuple(
        spec.id
        for spec in source_image_models(source.vendor, source.protocol, source.model_ids)
        if "api_key" in spec.kinds
    )


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
