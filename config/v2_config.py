import copy
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import (
    Callable,
    ClassVar,
    Iterator,
    List,
    Literal,
    Mapping,
    Optional,
    TypeVar,
    Union,
    get_args,
)
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from config import paths
from config.atomic_io import write_atomic
from config.platform_registry import (
    WORKBENCH_PLATFORM_ID,
    get_platform_descriptor,
    is_workbench_platform,
    platform_catalog_payload,
    platform_descriptors,
    supported_platform_ids,
    supported_platform_set,
)
from modules.agents.catalog import DEFAULT_AGENT_BACKEND
from modules.im.base import BaseIMConfig
from vibe.i18n import normalize_language
from vibe.trace_retention_policy import validate_retention_days

logger = logging.getLogger(__name__)

CONFIG_LOCK = threading.RLock()

#: How long a config write waits for a peer process before giving up. Config
#: writes are small and bounded, so a wait this long says the holder is stuck
#: rather than busy, and a settings save that fails is better than one that
#: never returns.
CONFIG_FILE_LOCK_TIMEOUT_SECONDS = 30.0


def _path_file_lock(lock_path: Path, *, timeout_seconds: float | None):
    """The shared cross-process lock for one path.

    Nesting is safe, and that is why this file no longer keeps a lock of its
    own: ``MigrationFileLock`` is re-entrant per path and thread, which both
    transactions here used to hand-roll as a thread-local depth counter beside
    a private descriptor and a private copy of the platform lock calls.
    """

    # Import lazily because storage's package initializer imports V2Config.
    from storage.lock import MigrationFileLock

    return MigrationFileLock(lock_path, timeout_seconds=timeout_seconds)


@contextmanager
def _memory_config_transaction(config_path: Path) -> Iterator[None]:
    """Narrow cross-process exclusive section for Memory candidate+marker writes.

    UI and Controller run in different processes. ``CONFIG_LOCK`` is process-local,
    so settlement and a concurrent settings save need a shared file lock around
    the durable Memory unit. This is not a general multi-writer config service.

    Lock order is always ``CONFIG_LOCK`` then the file lock, matching existing
    callers that already hold ``CONFIG_LOCK`` when they enter ``save_config``.
    Nesting re-enters both: ``MigrationFileLock`` is re-entrant per path and
    thread, which is what this used to hand-roll as a thread-local depth counter
    beside its own descriptor and its own platform lock calls.
    """

    lock_path = config_path.parent / "memory-config.tx.lock"
    # CONFIG_LOCK first so threads that already hold it (remote_access/settings
    # helpers) never wait on the file lock while another waiter holds the file
    # lock and waits for CONFIG_LOCK.
    with CONFIG_LOCK:
        # Unbounded, as this always was: the section holds a config read-modify-
        # write, so the only way to wait forever is for a live peer to still be
        # inside one.
        with _path_file_lock(lock_path, timeout_seconds=None):
            yield


@contextmanager
def config_write_transaction(config_path: Optional[Path] = None) -> Iterator["V2Config"]:
    """Cross-process read-modify-write transaction for ``config.json`` (#1458).

    Generalizes the Memory transaction: the same file lock serializes the
    WHOLE load→mutate→save cycle, so the mutator always sees a snapshot
    loaded inside the transaction and cannot revert a concurrent writer's
    fields (the stale-snapshot race ``CONFIG_LOCK`` cannot fix because it
    is process-local while the UI API and controller are separate
    processes). Lock order and re-entrancy match the Memory transaction:
    ``CONFIG_LOCK`` first, then the file lock; both are re-entrant, so a
    nested transaction on the same config takes the same pair.

    Yields a freshly loaded ``V2Config``; mutate it in place and it is
    saved on clean context exit. Mutator exceptions abort with no write.
    """

    from config import paths as _paths

    path = config_path or _paths.get_config_path()
    with config_file_lock(path):
        try:
            config = V2Config.load(path)
        except FileNotFoundError:
            config = V2Config.default()
        yield config
        config.save(path)


def update_config_fields(
    mutator: Callable[["V2Config"], None],
    *,
    config_path: Optional[Path] = None,
) -> "V2Config":
    """Apply ``mutator`` to a fresh config under the cross-process lock.

    The preferred shape for narrow field writers (relay marker, language,
    default provider, secrets rotation): decisions are made before the
    call, the mutator only assigns fields, and the transaction guarantees
    neither snapshot staleness nor lost updates. Direct
    ``V2Config.load`` → mutate → ``save()`` pairs outside a transaction
    are a defect for cross-process state (see docs/plans/
    config-cross-process-write-serialization.md).
    """

    with config_write_transaction(config_path) as config:
        mutator(config)
    return config


_MODEL_HUB_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|rk|pk|sess|token)[-_][a-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(?:authorization|api[_ -]?key|access[_ -]?token)\s*[:=]\s*"
        r"(?:sk[-_][a-z0-9_-]{8,}|[a-z0-9._~+/=-]{16,})"
    ),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"),
)


def _contains_model_hub_credential_material(value: object) -> bool:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return any(pattern.search(rendered) for pattern in _MODEL_HUB_CREDENTIAL_PATTERNS)

DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS = 600
DEFAULT_SHOW_PAGE_API_TIMEOUT_SECONDS = 90.0

# Harness run staleness sweep (``docs/plans/agent-run-zombie-settlement.md`` §4.4).
# How often the sweep may run at all — it rides the scheduler's existing 2 s tick, so
# this only rate-limits the candidate query.
DEFAULT_HARNESS_RUN_SWEEP_INTERVAL_SECONDS = 60
# How long a ``running`` agent run may have no live owner before it is declared
# orphaned. Must comfortably exceed the gap between claiming a row and registering the
# executing turn, so a legitimately-starting run is never swept.
DEFAULT_HARNESS_RUN_ORPHAN_GRACE_SECONDS = 120
# How long a ``queued`` run may sit while its transport is unavailable. Long enough
# that a brief IM reconnect just delivers late instead of failing the run.
DEFAULT_HARNESS_RUN_QUEUED_TTL_SECONDS = 1800
# How long a workbench queue hold may go unrefreshed. Deliberately the longest of the
# three: a session actively recovering its queue keeps touching the row.
DEFAULT_HARNESS_RUN_HOLD_TTL_SECONDS = 3600

# Absolute-time backstop for evicting a Codex transport whose turn is stuck
# "active" forever (e.g. the ``codex app-server`` wedged or silently
# disconnected after ``turn/start`` but before ``turn/completed``, so
# ``_active_turns`` is never cleared). Without this, ``evict_idle_transports``
# treats an active turn as an ABSOLUTE veto and the wedged app-server process
# leaks until service restart (mirrors the Claude leak in #622/#623).
#
# A transport with an active turn is force-evicted once it has been idle for
# ``max(idle_timeout * MULTIPLIER, FLOOR_SECONDS)``. Set the multiplier <= 0 to
# disable the backstop entirely.
#
# TRADE-OFF: this cap is driven purely by ``last_activity`` (refreshed on every
# Codex notification), so it CANNOT distinguish a genuinely wedged turn from a
# legitimately long, fully-silent one. A single tool/MCP run or model "thinking"
# phase that emits no notifications for longer than the cap will be misjudged as
# stuck and have its transport torn down. The multiplier defaults higher than a
# typical tool-run assumption, and the floor guarantees a >= 30 min window even
# when ``idle_timeout`` is configured small, to keep that false-positive rare.
DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER = 3
DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_FLOOR_SECONDS = 1800

# Absolute-age backstop for idle eviction. A Claude session that is still
# flagged ``active`` (its per-turn receiver never released the flag, e.g. a
# long-lived receiver blocked on ``receive_messages`` with no stream EOF) is
# force-evicted once its ``last_activity`` is older than
# ``max(idle_timeout * STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER, FLOOR_SECONDS)``.
# This decouples eviction from the receiver's flag-release logic, so a
# stuck-active session can no longer pin its ~220MB ``claude`` subprocess until
# the next service restart. A genuine in-flight turn keeps touching
# ``last_activity`` (assistant/tool messages), so it normally stays well under
# this cap. Set the multiplier to 0 to disable the backstop.
#
# Trade-off: ``last_activity`` is only refreshed when an SDK message arrives.
# Because a stuck (blocked-receiver) session and a session running a single
# silent tool call are indistinguishable from ``last_activity`` alone, a real
# turn whose ONE tool invocation runs silently for longer than
# the cap would be force-evicted mid-turn. The default cap is at least 30min
# because Claude Code's Bash tool caps at 10min, so a single 30min-silent turn
# is not expected in practice; raise the multiplier if your deployment runs
# longer silent tools (e.g. long builds via custom/MCP tools that emit no
# intermediate messages).
DEFAULT_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER = 3
DEFAULT_STUCK_ACTIVE_IDLE_EVICTION_FLOOR_SECONDS = 1800
DEFAULT_OPENCODE_ERROR_RETRY_LIMIT = 1
# OpenCode >= 1.18.x bounds its own provider retries (RETRY_MAX_RETRIES = 5) and
# writes the exhausted error onto the assistant message, which the poll loop's
# error-driven settlement already turns into a failed terminal result. The
# historical wall-clock cap from #1197 therefore default-offs here: a positive
# value is an explicit opt-in for operators who still want a hard bound.
DEFAULT_OPENCODE_ACTIVE_TURN_TIMEOUT_SECONDS = 0
# The shipped default before the opt-in change. A settings save materializes the
# whole ``agents.opencode`` section, so a persisted value equal to this constant
# is the old default's echo rather than an operator's choice; load-time
# migration neutralizes exactly that value. Any other value is an explicit
# choice and survives the migration.
LEGACY_DEFAULT_OPENCODE_ACTIVE_TURN_TIMEOUT_SECONDS = 90 * 60
DEFAULT_CHAT_MESSAGE_FONT_SIZE_PX = 14
MIN_CHAT_MESSAGE_FONT_SIZE_PX = 12
MAX_CHAT_MESSAGE_FONT_SIZE_PX = 20
DEFAULT_AGENT_PROGRESS_STYLE = "off"
MODEL_HUB_ENABLED_ENV = "VIBE_MODEL_HUB_ENABLED"
MODEL_HUB_BACKENDS = ("claude", "codex", "opencode")
MODEL_HUB_LEGACY_CREATED_AT = "1970-01-01T00:00:00Z"
_LEGACY_CLAUDE_FAMILY_ALIASES = {
    "opus": "opus",
    "opus[1m]": "opus",
    "sonnet": "sonnet",
    "sonnet[1m]": "sonnet",
    "haiku": "haiku",
}
_LEGACY_CLAUDE_MODEL_ID = re.compile(
    r"^claude-(?P<family>opus|sonnet|haiku|fable)-(?P<version>\d+(?:-\d+)*?)(?:-(?P<date>\d{8}))?$"
)


def normalize_model_hub_vendor_id(value: object) -> str:
    """Return the canonical persisted vendor id used by matching-v1."""

    if not isinstance(value, str):
        raise ValueError("Config 'model_hub.sources.vendor' must be a non-empty string")
    vendor = value.strip().lower()
    if (
        not vendor
        or re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", vendor) is None
        or _contains_model_hub_credential_material(vendor)
    ):
        raise ValueError("Config 'model_hub.sources.vendor' is invalid")
    return vendor


def canonical_opencode_menu_identity(identifier: object) -> tuple[str, str]:
    """Validate and split one persisted OpenCode ``provider/model`` identity."""

    from core.handlers.model_hub.identifiers import parse_opencode_model_id

    if not isinstance(identifier, str) or identifier != identifier.strip():
        raise ValueError("Invalid OpenCode model identifier")
    provider_id, model_id = parse_opencode_model_id(identifier)
    if (
        re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", provider_id) is None
        or provider_id != provider_id.strip()
        or model_id != model_id.strip()
    ):
        raise ValueError("Invalid OpenCode model identifier")
    if _contains_model_hub_credential_material(identifier):
        raise ValueError("OpenCode model identifier contains credential material")
    return provider_id, model_id


def model_hub_fixed_menu_ids(backend: str) -> tuple[str, ...]:
    """Return the bundled fixed-menu ids used by persisted Hub routes."""

    if backend not in {"claude", "codex"}:
        return ()
    from vibe.backend_model_catalog import backend_model_entries, load_bundled_catalog

    return tuple(
        entry["id"]
        for entry in backend_model_entries(backend, load_bundled_catalog())
    )


def _migrate_fixed_menu_routes_on_load(payload: dict) -> dict:
    """Adapt fixed-menu route keys to the current bundled catalog on reload.

    Newly introduced menu ids receive empty routes. Legacy route keys remain
    intact so the canonical agent loader can preserve them as manual catalog
    rows; matching and placement never run here.
    """

    model_hub = payload.get("model_hub")
    if not isinstance(model_hub, dict):
        return payload
    agents = model_hub.get("agents")
    if not isinstance(agents, dict):
        return payload

    migrated_agents = dict(agents)
    changed = False
    for backend in ("claude", "codex"):
        raw_agent = agents.get(backend)
        if not isinstance(raw_agent, dict):
            continue
        if "models" in raw_agent:
            continue
        routes = raw_agent.get("routes")
        expected_menu_ids = model_hub_fixed_menu_ids(backend)
        if not isinstance(routes, dict) or not expected_menu_ids:
            continue
        migrated_routes = {
            model_id: routes.get(model_id, {"hops": []})
            for model_id in expected_menu_ids
        }
        migrated_routes.update(
            (model_id, route)
            for model_id, route in routes.items()
            if model_id not in migrated_routes
        )
        if migrated_routes == routes:
            continue
        migrated_agent = dict(raw_agent)
        migrated_agent["routes"] = migrated_routes
        migrated_agents[backend] = migrated_agent
        changed = True

    if not changed:
        return payload
    migrated_model_hub = dict(model_hub)
    migrated_model_hub["agents"] = migrated_agents
    migrated_payload = dict(payload)
    migrated_payload["model_hub"] = migrated_model_hub
    return migrated_payload


def _legacy_source_eligible_for_backend(source: object, backend: str) -> bool:
    if not isinstance(source, dict):
        return False
    if source.get("kind") == "api_key":
        return source.get("supply_channel") == "hub"
    expected_backend = {"anthropic": "claude", "openai": "codex"}.get(source.get("vendor"))
    return expected_backend == backend


def _legacy_recommended_source_order(
    sources: dict[str, dict],
    backend: str,
) -> list[str]:
    def sort_key(source: dict) -> tuple[object, ...]:
        if source.get("kind") == "subscription":
            return (
                0,
                0 if source.get("supply_channel") == "native_cli" else 1,
                str(source.get("id") or ""),
            )
        created_at = str(source.get("created_at") or MODEL_HUB_LEGACY_CREATED_AT)
        try:
            created_timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            created_timestamp = 0
        return (1, created_timestamp, str(source.get("id") or ""))

    return [
        str(source.get("id"))
        for source in sorted(sources.values(), key=sort_key)
        if _legacy_source_eligible_for_backend(source, backend)
    ]


def _legacy_source_order(
    model_hub: dict,
    sources: dict[str, dict],
    agent: dict,
    backend: str,
) -> list[str]:
    source_settings = agent.get("sources")
    if isinstance(source_settings, dict):
        policy = source_settings.get("policy")
        order = source_settings.get("order")
        if policy is None and isinstance(order, list):
            return [
                source_id
                for source_id in order
                if isinstance(source_id, str)
                and source_id in sources
                and _legacy_source_eligible_for_backend(sources[source_id], backend)
            ]
        if policy == "custom" and isinstance(order, list):
            return [
                source_id
                for source_id in order
                if isinstance(source_id, str)
                and source_id in sources
                and _legacy_source_eligible_for_backend(sources[source_id], backend)
            ]
        if policy == "follow":
            return _legacy_recommended_source_order(sources, backend)

    priority_order = model_hub.get("priority_order")
    if isinstance(priority_order, list):
        ordered = [
            source_id
            for source_id in priority_order
            if isinstance(source_id, str)
            and source_id in sources
            and _legacy_source_eligible_for_backend(sources[source_id], backend)
        ]
        if ordered:
            return ordered
    return _legacy_recommended_source_order(sources, backend)


def _legacy_source_order_setting_is_valid(value: object, *, required: bool = False) -> bool:
    """Validate the pre-v5 source-order shape before migration can infer hops."""

    if not isinstance(value, dict):
        return False
    if set(value) - {"policy", "order"}:
        return False
    policy = value.get("policy")
    if policy is not None and not isinstance(policy, str):
        return False
    if policy not in {None, "custom", "follow"}:
        return False
    if required and not isinstance(value.get("order"), list):
        return False
    order = value.get("order")
    if order is None:
        return not required
    return (
        isinstance(order, list)
        and all(isinstance(source_id, str) for source_id in order)
        and len(order) == len(set(order))
    )


def _legacy_mapping_is_valid(value: object) -> bool:
    """Match the pre-v5 mapping parser's required fields and types."""

    if (
        not isinstance(value, dict)
        or set(value) - {"builtin_id", "target_model_id", "enabled"}
        or not isinstance(value.get("builtin_id"), str)
        or not value["builtin_id"]
        or not isinstance(value.get("enabled"), bool)
    ):
        return False
    target_model_id = value.get("target_model_id")
    if value["enabled"]:
        return isinstance(target_model_id, str) and bool(target_model_id)
    return target_model_id is None or isinstance(target_model_id, str)


def _legacy_claude_matching_model_id(source: dict, requested_model: str) -> str | None:
    """Resolve the pre-v5 Claude aliases against discovered native models.

    Claude's old native CLI resolver treated ``opus``/``sonnet``/``haiku`` as
    family selectors and persisted the newest concrete model it observed. Keep
    that behavior at the disk migration boundary; the current runtime resolver
    intentionally only follows persisted exact hops.
    """

    if source.get("vendor") != "anthropic":
        return None
    observed_models = [
        model
        for model in source.get("models") or []
        if isinstance(model, dict)
        and model.get("origin", model.get("provenance")) == "discovered"
    ]
    family = _LEGACY_CLAUDE_FAMILY_ALIASES.get(requested_model)
    requested_version: tuple[int, ...] | None = None
    match = _LEGACY_CLAUDE_MODEL_ID.fullmatch(requested_model)
    requested_date: int | None = None
    if family is None and match is not None:
        family = match.group("family")
        requested_version = tuple(int(part) for part in match.group("version").split("-"))
        requested_date = int(match.group("date")) if match.group("date") else None
    if family is None:
        return None
    if requested_date is not None:
        return next(
            (
                model.get("id")
                for model in observed_models
                if model.get("id") == requested_model
            ),
            None,
        )

    matches: list[tuple[tuple[int, ...], int, str]] = []
    for model in observed_models:
        model_id = model.get("id")
        if not isinstance(model_id, str):
            continue
        parsed = _LEGACY_CLAUDE_MODEL_ID.fullmatch(model_id)
        if parsed is None or parsed.group("family") != family:
            continue
        version = tuple(int(part) for part in parsed.group("version").split("-"))
        if requested_version is not None and version != requested_version:
            continue
        date = int(parsed.group("date")) if parsed.group("date") else 0
        matches.append((version, date, model_id))
    return max(matches)[2] if matches else None


def _legacy_route_hops(
    sources: dict[str, dict],
    source_order: list[str],
    backend: str,
    target_model_id: str,
) -> list[dict[str, str]]:
    provider: Optional[str] = None
    bare_target = backend == "opencode" and "/" not in target_model_id
    if backend == "opencode":
        if not bare_target:
            try:
                provider, target_model_id = canonical_opencode_menu_identity(target_model_id)
            except ValueError:
                return []
    hops: list[dict[str, str]] = []
    bare_matches: list[dict[str, str]] = []
    for source_id in source_order:
        source = sources[source_id]
        legacy_claude_model_id = (
            _legacy_claude_matching_model_id(source, target_model_id)
            if backend == "claude"
            else None
        )
        if legacy_claude_model_id is not None:
            hops.append({"source_id": source_id, "model_id": legacy_claude_model_id})
            continue
        for model in source.get("models") or []:
            if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                continue
            model_id = model["id"]
            if provider is not None:
                try:
                    source_provider, source_model_id = canonical_opencode_menu_identity(
                        f"{source.get('vendor') or ''}/{model_id}"
                    )
                except ValueError:
                    try:
                        source_provider, source_model_id = canonical_opencode_menu_identity(
                            f"custom/{model_id}"
                        )
                    except ValueError:
                        continue
                if (source_provider, source_model_id) != (provider, target_model_id):
                    continue
                hops.append({"source_id": source_id, "model_id": model_id})
                continue
            if bare_target:
                try:
                    _, source_model_id = canonical_opencode_menu_identity(
                        f"{source.get('vendor') or ''}/{model_id}"
                    )
                except ValueError:
                    try:
                        _, source_model_id = canonical_opencode_menu_identity(
                            f"custom/{model_id}"
                        )
                    except ValueError:
                        continue
                if source_model_id == target_model_id or source_model_id.endswith(
                    f"/{target_model_id}"
                ):
                    bare_matches.append({"source_id": source_id, "model_id": model_id})
                continue
            if model_id == target_model_id:
                hops.append({"source_id": source_id, "model_id": model_id})
    if bare_target and len(bare_matches) == 1:
        return bare_matches
    return hops


def _migrate_legacy_model_hub_payload(payload: dict) -> tuple[dict, bool, tuple[str, ...]]:
    """Convert the pre-v5 Model Hub shape before the strict parser runs.

    The final parser intentionally rejects retired fields. Disk loading is the
    compatibility boundary, so old ``mappings`` are converted into exact route
    hops when the old model inventory identifies their source unambiguously.
    Unrepresentable mappings remain in the untouched backup and produce a load
    warning instead of silently selecting a different source.
    """

    model_hub = payload.get("model_hub")
    if not isinstance(model_hub, dict):
        return payload, False, ()
    agents = model_hub.get("agents")
    if not isinstance(agents, dict):
        return payload, False, ()
    if set(agents) - set(MODEL_HUB_BACKENDS):
        return payload, False, ()
    raw_sources = model_hub.get("sources")
    if raw_sources is not None and not isinstance(raw_sources, list):
        return payload, False, ()
    if "priority_order" in model_hub and not _legacy_source_order_setting_is_valid(
        {"order": model_hub["priority_order"]}
    ):
        return payload, False, ()
    if (
        "subscription_hub_experimental" in model_hub
        and not isinstance(model_hub["subscription_hub_experimental"], bool)
    ):
        return payload, False, ()
    legacy_sources_by_id = {
        source.get("id"): source
        for source in raw_sources or []
        if isinstance(source, dict) and isinstance(source.get("id"), str)
    }
    if "priority_order" in model_hub and any(
        source_id not in legacy_sources_by_id
        for source_id in model_hub["priority_order"]
    ):
        return payload, False, ()
    for source in raw_sources or []:
        if not isinstance(source, dict):
            return payload, False, ()
        models = source.get("models")
        if "models" in source and not isinstance(models, list):
            return payload, False, ()
        if isinstance(models, list) and any(not isinstance(model, dict) for model in models):
            return payload, False, ()
        for model in models or []:
            if "provenance" not in model:
                continue
            provenance = model["provenance"]
            if not isinstance(provenance, str) or provenance not in {"discovered", "manual"}:
                return payload, False, ()
            if "origin" in model and model["origin"] != provenance:
                return payload, False, ()
        if "experimental_consent_at" in source:
            try:
                _validate_optional_datetime(
                    source["experimental_consent_at"],
                    "model_hub.sources.experimental_consent_at",
                )
            except (TypeError, ValueError):
                return payload, False, ()
    for backend, agent in agents.items():
        if not isinstance(agent, dict):
            return payload, False, ()
        if set(agent) - {
            "backend",
            "mode",
            "menu_kind",
            "sources",
            "mappings",
            "routes",
            "menu",
            "models",
        }:
            return payload, False, ()
        expected_menu_kind = "open" if backend == "opencode" else "fixed"
        if agent.get("backend") != backend or agent.get("menu_kind") != expected_menu_kind:
            return payload, False, ()
        if "mode" in agent:
            mode = agent["mode"]
            if not isinstance(mode, str) or mode not in {"hub", "direct"}:
                return payload, False, ()
        mappings = agent.get("mappings")
        if "mappings" in agent and not isinstance(mappings, list):
            return payload, False, ()
        if isinstance(mappings, list) and any(
            not _legacy_mapping_is_valid(mapping) for mapping in mappings
        ):
            return payload, False, ()
        if backend in {"claude", "codex"}:
            fixed_menu_ids = set(model_hub_fixed_menu_ids(backend))
            if any(
                mapping["enabled"] and mapping["builtin_id"] not in fixed_menu_ids
                for mapping in mappings or []
            ):
                return payload, False, ()
        if "routes" in agent and not isinstance(agent["routes"], dict):
            return payload, False, ()
        menu = agent.get("menu")
        if isinstance(menu, dict) and "checked" in menu and not isinstance(menu["checked"], list):
            return payload, False, ()
        if "sources" in agent:
            source_settings = agent["sources"]
            if not _legacy_source_order_setting_is_valid(
                source_settings,
                required=isinstance(source_settings, dict)
                and source_settings.get("policy") == "custom",
            ):
                return payload, False, ()
            if (
                isinstance(source_settings, dict)
                and source_settings.get("policy") in {None, "custom"}
                and isinstance(source_settings.get("order"), list)
                and any(
                    source_id not in legacy_sources_by_id
                    or not _legacy_source_eligible_for_backend(
                        legacy_sources_by_id[source_id],
                        backend,
                    )
                    for source_id in source_settings["order"]
                )
            ):
                return payload, False, ()
    has_legacy_shape = bool(
        {"priority_order", "subscription_hub_experimental"} & set(model_hub)
        or any(isinstance(agent, dict) and "mappings" in agent for agent in agents.values())
        or any(
            isinstance(source, dict)
            and "experimental_consent_at" in source
            for source in raw_sources or []
        )
        or any(
            isinstance(source, dict)
            and any(
                isinstance(model, dict)
                and ("provenance" in model or "reasoning_efforts" not in model)
                for model in source.get("models") or []
            )
            for source in raw_sources or []
        )
    )
    if not has_legacy_shape:
        return payload, False, ()

    migrated_payload = copy.deepcopy(payload)
    migrated_model_hub = migrated_payload["model_hub"]
    migrated_model_hub.pop("priority_order", None)
    migrated_model_hub.pop("subscription_hub_experimental", None)

    raw_sources = migrated_model_hub.get("sources") or []
    sources_by_id: dict[str, dict] = {}
    for source in raw_sources:
        if not isinstance(source, dict):
            continue
        source.pop("experimental_consent_at", None)
        for model in source.get("models") or []:
            if not isinstance(model, dict):
                continue
            if "provenance" in model:
                if "origin" not in model:
                    model["origin"] = model["provenance"]
                model.pop("provenance", None)
            if "reasoning_efforts" not in model:
                model["reasoning_efforts"] = []
        source_id = source.get("id")
        if isinstance(source_id, str):
            sources_by_id[source_id] = source

    warnings: list[str] = []
    migrated_agents: dict[str, object] = {}
    for backend in MODEL_HUB_BACKENDS:
        raw_agent = agents.get(backend)
        if not isinstance(raw_agent, dict):
            continue

        agent = copy.deepcopy(raw_agent)
        source_order = _legacy_source_order(migrated_model_hub, sources_by_id, agent, backend)
        source_settings = {"order": source_order}
        route_ids: list[str]
        if backend in {"claude", "codex"}:
            route_ids = list(model_hub_fixed_menu_ids(backend))
        else:
            menu = agent.get("menu")
            checked = menu.get("checked") if isinstance(menu, dict) else []
            route_ids = [item for item in checked if isinstance(item, str)]

        if isinstance(agent.get("routes"), dict):
            routes = copy.deepcopy(agent["routes"])
        else:
            old_mappings = agent.get("mappings")
            mappings = old_mappings if isinstance(old_mappings, list) else []
            # The legacy resolver selected the first enabled mapping in list
            # order. Disabled duplicates must not shadow an earlier active one.
            mapping_by_menu: dict[str, dict] = {}
            for item in mappings:
                if not isinstance(item, dict) or item.get("enabled") is not True:
                    continue
                builtin_id = item.get("builtin_id")
                if isinstance(builtin_id, str) and builtin_id not in mapping_by_menu:
                    mapping_by_menu[builtin_id] = item
            if backend == "opencode":
                route_ids.extend(
                    builtin_id
                    for builtin_id, mapping in mapping_by_menu.items()
                    if builtin_id not in route_ids and mapping.get("enabled") is True
                )
            routes = {}
            for model_id in route_ids:
                mapping = mapping_by_menu.get(model_id)
                target_model_id = model_id
                if isinstance(mapping, dict) and mapping.get("enabled") is True:
                    target = mapping.get("target_model_id")
                    if isinstance(target, str) and target:
                        target_model_id = target
                hops = _legacy_route_hops(
                    sources_by_id,
                    source_order,
                    backend,
                    target_model_id,
                )
                if isinstance(mapping, dict) and mapping.get("enabled") is True and not hops:
                    warnings.append(
                        f"Model Hub route {backend}/{model_id} could not be mapped to a persisted source model"
                    )
                routes[model_id] = {"hops": hops}

        allowed_agent = {
            key: value
            for key, value in agent.items()
            if key in {"backend", "mode", "menu_kind", "menu", "models"}
        }
        allowed_agent["backend"] = backend
        agent_mode = agent.get("mode")
        allowed_agent["mode"] = agent_mode if isinstance(agent_mode, str) and agent_mode in {"hub", "direct"} else "direct"
        allowed_agent["menu_kind"] = "open" if backend == "opencode" else "fixed"
        allowed_agent["sources"] = source_settings
        allowed_agent["routes"] = routes
        migrated_agents[backend] = allowed_agent

    migrated_model_hub["agents"] = migrated_agents
    migrated_payload["model_hub"] = migrated_model_hub
    return migrated_payload, True, tuple(warnings)


def _migrate_opencode_active_turn_timeout_on_load(payload: dict) -> dict:
    """Neutralize the legacy wall-clock default echo on reload, once.

    Configs saved while the cap shipped carry
    ``agents.opencode.active_turn_timeout_seconds: 5400`` because a settings
    save materializes the whole section. That value names the old default, not
    an operator's choice, so exactly that value rewrites to the disabled
    default. ``legacy_turn_timeout_neutralized`` is the provenance marker: a
    config written under the opt-in semantics carries it, so a deliberate
    5400-second choice saved afterwards survives every later load. Idempotent:
    once rewritten (or never eligible) the pattern never matches again.
    """

    agents = payload.get("agents")
    if not isinstance(agents, dict):
        return payload
    opencode = agents.get("opencode")
    if not isinstance(opencode, dict):
        return payload
    if "legacy_turn_timeout_neutralized" in opencode:
        return payload
    if (
        opencode.get("active_turn_timeout_seconds")
        != LEGACY_DEFAULT_OPENCODE_ACTIVE_TURN_TIMEOUT_SECONDS
    ):
        return payload
    migrated = dict(payload)
    migrated_agents = dict(agents)
    migrated_opencode = dict(opencode)
    migrated_opencode["active_turn_timeout_seconds"] = (
        DEFAULT_OPENCODE_ACTIVE_TURN_TIMEOUT_SECONDS
    )
    migrated_opencode["legacy_turn_timeout_neutralized"] = True
    migrated_agents["opencode"] = migrated_opencode
    migrated["agents"] = migrated_agents
    return migrated


def _migrate_config_payload_on_load(payload: dict) -> tuple[dict, bool, tuple[str, ...]]:
    migrated, changed, warnings = _migrate_legacy_model_hub_payload(payload)
    migrated = _migrate_fixed_menu_routes_on_load(migrated)
    migrated = _migrate_opencode_active_turn_timeout_on_load(migrated)
    return migrated, changed, warnings


# The spellings a hand-edited config may use for a boolean, both sides in one
# place. ``""`` reads as off because ``test_show_agent_activity_defaults_off_and
# _round_trips`` states it; anything absent from here names no side at all.
_BOOL_SPELLINGS = {
    "1": True,
    "true": True,
    "yes": True,
    "on": True,
    "0": False,
    "false": False,
    "no": False,
    "off": False,
    "": False,
}


def _named_bool(path: str, value: object) -> bool:
    """The side a switch's value names, or a refusal.

    ``true``/``1``/``"yes"``/``"off"`` all name a side the caller meant, and
    ``_BOOL_SPELLINGS`` is the whole vocabulary in one place rather than a
    truthy list plus an implicit else. A value outside it names no side at all
    — ``"garbage"``, ``5``, ``[]`` — and is refused rather than read as false,
    which on a write path would answer 200 having stored the side the caller
    never asked for.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        spelled = _BOOL_SPELLINGS.get(value.strip().lower())
        if spelled is not None:
            return spelled
    elif isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"Config '{path}' must be a boolean")


def _named_value(path: str, declared: object, value: object) -> object:
    """The value a field's declared kind names, or a refusal.

    One vocabulary for every field this reaches, because the alternative is
    what the review found twice: each field open-coding its own ``int(...)`` /
    ``str(...)`` / truthy test, and each one deciding on its own whether a
    value it cannot read is an error or something to repair in place. Repair
    belongs to ``V2Config.load``, behind a backup and a warning; this states
    only what a value must name to be readable at all.

    A switch reads the spellings above. A number reads any spelling that names
    one, so a hand-edited ``"14"`` still means 14, and otherwise reads only a
    number it can store unchanged: a ``bool`` is refused because Python makes it
    an ``int`` and ``int(True)`` is a legal ``1``, an infinity and a NaN are
    refused because neither names a setting any field here can hold, and a
    fractional value posted to an integer field is refused because ``int(14.9)``
    is ``14`` — every one of them otherwise answers 200 having stored a setting
    the caller never sent. A string reads only a string: no
    number names a URL or a credential, and silently emptying one logs the
    operator out or unpairs the instance. Kinds outside this vocabulary —
    containers, nested sections — are left to the code that already
    understands them.
    """

    if isinstance(declared, str):
        text = declared
    elif isinstance(declared, type):
        text = declared.__name__
    else:
        # ``Optional[int]`` and friends are not classes, and their ``__name__``
        # is the bare ``"Optional"``, which would drop the kind being wrapped.
        text = str(declared)
    optional = re.fullmatch(r"(?:typing\.)?Optional\[(?:typing\.)?(.+)\]", text)
    if optional:
        if value is None:
            return None
        text = optional.group(1)
    if text == "bool":
        return _named_bool(path, value)
    if text in ("int", "float"):
        if isinstance(value, bool):
            raise ValueError(f"Config '{path}' must be a number")
        try:
            number = int(value) if text == "int" else float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            # ``OverflowError`` is how Python spells "this float names no
            # integer": ``int(float("inf"))`` raises it rather than
            # ``ValueError``, and JSON turns both a hand-edited ``1e309`` and
            # the ``Infinity`` an older release serialized back into that
            # float. Left uncaught it escapes recovery on the disk path and
            # answers 500 on the write path, so it is spelled here as what it
            # is — a value naming no number — instead of being caught wider.
            raise ValueError(f"Config '{path}' must be a number") from exc
        if isinstance(number, float) and not math.isfinite(number):
            # An infinity or a NaN survives ``float(...)`` and then behaves as
            # a setting: an infinite ASR timeout never expires, and a NaN loses
            # every range comparison so it clamps to whichever bound is written
            # first. Refused for the same reason as the rest — it names no
            # setting — rather than clamped, which would answer 200 having
            # stored a bound the caller never asked for. Only floats are
            # checked: an ``int`` is always finite, and asking ``math`` about a
            # large one would raise the overflow just caught above.
            raise ValueError(f"Config '{path}' must be a number")
        if isinstance(value, (int, float)) and number != value:
            # The conversion succeeded and changed the number. ``int(14.9)`` is
            # ``14``, so a posted font size of 14.9 answered 200 having stored a
            # size nobody asked for, and ``float`` of an integer past 2**53 lands
            # on its neighbour the same way. Both are the defect this whole
            # vocabulary exists to refuse — a value coerced into a legal setting
            # instead of refused — so the rule is stated as what it is: a number
            # this reads is the number it was given. Only a numeric input is
            # held to it. A string is a spelling, not a number, and reading
            # ``"14"`` as 14 is the normalization this deliberately keeps; a
            # fractional spelling has no route here anyway, because ``int`` of
            # ``"14.9"`` already raises above.
            raise ValueError(f"Config '{path}' must be a number")
        return number
    if text == "str":
        if not isinstance(value, str):
            raise ValueError(f"Config '{path}' must be a string")
    return value


def _refuse_values_naming_nothing(section: str, instance: object) -> None:
    """Hold every field of ``instance`` to the kind it declares.

    Stated over the declared fields rather than over the ones some reviewer
    reached, so a field added to the dataclass is held to its own declaration
    without editing this call site — which is the difference between closing
    this class and paying for it one review round per field.

    Raised one field at a time, in the ``Config '<section>.<field>'`` shape
    every other message on this path uses, because that is what lets a disk
    load recover the unreadable field alone instead of discarding the section.
    A second unreadable field simply raises on the next pass of the recovery
    loop.
    """

    for info in fields(instance):
        setattr(
            instance,
            info.name,
            _named_value(f"{section}.{info.name}", info.type, getattr(instance, info.name)),
        )


# Sections whose fields are independent of one another, so a single unreadable
# field is recovered on its own instead of discarding its siblings — and whose
# switches recover to off rather than to the fresh-install default. See
# ``_reset_recoverable_config_section``.
_FIELD_SCOPED_RECOVERY_SECTIONS = ("ui", "audio_asr")

# Retention is optional and destructive: a malformed field must disable only
# that policy while preserving the rest of the runtime settings (cwd, logging,
# resource governance, and Harness timeouts).
_RUNTIME_RETENTION_FIELDS = frozenset(
    {
        "agent_events_trace_retention_enabled",
        "agent_events_trace_retention_days",
    }
)

# The switch that decides whether an optional feature runs at all. Named once
# because recovery has to tell it apart from every other switch in a section:
# the rest describe how a feature behaves, this one decides whether it happens.
_FEATURE_SWITCH_FIELD = "enabled"


def _recovery_section_for_error(error: BaseException) -> Optional[str]:
    message = str(error)
    match = re.search(r"Config '([^']+)'", message)
    if match:
        path = match.group(1)
        if path == "memory.cloud" or path.startswith("memory.cloud."):
            return "memory.cloud"
        if path.startswith("memory.processing.rerank"):
            return "memory.rerank"
        if path.startswith("memory.processing.multimodal"):
            return "memory.multimodal"
        if path == "platform":
            return "platforms"
        if path.startswith("agents."):
            backend = path.split(".", 2)[1]
            if backend in MODEL_HUB_BACKENDS or backend == "avault":
                return f"agents.{backend}"
        return path.split(".", 1)[0]

    lowered = message.lower()
    if "memory rerank endpoint" in lowered:
        return "memory.rerank"
    if "memory multimodal endpoint" in lowered:
        return "memory.multimodal"
    if "unsupported enabled platform" in lowered:
        return "platforms"
    for platform_id in supported_platform_ids():
        if f"invalid {platform_id.lower()}" in lowered:
            return platform_id
    if any(
        marker in lowered
        for marker in (
            "source state",
            "hub sources",
            "engine credential ref",
            "native source",
            "api-key source",
            "manual model",
            "subscription source",
            "opencode model identifier",
        )
    ):
        return "model_hub"
    for section in (
        "model_hub",
        "memory",
        "remote_access",
        "audio_asr",
        "runtime",
        "agents",
        "platforms",
        "update",
        "ui",
    ):
        if section in lowered:
            return section
    return None


def _recovery_field_for_error(section: Optional[str], error: BaseException) -> Optional[str]:
    """The single field an error names, for the field-scoped sections only.

    Restricted to those sections so every other section keeps recovering — and
    de-duplicating — as a whole, which is what stops the recovery loop.
    """

    if section == "memory.cloud":
        match = re.search(r"Config '([^']+)'", str(error))
        if not match:
            return None
        path = match.group(1)
        prefix = "memory.cloud."
        if not path.startswith(prefix):
            return None
        return path[len(prefix) :].split(".", 1)[0]
    if section == "runtime":
        match = re.search(r"Config '([^']+)'", str(error))
        if not match:
            return None
        path = match.group(1)
        prefix = "runtime."
        if not path.startswith(prefix):
            return None
        field_name = path[len(prefix) :].split(".", 1)[0]
        return field_name if field_name in _RUNTIME_RETENTION_FIELDS else None
    if section not in _FIELD_SCOPED_RECOVERY_SECTIONS:
        return None
    match = re.search(r"Config '([^']+)'", str(error))
    if not match:
        return None
    path = match.group(1).split(".", 1)
    if len(path) != 2 or path[0] != section:
        return None
    return path[1]


def _recover_switch_section_field(
    payload: dict, section: str, field_name: Optional[str]
) -> bool:
    """Keep what the file still says, and leave a recovered feature switched off.

    Two questions, answered separately, because answering both the same way is
    what produced this function's two review rounds.

    *How much of what the operator wrote is discarded?* As little as possible.
    Replacing the whole section answers an unreadable font size by hiding the
    tool-call rows and an unreadable model name by forgetting the endpoint, so
    the unreadable field is recovered alone: a field declared ``bool`` resolves
    to ``False`` because a value naming no side is no evidence the feature was
    on, and any other field is dropped so its declared default applies.

    *Does an optional feature still run afterwards?* No. ``AGENTS.md`` states it
    — "a broken optional-feature section disables that feature and warns" — and
    the two directions are not the symmetric pair the previous round argued they
    were. Leaving ``audio_asr.enabled`` on runs transcription under a
    configuration nobody wrote, shipping user audio off-machine against
    substituted settings, and that cannot be taken back. Leaving it off costs a
    warning the operator reads before turning it back on. Only one of those is
    reversible, so any repair here clears the section's ``enabled`` switch.

    Both answers are stated over the section's declared fields rather than over
    the sections that exist today: a section with no ``enabled`` is presentation
    and keeps its siblings untouched, and one that declares it inherits the
    disable without editing this function.
    """

    declared = V2Config.__dataclass_fields__[section].default_factory
    declared_fields = {info.name for info in fields(declared)}
    switches = {info.name for info in fields(declared) if info.type in (bool, "bool")}
    stored = payload.get(section)
    if not isinstance(stored, dict):
        # Nothing readable to keep, so every switch resolves off.
        stored = {name: False for name in switches}
        payload[section] = stored
    elif field_name is None:
        stored.update({name: False for name in switches})
    elif field_name in switches:
        stored[field_name] = False
    elif field_name in declared_fields:
        stored.pop(field_name, None)
    else:
        return False
    if _FEATURE_SWITCH_FIELD in switches:
        stored[_FEATURE_SWITCH_FIELD] = False
    return True
    return False


def _memory_cloud_recovery_requires_managed_fence(payload: dict) -> bool:
    """Fail closed when a paired instance is not authoritatively personal."""

    remote_access = payload.get("remote_access")
    if not isinstance(remote_access, dict):
        return False
    vibe_cloud = remote_access.get("vibe_cloud")
    if not isinstance(vibe_cloud, dict):
        return False
    instance_kind = vibe_cloud.get("instance_kind")
    if instance_kind == "personal":
        return False
    if instance_kind == "organization":
        return True
    return bool(
        vibe_cloud.get("enabled") is True
        and isinstance(vibe_cloud.get("instance_id"), str)
        and vibe_cloud.get("instance_id", "").strip()
        and isinstance(vibe_cloud.get("instance_secret"), str)
        and vibe_cloud.get("instance_secret", "").strip()
    )


def _memory_cloud_recovery_requires_identity_fence(
    memory: dict,
    cloud: dict,
    *,
    managed_fence: bool,
) -> bool:
    """Never select a recovered cloud runtime without an applied identity."""

    applied_identity = cloud.get("applied_embedding_identity")
    if isinstance(applied_identity, str) and applied_identity.strip():
        return False
    return memory.get("mode") == "platform" or bool(
        managed_fence and cloud.get("organization_attached") is True
    )


def _recover_memory_cloud_section(payload: dict, field_name: Optional[str]) -> bool:
    """Recover one cloud-cache member without inventing recovery workflow state."""

    memory = payload.get("memory")
    if not isinstance(memory, dict):
        return False
    cloud = memory.get("cloud")
    managed_fence = _memory_cloud_recovery_requires_managed_fence(payload)
    if not isinstance(cloud, dict) or field_name is None:
        if managed_fence:
            cloud = {
                "scope": "organization",
                "organization_attached": True,
                "runtime_apply_pending": True,
            }
        else:
            cloud = {}
        memory["cloud"] = cloud
        if _memory_cloud_recovery_requires_identity_fence(
            memory,
            cloud,
            managed_fence=managed_fence,
        ):
            memory["repair_required"] = True
        return True

    cloud.pop(field_name, None)
    if field_name == "runtime_apply_pending":
        cloud["runtime_apply_pending"] = True
    elif field_name == "memory_llm_source":
        # A source mismatch means the cached effective LLM cannot be trusted.
        # Keep the rest of the cloud identity for diagnostics, but fail closed
        # until the next authoritative status refresh supplies a new pair.
        capabilities = cloud.get("capabilities")
        if isinstance(capabilities, dict):
            capabilities["memory_llm"] = False
    elif field_name == "applied_embedding_identity":
        live_identity = cloud.get("embedding_identity")
        if isinstance(live_identity, str) and live_identity.strip():
            cloud["applied_embedding_identity"] = live_identity
    elif field_name == "source_instance_id":
        cloud["capabilities"] = {}
        cloud["embedding_identity"] = None
        cloud["model_access_key"] = None
        cloud["proxy_base_url"] = None

    if managed_fence and field_name in {
        "scope",
        "organization_attached",
        "transition_notice_pending",
    }:
        cloud["scope"] = "organization"
        cloud["organization_attached"] = True
        cloud["transition_notice_pending"] = False
    if _memory_cloud_recovery_requires_identity_fence(
        memory,
        cloud,
        managed_fence=managed_fence,
    ):
        memory["repair_required"] = True
    return True


def _recover_runtime_field(payload: dict, field_name: Optional[str]) -> bool:
    """Repair one retention field without discarding the runtime section."""

    if field_name not in _RUNTIME_RETENTION_FIELDS:
        return False
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        return False
    # A recovered policy is disabled even if the other retention field looked
    # valid. The warning attached by ``V2Config.load`` keeps all automatic and
    # status consumers fail-closed until the operator repairs the file.
    runtime["agent_events_trace_retention_enabled"] = False
    if field_name == "agent_events_trace_retention_days":
        runtime[field_name] = 30
    else:
        runtime[field_name] = False
    return True


def _reset_recoverable_config_section(
    payload: dict, section: str, field_name: Optional[str] = None
) -> bool:
    """Replace one independently recoverable section with its safe default.

    This is deliberately outside ``V2Config.from_payload``. Direct callers and
    API writes still get strict validation; only disk loading may enter this
    loss-avoiding recovery path, and the original file is backed up first.
    """

    if section == "runtime" and field_name is not None:
        if _recover_runtime_field(payload, field_name):
            return True
    if section in _FIELD_SCOPED_RECOVERY_SECTIONS:
        return _recover_switch_section_field(payload, section, field_name)
    if section == "memory.rerank":
        memory = payload.get("memory")
        processing = memory.get("processing") if isinstance(memory, dict) else None
        if not isinstance(processing, dict):
            return False
        processing.pop("rerank", None)
        return True
    if section == "memory.multimodal":
        memory = payload.get("memory")
        processing = memory.get("processing") if isinstance(memory, dict) else None
        if not isinstance(processing, dict):
            return False
        processing.pop("multimodal", None)
        return True
    if section == "memory.cloud":
        return _recover_memory_cloud_section(payload, field_name)
    if section == "runtime":
        # Keep this in sync with ``V2Config.default``.  RuntimeConfig has a
        # required cwd, so an empty object would make the recovery loop fail a
        # second time and discard otherwise valid settings.
        payload[section] = {"default_cwd": str(Path.home() / "work")}
        return True
    if section in {
        "model_hub",
        "memory",
        "remote_access",
        "update",
    }:
        payload[section] = {}
        return True
    if section == "agents":
        defaults = V2Config.default().agents
        payload[section] = {
            "opencode": dict(defaults.opencode.__dict__),
            "claude": dict(defaults.claude.__dict__),
            "codex": dict(defaults.codex.__dict__),
            "avault": dict(defaults.avault.__dict__),
        }
        return True
    if section == "gateway":
        payload[section] = None
        return True
    if section == "mode":
        payload[section] = "self_host"
        return True
    if section == "platforms":
        payload["platforms"] = {"enabled": [], "primary": WORKBENCH_PLATFORM_ID}
        payload["platform"] = WORKBENCH_PLATFORM_ID
        return True
    if section in {"ack_mode", "language", "agent_progress_style"}:
        payload[section] = {
            "ack_mode": "typing",
            "language": "en",
            "agent_progress_style": DEFAULT_AGENT_PROGRESS_STYLE,
        }[section]
        return True
    if section in {
        "show_duration",
        "include_time_info",
        "include_user_info",
        "reply_enhancements",
        "show_pages_prompt",
        "setup_completed",
    }:
        payload[section] = {
            "show_duration": False,
            "include_time_info": True,
            "include_user_info": True,
            "reply_enhancements": True,
            "show_pages_prompt": True,
            "setup_completed": False,
        }[section]
        return True
    if section in {"agent_status_heartbeat_ms", "agent_status_no_output_ms"}:
        payload.pop(section, None)
        return True
    if section.startswith("agents."):
        agent_name = section.split(".", 1)[1]
        agents_payload = payload.get("agents")
        if not isinstance(agents_payload, dict):
            return False
        if agent_name == "codex":
            # Codex is opt-in in the canonical first-run/recovery config.
            agents_payload[agent_name] = dict(V2Config.default().agents.codex.__dict__)
        else:
            agents_payload[agent_name] = {}
        return True
    if section in set(supported_platform_ids()) | {WORKBENCH_PLATFORM_ID}:
        payload[section] = {}
        platforms_payload = payload.get("platforms")
        if not isinstance(platforms_payload, dict):
            platforms_payload = {"enabled": [], "primary": WORKBENCH_PLATFORM_ID}
        enabled = platforms_payload.get("enabled")
        if not isinstance(enabled, list):
            enabled = []
        enabled = [item for item in enabled if item != section]
        primary = platforms_payload.get("primary")
        if primary == section or primary not in enabled:
            primary = enabled[0] if enabled else WORKBENCH_PLATFORM_ID
        payload["platforms"] = {"enabled": enabled, "primary": primary}
        if payload.get("platform") == section:
            payload["platform"] = primary
        return True
    return False


def _backup_config_file(
    path: Path,
    label: str,
    *,
    content: bytes | None = None,
) -> Optional[Path]:
    if content is None and not path.exists():
        return None
    try:
        if content is None:
            content = path.read_bytes()
        existing = sorted(
            path.parent.glob(f"{path.name}.bak-{label}-*"),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
        for candidate in existing:
            try:
                if candidate.read_bytes() == content:
                    candidate.chmod(0o600)
                    return candidate
            except OSError:
                continue
    except OSError:
        pass

    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    backup = path.with_name(f"{path.name}.bak-{label}-{stamp}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        file_descriptor = os.open(backup, flags, 0o600)
        with os.fdopen(file_descriptor, "wb") as destination:
            if content is None:
                with path.open("rb") as source:
                    shutil.copyfileobj(source, destination)
            else:
                destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
    except OSError as exc:
        logger.warning("Could not back up config before recovery (%s): %s", path, exc)
        try:
            backup.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return backup


def _config_file_lock(path: Path):
    """The shared cross-process lock guarding one config file."""

    return _path_file_lock(
        path.with_name(f".{path.name}.lock"),
        timeout_seconds=CONFIG_FILE_LOCK_TIMEOUT_SECONDS,
    )


@contextmanager
def config_file_lock(config_path: Optional[Path] = None) -> Iterator[None]:
    """Serialize work that must observe one exact persisted config snapshot.

    This is the same cross-process transaction used by ``V2Config.save`` and
    ``config_write_transaction``. It also takes the migration lock used by
    ``V2Config.load`` while persisting migrations, so ordinary saves cannot
    replace a file between migration verification and replacement. Keeping one
    lock path for ordinary config writes and guarded state transitions prevents
    a pairing update from racing a reader that is about to persist
    instance-owned state.
    """

    path = config_path or paths.get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _memory_config_transaction(path):
        with _config_file_lock(path):
            yield


def _write_config_payload(path: Path, payload: dict) -> None:
    content = json.dumps(payload, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    with config_file_lock(path):
        write_atomic(path, content)


def _write_config_payload_if_unchanged(
    path: Path,
    payload: dict,
    expected_raw: str,
) -> bool:
    """Replace a config only if the final pre-replace snapshot still matches."""

    content = json.dumps(payload, indent=2)
    temp_name: str | None = None
    with CONFIG_LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                delete=False,
            ) as tmp:
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
                temp_name = tmp.name
            current_raw = path.read_text(encoding="utf-8")
            if current_raw != expected_raw:
                return False
            os.replace(temp_name, path)
            temp_name = None
            return True
        except (OSError, UnicodeDecodeError):
            return False
        finally:
            if temp_name is not None:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass


def _persist_migrated_config_payload(
    path: Path,
    expected_raw: str,
    payload: dict,
) -> tuple[Optional[Path], Optional[str]]:
    """Persist a migration only when the snapshot read at load is unchanged."""

    try:
        with CONFIG_LOCK:
            with _config_file_lock(path):
                try:
                    current_raw = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    return None, f"Could not verify config before migration persistence: {exc}"
                if current_raw != expected_raw:
                    return None, "Skipped config migration because the file changed during load"
                backup = _backup_config_file(
                    path,
                    "model-hub-migration",
                    content=expected_raw.encode("utf-8"),
                )
                if backup is None:
                    return None, "Skipped config migration because the original file could not be backed up"
                if not _write_config_payload_if_unchanged(path, payload, expected_raw):
                    return backup, "Skipped config migration because the file changed before replacement"
                return backup, None
    except TimeoutError as exc:
        return None, f"Could not acquire config migration lock: {exc}"


_MODEL_HUB_CREDENTIAL_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "bearer",
    "credential",
    "key",
    "password",
    "passwd",
    "secret",
    "sig",
    "signature",
    "token",
}


def is_model_hub_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Return the backend-authoritative Model Hub release capability."""

    source = os.environ if environ is None else environ
    return source.get(MODEL_HUB_ENABLED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _validate_optional_datetime(value: object, field_path: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Config '{field_path}' must be a date-time string or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Config '{field_path}' must be a valid date-time") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Config '{field_path}' must include a timezone")
    return value


def normalize_model_hub_base_url(
    value: object,
    *,
    append_path: str | None = None,
) -> Optional[str]:
    """Validate a Source URL and optionally append an upstream API path."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Config 'model_hub.sources.base_url' is invalid")
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError("Config 'model_hub.sources.base_url' is invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("Config 'model_hub.sources.base_url' is invalid")
    for key, _ in parse_qsl(
        parsed.query,
        keep_blank_values=True,
    ):
        normalized = key.strip().lower().replace("-", "_").replace(".", "_")
        if normalized in _MODEL_HUB_CREDENTIAL_QUERY_KEYS or any(
            marker in normalized
            for marker in (
                "api_key",
                "access_token",
                "auth_token",
                "token",
                "authorization",
                "signature",
                "secret",
                "password",
                "credential",
            )
        ):
            raise ValueError("Config 'model_hub.sources.base_url' is invalid")
    if _contains_model_hub_credential_material(value):
        raise ValueError("Config 'model_hub.sources.base_url' is invalid")
    path = parsed.path.rstrip("/")
    if append_path is not None:
        if not append_path.startswith("/") or append_path.startswith("//"):
            raise ValueError("Model Hub URL path must be relative to the API root")
        path = f"{path}{append_path}"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, parsed.query, ""))


def _validate_model_hub_base_url(value: object) -> Optional[str]:
    return normalize_model_hub_base_url(value)


_MODEL_HUB_SOURCE_CLIENT_NONCE_PATTERN = re.compile(r"^scn_[a-z0-9]{16,64}$")


def validate_model_hub_source_client_nonce(value: object) -> str:
    if (
        not isinstance(value, str)
        or _MODEL_HUB_SOURCE_CLIENT_NONCE_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("Config 'model_hub.sources.client_nonce' is invalid")
    return value


def _filter_dataclass_fields(dc_class, payload: dict) -> dict:
    """Filter payload to only include fields defined in dataclass."""
    valid_fields = {f.name for f in fields(dc_class)}
    return {k: v for k, v in payload.items() if k in valid_fields}


@dataclass
class SlackConfig(BaseIMConfig):
    bot_token: str = ""
    app_token: Optional[str] = None
    signing_secret: Optional[str] = None
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    app_id: Optional[str] = None
    require_mention: bool = False
    # Global default for the per-channel require_bind gate (allowed users).
    # False=any channel member may drive the agent, True=only bound users.
    # Channels whose per-channel require_bind is None inherit this value.
    require_bind: bool = False
    disable_link_unfurl: bool = False

    def validate(self) -> None:
        # Allow empty token for initial setup
        if self.bot_token and not self.bot_token.startswith("xoxb-"):
            raise ValueError("Invalid Slack bot token format (should start with xoxb-)")
        if self.app_token and not self.app_token.startswith("xapp-"):
            raise ValueError("Invalid Slack app token format (should start with xapp-)")


@dataclass
class DiscordConfig(BaseIMConfig):
    bot_token: str = ""
    application_id: Optional[str] = None
    # Legacy input fields. Runtime server access is stored in settings.json
    # under scopes.guild.discord so it stays with channel/user scope settings.
    guild_allowlist: Optional[List[str]] = None
    guild_denylist: Optional[List[str]] = None
    require_mention: bool = False
    # Global default for the per-channel require_bind gate (allowed users).
    require_bind: bool = False
    # Auto-archive duration (minutes) for threads created by vibe-remote.
    # Discord only accepts 60, 1440, 4320, or 10080 (1h / 1d / 3d / 7d).
    # Defaults to 10080 (7d) to match Discord's longest native inactivity window
    # rather than aggressively archiving idle sessions after 1 hour.
    thread_auto_archive_minutes: int = 10080

    def validate(self) -> None:
        # Allow empty token for initial setup
        if self.bot_token and len(self.bot_token.strip()) < 10:
            raise ValueError("Invalid Discord bot token format")
        allowed_archive = {60, 1440, 4320, 10080}
        if self.thread_auto_archive_minutes not in allowed_archive:
            raise ValueError(
                "Invalid Discord thread_auto_archive_minutes "
                f"{self.thread_auto_archive_minutes!r}; must be one of "
                f"{sorted(allowed_archive)}"
            )


@dataclass
class TelegramConfig(BaseIMConfig):
    bot_token: str = ""
    require_mention: bool = True
    # Global default for the per-channel require_bind gate (allowed users).
    require_bind: bool = False
    forum_auto_topic: bool = True
    use_webhook: bool = False
    webhook_url: Optional[str] = None
    webhook_secret_token: Optional[str] = None
    allowed_chat_ids: Optional[List[str]] = None
    allowed_user_ids: Optional[List[str]] = None

    def validate(self) -> None:
        # Allow empty token for initial setup
        if self.bot_token and ":" not in self.bot_token:
            raise ValueError("Invalid Telegram bot token format")


@dataclass
class LarkConfig(BaseIMConfig):
    app_id: str = ""
    app_secret: str = ""
    require_mention: bool = False
    # Global default for the per-channel require_bind gate (allowed users).
    require_bind: bool = False
    domain: str = "feishu"  # "feishu" for domestic (open.feishu.cn), "lark" for international (open.larksuite.com)

    def validate(self) -> None:
        if self.domain not in ("feishu", "lark"):
            raise ValueError(f"Invalid lark domain: {self.domain!r}. Must be 'feishu' or 'lark'.")

    @property
    def api_base_url(self) -> str:
        """Return the base API URL for the configured domain."""
        if self.domain == "lark":
            return "https://open.larksuite.com"
        return "https://open.feishu.cn"


@dataclass
class WeChatConfig(BaseIMConfig):
    bot_token: str = ""
    base_url: str = "https://ilinkai.weixin.qq.com"
    cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"
    require_mention: bool = False  # unused for WeChat DM-only, kept for interface compat
    require_bind: bool = False  # unused for WeChat DM-only, kept for interface compat

    def validate(self) -> None:
        # bot_token can be empty during setup wizard (filled after QR login)
        pass


@dataclass
class AvibeConfig(BaseIMConfig):
    """Avibe — Vibe Remote's own Web UI surfaced as a first-class IM platform.

    Runs in-process; no remote credentials. ``enabled`` lets headless
    deployments skip the workbench surface entirely while keeping the
    other IM-bridge platforms (Slack/Discord/...) wired up.
    """

    enabled: bool = True

    def validate(self) -> None:
        return None


@dataclass
class GatewayConfig:
    relay_url: Optional[str] = None
    workspace_token: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    last_connected_at: Optional[str] = None


@dataclass
class AudioAsrConfig:
    enabled: bool = True
    echo_transcript: bool = True
    enabled_configured: bool = False
    timeout_seconds: float = 60.0
    endpoint_path: str = "/v1/audio/transcriptions"
    model: str = "qwen3-asr-flash"
    max_file_bytes: Optional[int] = None

    def __post_init__(self) -> None:
        """Refuse a value that is not the kind this class declares.

        ``from_payload`` copies this section verbatim, and an Editor may now
        persist it, so a stale client or a hand-edited config can put ``[]``
        where a switch belongs. Nothing further down coerces it: the Settings
        pages read ``value !== false`` as on while ``AudioAsrService`` reads the
        same value for truth, so ASR renders as enabled while transcription is
        off. A ``true`` where ``timeout_seconds`` belongs is the same defect one
        kind over — ``float(True)`` is a legal one-second timeout.

        Enforced here rather than in ``from_payload`` so every construction path
        holds the invariant the field types already claim.
        """
        _refuse_values_naming_nothing("audio_asr", self)


_MEMORY_MAX_URL_BYTES = 2048
_MEMORY_MAX_MODEL_BYTES = 512
_MEMORY_MAX_API_KEY_BYTES = 16 * 1024
_MEMORY_CLOUD_MODEL_ALIAS = "avibe-cloud-chat"
_MEMORY_CLOUD_MULTIMODAL_ALIAS = "avibe-cloud-multimodal"

MemoryMode = Literal["platform", "custom"]
MemoryCloudScope = Literal["organization", "platform"]
MemoryCloudLlmSource = Literal["dedicated", "chat_fallback"]
MemoryRerankProvider = Literal["deepinfra", "vllm", "dashscope"]
MEMORY_RERANK_PROVIDERS = frozenset(get_args(MemoryRerankProvider))
DEFAULT_MEMORY_RERANK_PROVIDER: MemoryRerankProvider = "deepinfra"
DASHSCOPE_RERANK_MODEL = "gte-rerank-v2"


@dataclass
class MemoryEndpointConfig:
    """One write-only processing endpoint used by the local memory sidecar."""

    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = field(default=None, repr=False)
    provider: Optional[str] = None

    def validate(self, *, name: str) -> None:
        self.base_url = _validate_memory_url(
            self.base_url,
            path=f"memory.processing.{name}.base_url",
        )
        self.model = _validate_memory_text(
            self.model,
            name=f"memory.processing.{name}.model",
            maximum=_MEMORY_MAX_MODEL_BYTES,
        )
        self.api_key = _validate_memory_key(
            self.api_key,
            path=f"memory.processing.{name}.api_key",
        )
        if name != "rerank":
            self.provider = None
            return
        if not any((self.base_url, self.model, self.api_key)):
            self.provider = None
            return
        provider = (self.provider or "").strip() or _inferred_memory_rerank_provider(
            base_url=self.base_url,
            model=self.model,
        )
        if provider not in MEMORY_RERANK_PROVIDERS:
            raise ValueError(
                "Memory rerank endpoint provider must be deepinfra, vllm, or dashscope"
            )
        if provider == "dashscope" and self.model != DASHSCOPE_RERANK_MODEL:
            raise ValueError(
                "Memory DashScope rerank endpoint model must be gte-rerank-v2"
            )
        self.provider = provider

    def complete(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    def rerank_provider(self) -> MemoryRerankProvider:
        provider = (self.provider or "").strip()
        if provider in MEMORY_RERANK_PROVIDERS:
            return provider
        return _inferred_memory_rerank_provider(
            base_url=self.base_url,
            model=self.model,
        )


@dataclass
class MemoryProcessingConfig:
    llm: MemoryEndpointConfig = field(default_factory=MemoryEndpointConfig)
    embedding: MemoryEndpointConfig = field(default_factory=MemoryEndpointConfig)
    rerank: Optional[MemoryEndpointConfig] = None
    multimodal: Optional[MemoryEndpointConfig] = None

    def validate(self) -> None:
        self.llm.validate(name="llm")
        self.embedding.validate(name="embedding")
        if self.rerank is not None:
            self.rerank.validate(name="rerank")
            if not any(
                (
                    self.rerank.base_url,
                    self.rerank.model,
                    self.rerank.api_key,
                )
            ):
                self.rerank = None
            elif not self.rerank.complete():
                raise ValueError(
                    "Memory rerank endpoint must include base_url, model, and api_key"
                )
        if self.multimodal is not None:
            self.multimodal.validate(name="multimodal")
            if not any(
                (
                    self.multimodal.base_url,
                    self.multimodal.model,
                    self.multimodal.api_key,
                )
            ):
                self.multimodal = None
            elif not self.multimodal.complete():
                raise ValueError(
                    "Memory multimodal endpoint must include base_url, model, and api_key"
                )


@dataclass
class MemoryCloudCapabilities:
    asr: bool = False
    chat: bool = False
    embedding: bool = False
    multimodal: bool = False
    # Older persisted cloud caches did not have the effective Memory LLM
    # capability. ``None`` keeps that shape distinguishable while the helper
    # methods treat it as Chat fallback until the next status sync.
    memory_llm: bool | None = None

    def validate(self) -> None:
        if any(
            not isinstance(value, bool)
            for value in (
                self.asr,
                self.chat,
                self.embedding,
                self.multimodal,
            )
        ) or (self.memory_llm is not None and not isinstance(self.memory_llm, bool)):
            raise ValueError("Config 'memory.cloud.capabilities' values must be booleans")

    def memory_available(self) -> bool:
        effective_memory_llm = self.chat if self.memory_llm is None else self.memory_llm
        return effective_memory_llm and self.embedding


@dataclass
class MemoryCloudConfig:
    """Cached Cloud Model Service resolution and write-only model key."""

    scope: MemoryCloudScope | None = None
    capabilities: MemoryCloudCapabilities = field(default_factory=MemoryCloudCapabilities)
    memory_llm_source: MemoryCloudLlmSource | None = None
    embedding_identity: str | None = None
    revision: int | None = None
    quota_enforced: bool = False
    model_access_key: str | None = field(default=None, repr=False)
    proxy_base_url: str | None = None
    source_instance_id: str = ""
    organization_attached: bool = False
    transition_notice_pending: bool = False
    applied_embedding_identity: str | None = None
    runtime_apply_pending: bool = False

    def validate(self) -> None:
        if self.scope is not None and self.scope not in get_args(MemoryCloudScope):
            raise ValueError("Config 'memory.cloud.scope' must be 'organization', 'platform', or null")
        if self.memory_llm_source is not None and self.memory_llm_source not in get_args(
            MemoryCloudLlmSource
        ):
            raise ValueError(
                "Config 'memory.cloud.memory_llm_source' must be 'dedicated', 'chat_fallback', or null"
            )
        self.capabilities.validate()
        if self.memory_llm_source == "dedicated" and self.capabilities.memory_llm is False:
            raise ValueError(
                "Config 'memory.cloud.memory_llm_source' dedicated requires memory_llm"
            )
        if (
            self.memory_llm_source == "chat_fallback"
            and self.capabilities.memory_llm is not None
            and self.capabilities.memory_llm != self.capabilities.chat
        ):
            raise ValueError(
                "Config 'memory.cloud.memory_llm_source' chat_fallback requires chat"
            )
        if self.embedding_identity is not None:
            self.embedding_identity = _validate_memory_text(
                self.embedding_identity,
                name="memory.cloud.embedding_identity",
                maximum=_MEMORY_MAX_MODEL_BYTES,
            )
        if self.applied_embedding_identity is not None:
            self.applied_embedding_identity = _validate_memory_text(
                self.applied_embedding_identity,
                name="memory.cloud.applied_embedding_identity",
                maximum=_MEMORY_MAX_MODEL_BYTES,
            )
        if self.revision is not None and (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise ValueError("Config 'memory.cloud.revision' must be a non-negative integer or null")
        for name, value in (
            ("quota_enforced", self.quota_enforced),
            ("organization_attached", self.organization_attached),
            ("transition_notice_pending", self.transition_notice_pending),
            ("runtime_apply_pending", self.runtime_apply_pending),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"Config 'memory.cloud.{name}' must be a boolean")
        if not isinstance(self.source_instance_id, str):
            raise ValueError("Config 'memory.cloud.source_instance_id' must be a string")
        self.source_instance_id = self.source_instance_id.strip()
        if len(self.source_instance_id.encode("utf-8")) > _MEMORY_MAX_MODEL_BYTES:
            raise ValueError("Config 'memory.cloud.source_instance_id' is invalid")
        if self.proxy_base_url is not None:
            self.proxy_base_url = _validate_memory_url(
                self.proxy_base_url,
                path="memory.cloud.proxy_base_url",
            )
        if self.model_access_key is not None:
            self.model_access_key = _validate_memory_key(
                self.model_access_key,
                path="memory.cloud.model_access_key",
            )
            if not self.model_access_key.startswith("mak_"):
                raise ValueError("Config 'memory.cloud.model_access_key' is invalid")

    def memory_capability_available(self) -> bool:
        return bool(
            self.capabilities.memory_available()
            and self.embedding_identity
            and self.proxy_base_url
        )

    def runtime_ready(self) -> bool:
        return self.memory_capability_available() and bool(self.model_access_key)


@dataclass
class MemoryConfig:
    """Persisted local EverOS configuration; credentials are API-write-only."""

    enabled: bool = False
    mode: MemoryMode | None = None
    processing: MemoryProcessingConfig = field(default_factory=MemoryProcessingConfig)
    cloud: MemoryCloudConfig = field(default_factory=MemoryCloudConfig)
    # Released recovery markers collapse into one durable repair fence. It is
    # persisted internally until a successful destructive Repair clears it,
    # but never exposed through public config projections.
    legacy_needs_repair: bool = field(default=False, repr=False, compare=False)

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("Config 'memory.enabled' must be a boolean")
        if self.mode is not None and self.mode not in get_args(MemoryMode):
            raise ValueError("Config 'memory.mode' must be 'platform', 'custom', or null")
        if not isinstance(self.legacy_needs_repair, bool):
            raise ValueError("Config 'memory' legacy repair state must be a boolean")
        self.processing.validate()
        self.cloud.validate()
        if (
            self.enabled
            and not self.cloud_runtime_selected()
            and not self.custom_processing_complete()
        ):
            raise ValueError("Both Memory processing endpoints must be complete before enabling Memory")

    def custom_processing_complete(self) -> bool:
        return self.processing.llm.complete() and self.processing.embedding.complete()

    def cloud_runtime_selected(self) -> bool:
        if self.cloud.scope == "organization":
            # A pending enterprise transition is deliberately fenced on the
            # last applied custom identity until the user accepts a data reset.
            return self.cloud.organization_attached
        # Mode owns the runtime source. A missing or recovered cloud cache must
        # pause platform Memory, never expose saved custom endpoints as fallback.
        return self.mode == "platform"

    def runtime_source(self) -> Literal["cloud", "custom", "unavailable"]:
        if self.cloud_runtime_selected():
            return "cloud" if self.cloud.runtime_ready() else "unavailable"
        if self.custom_processing_complete():
            return "custom"
        return "unavailable"

    def settings_mode(self) -> Literal["organization", "platform", "custom"]:
        if self.cloud.scope == "organization":
            # An organization without the complete Memory pair cannot replace
            # a released working custom setup. Fresh installs still get the
            # read-only managed state instead of a misleading platform fallback.
            if self.custom_processing_complete() and not (
                self.cloud.memory_capability_available()
                or self.cloud.organization_attached
                or self.cloud.transition_notice_pending
            ):
                return "custom"
            return "organization"
        if self.mode == "platform":
            return "platform"
        return "custom"

    def runtime_processing(self) -> MemoryProcessingConfig:
        """Return the sidecar-facing endpoints without changing saved custom slots."""

        if not self.cloud_runtime_selected():
            return self.processing
        if not self.cloud.runtime_ready():
            return MemoryProcessingConfig()
        base_url = self.cloud.proxy_base_url
        key = self.cloud.model_access_key
        # A changed managed embedding identity is not admitted until the user
        # accepts local data loss. Keep the last applied identity as the
        # sidecar-facing baseline while the control plane reports the change.
        embedding_identity = (
            self.cloud.applied_embedding_identity
            or self.cloud.embedding_identity
        )
        multimodal = None
        if self.cloud.capabilities.multimodal:
            multimodal = MemoryEndpointConfig(
                base_url=f"{base_url}/mm",
                model=_MEMORY_CLOUD_MULTIMODAL_ALIAS,
                api_key=key,
            )
        return MemoryProcessingConfig(
            llm=MemoryEndpointConfig(
                base_url=base_url,
                model=_MEMORY_CLOUD_MODEL_ALIAS,
                api_key=key,
            ),
            embedding=MemoryEndpointConfig(
                base_url=base_url,
                model=f"avibe-cloud-embedding-{embedding_identity}",
                api_key=key,
            ),
            rerank=None,
            multimodal=multimodal,
        )

    def runtime_embedding_identity(self) -> tuple[str, str | None, str | None]:
        if self.cloud_runtime_selected():
            # Capability removal clears the live status identity, but the last
            # applied value remains the comparison baseline for a checked resume.
            return (
                "cloud",
                self.cloud.applied_embedding_identity
                or self.cloud.embedding_identity,
                None,
            )
        return (
            "custom",
            self.processing.embedding.base_url,
            self.processing.embedding.model,
        )

    def effective_multimodal_available(self) -> bool:
        if self.cloud_runtime_selected():
            # Cloud chat is the declared fallback when no dedicated mm slot exists;
            # a dedicated multimodal slot remains valid without Chat.
            return self.cloud.runtime_ready() and (
                self.cloud.capabilities.multimodal or self.cloud.capabilities.chat
            )
        return bool(self.processing.multimodal and self.processing.multimodal.complete())


def _inferred_memory_rerank_provider(
    *,
    base_url: Optional[str],
    model: Optional[str] = None,
) -> MemoryRerankProvider:
    hostname = (urlsplit((base_url or "").strip()).hostname or "").lower()
    # Legacy omitted-provider configs meant DeepInfra. Only an unambiguous
    # Bailian workspace host may change that default on upgrade.
    if hostname.endswith(".maas.aliyuncs.com"):
        return "dashscope"
    return DEFAULT_MEMORY_RERANK_PROVIDER


def _validate_memory_url(value: object, *, path: str) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"Config '{path}' must be a string")
    candidate = value.strip()
    if (
        not candidate
        or len(candidate.encode("utf-8")) > _MEMORY_MAX_URL_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        raise ValueError(f"Config '{path}' is invalid")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"Config '{path}' is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Config '{path}' is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"Config '{path}' is invalid")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError(f"Config '{path}' requires HTTPS")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _validate_memory_text(value: object, *, name: str, maximum: int) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"Config '{name}' must be a string")
    candidate = value.strip()
    if (
        not candidate
        or len(candidate.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
        or _looks_like_ui_mask(candidate)
    ):
        raise ValueError(f"Config '{name}' is invalid")
    return candidate


def _validate_memory_key(value: object, *, path: str) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"Config '{path}' must be a string")
    if (
        len(value.encode("utf-8")) > _MEMORY_MAX_API_KEY_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or _looks_like_ui_mask(value)
    ):
        raise ValueError(f"Config '{path}' is invalid")
    return value


def _looks_like_ui_mask(value: str) -> bool:
    stripped = value.strip()
    return bool(stripped) and all(character in {"*", "•", "x", "X"} for character in stripped)


def memory_config_to_payload(
    memory: MemoryConfig,
    *,
    include_secrets: bool = False,
    include_internal: bool = False,
) -> dict:
    """Project Memory config without ever returning a reusable API key."""

    def endpoint_payload(
        endpoint: MemoryEndpointConfig,
        *,
        include_provider: bool = False,
    ) -> dict:
        key = endpoint.api_key
        payload = {
            "base_url": endpoint.base_url,
            "model": endpoint.model,
            "api_key": key if include_secrets else None,
            "has_api_key": bool(key),
        }
        if include_provider:
            payload["provider"] = endpoint.rerank_provider()
        return payload

    processing = {
        "llm": endpoint_payload(memory.processing.llm),
        "embedding": endpoint_payload(memory.processing.embedding),
    }
    if memory.processing.rerank is not None:
        processing["rerank"] = endpoint_payload(
            memory.processing.rerank,
            include_provider=True,
        )
    if memory.processing.multimodal is not None:
        processing["multimodal"] = endpoint_payload(memory.processing.multimodal)
    payload = {
        "enabled": memory.enabled,
        "mode": memory.mode,
        "processing": processing,
        "cloud": {
            "scope": memory.cloud.scope,
            "capabilities": {
                "asr": memory.cloud.capabilities.asr,
                "chat": memory.cloud.capabilities.chat,
                "embedding": memory.cloud.capabilities.embedding,
                "multimodal": memory.cloud.capabilities.multimodal,
                "memory_llm": memory.cloud.capabilities.memory_llm,
            },
            "memory_llm_source": memory.cloud.memory_llm_source,
            "embedding_identity": memory.cloud.embedding_identity,
            "revision": memory.cloud.revision,
            "quota_enforced": memory.cloud.quota_enforced,
            "model_access_key": (
                memory.cloud.model_access_key if include_secrets else None
            ),
            "has_model_access_key": bool(memory.cloud.model_access_key),
            "proxy_base_url": memory.cloud.proxy_base_url,
            "source_instance_id": memory.cloud.source_instance_id,
            "organization_attached": memory.cloud.organization_attached,
            "transition_notice_pending": memory.cloud.transition_notice_pending,
            "applied_embedding_identity": memory.cloud.applied_embedding_identity,
            "runtime_apply_pending": memory.cloud.runtime_apply_pending,
        },
    }
    if include_internal and memory.legacy_needs_repair:
        payload["repair_required"] = True
    return payload


def _optional_memory_object(value: object, *, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Config '{name}' must be an object")
    return value


def memory_config_from_payload(payload: object) -> MemoryConfig:
    """Parse Memory config and collapse released recovery fields into one fence."""

    payload = _optional_memory_object(payload, name="memory")
    processing_payload = _optional_memory_object(
        payload.get("processing", {}),
        name="memory.processing",
    )
    llm_payload = _optional_memory_object(
        processing_payload.get("llm", {}),
        name="memory.processing.llm",
    )
    embedding_payload = _optional_memory_object(
        processing_payload.get("embedding", {}),
        name="memory.processing.embedding",
    )
    rerank_payload = None
    if "rerank" in processing_payload:
        rerank_payload = _optional_memory_object(
            processing_payload["rerank"],
            name="memory.processing.rerank",
        )
    multimodal_payload = None
    if "multimodal" in processing_payload:
        multimodal_payload = _optional_memory_object(
            processing_payload["multimodal"],
            name="memory.processing.multimodal",
        )
    cloud_payload = _optional_memory_object(
        payload.get("cloud", {}),
        name="memory.cloud",
    )
    cloud_capabilities_payload = _optional_memory_object(
        cloud_payload.get("capabilities", {}),
        name="memory.cloud.capabilities",
    )

    repair_required = payload.get("repair_required", False)
    if not isinstance(repair_required, bool):
        raise ValueError("Config 'memory.repair_required' must be a boolean")
    legacy_pending = payload.get("embedding_change_pending", False)
    if not isinstance(legacy_pending, bool):
        raise ValueError("Config 'memory.embedding_change_pending' must be a boolean")
    legacy_intent = payload.get("recovery_intent")
    if legacy_intent is not None and (
        not isinstance(legacy_intent, str)
        or legacy_intent not in {"rebuild", "factory_reset"}
    ):
        raise ValueError(
            "Config 'memory.recovery_intent' must be 'rebuild', 'factory_reset', or null"
        )
    transition_rebuild_owned = cloud_payload.get("transition_rebuild_owned", False)
    if not isinstance(transition_rebuild_owned, bool):
        raise ValueError(
            "Config 'memory.cloud.transition_rebuild_owned' must be a boolean"
        )
    legacy_needs_repair = bool(
        repair_required
        or legacy_pending
        or legacy_intent is not None
        or transition_rebuild_owned
    )

    memory = MemoryConfig(
        enabled=payload.get("enabled", False),
        mode=payload.get("mode"),
        legacy_needs_repair=legacy_needs_repair,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig(
                **_filter_dataclass_fields(MemoryEndpointConfig, llm_payload)
            ),
            embedding=MemoryEndpointConfig(
                **_filter_dataclass_fields(MemoryEndpointConfig, embedding_payload)
            ),
            rerank=(
                MemoryEndpointConfig(
                    **_filter_dataclass_fields(MemoryEndpointConfig, rerank_payload)
                )
                if rerank_payload is not None
                else None
            ),
            multimodal=(
                MemoryEndpointConfig(
                    **_filter_dataclass_fields(
                        MemoryEndpointConfig,
                        multimodal_payload,
                    )
                )
                if multimodal_payload is not None
                else None
            ),
        ),
        cloud=MemoryCloudConfig(
            **_filter_dataclass_fields(
                MemoryCloudConfig,
                {
                    **cloud_payload,
                    "capabilities": MemoryCloudCapabilities(
                        **_filter_dataclass_fields(
                            MemoryCloudCapabilities,
                            cloud_capabilities_payload,
                        )
                    ),
                },
            )
        ),
    )
    memory.validate()
    return memory


@dataclass
class RuntimeConfig:
    default_cwd: str
    log_level: str = "INFO"
    # Synchronous handlers under /show/*/api and /p/*/api may depend on slower
    # operational systems. Keep this separate from ordinary Runtime asset
    # requests, which retain their shorter transport timeout.
    show_page_api_timeout_seconds: float = DEFAULT_SHOW_PAGE_API_TIMEOUT_SECONDS
    # Linux/cgroup v2 best-effort resource governance for aggregate agent
    # workload. "auto" enables it only when Avibe can create and write the
    # delegated cgroup; unsupported systems silently fall back to legacy spawn.
    resource_governance: dict = field(default_factory=lambda: {"mode": "auto"})
    # Harness run staleness sweep. A Run whose executor died, whose transport never
    # came back, or whose workbench queue hold was never released has nothing left to
    # write its terminal state, so it would sit ``running``/``queued`` forever. The
    # sweep terminalizes those on evidence (see
    # ``docs/plans/agent-run-zombie-settlement.md`` §4). Each key is seconds and ``0``
    # disables that class; the defaults are the product decision, so there is no UI.
    harness_run_sweep_interval_seconds: int = DEFAULT_HARNESS_RUN_SWEEP_INTERVAL_SECONDS
    harness_run_orphan_grace_seconds: int = DEFAULT_HARNESS_RUN_ORPHAN_GRACE_SECONDS
    harness_run_queued_ttl_seconds: int = DEFAULT_HARNESS_RUN_QUEUED_TTL_SECONDS
    harness_run_hold_ttl_seconds: int = DEFAULT_HARNESS_RUN_HOLD_TTL_SECONDS
    # Echo the Harness-originated prompt into the IM conversation when a background
    # task (scheduled task, watch, webhook, hook, ``vibe agent run``) starts an agent
    # turn there. The Workbench transcript already renders that prompt from the
    # ``harness`` Message row; an IM channel had no equivalent, so a scheduled reply
    # arrived as an answer to a question nobody in the channel could see. Off means
    # today's behavior (result only).
    harness_prompt_echo: bool = True
    # Bounded retention for internal agent trace events (``agent_events`` rows
    # with ``event_type='tool_call'`` and ``visibility='trace'``). These are
    # debug/diagnostic material, never chat messages; without a bound an active
    # installation grows ``vibe.sqlite`` without limit. Default-on: the first
    # controller startup after upgrading runs a pass that permanently deletes
    # tool_call traces older than the window — set ``enabled=False`` BEFORE
    # upgrading to preserve old diagnostics. User docs (en+zh): the
    # ``vibe data retention`` section of the CLI reference (avibe-docs).
    agent_events_trace_retention_enabled: bool = True
    agent_events_trace_retention_days: int = 30

    def __post_init__(self) -> None:
        self.show_page_api_timeout_seconds = _named_value(
            "runtime.show_page_api_timeout_seconds",
            float,
            self.show_page_api_timeout_seconds,
        )
        if self.show_page_api_timeout_seconds <= 0:
            raise ValueError(
                "Config 'runtime.show_page_api_timeout_seconds' must be greater than zero"
            )
        # These values control irreversible deletion.  Dataclass annotations do
        # not validate JSON payloads, and coercing a string/bool here could turn
        # a malformed config into a shorter retention window.
        if not isinstance(self.agent_events_trace_retention_enabled, bool):
            raise ValueError(
                "Config 'runtime.agent_events_trace_retention_enabled' must be a boolean"
            )
        validate_retention_days(
            self.agent_events_trace_retention_days,
            field="Config 'runtime.agent_events_trace_retention_days'",
        )


@dataclass
class OpenCodeConfig:
    enabled: bool = True
    cli_path: str = "opencode"
    default_agent: Optional[str] = None
    default_reasoning_effort: Optional[str] = None
    error_retry_limit: int = DEFAULT_OPENCODE_ERROR_RETRY_LIMIT  # Max retries on LLM stream errors (0 = no retry)
    active_turn_timeout_seconds: int = DEFAULT_OPENCODE_ACTIVE_TURN_TIMEOUT_SECONDS
    # Provenance for the legacy-default neutralization: once this config has
    # been written (or migrated) under the opt-in semantics the key is present,
    # and a deliberate ``active_turn_timeout_seconds == 5400`` saved afterwards
    # is an explicit operator choice the load migration must preserve.
    legacy_turn_timeout_neutralized: bool = True
    # Provider the user picked in Settings → Backends → OpenCode. The provider
    # catalog itself lives in ~/.config/opencode/opencode.json (OpenCode's own
    # state file). Stays ``None`` until the user explicitly chooses so legacy
    # installs (e.g. Ollama/OpenAI users) keep falling back to OpenCode's own
    # routing for bare-model strings instead of being silently rerouted to
    # Anthropic on upgrade.
    default_provider: Optional[str] = None


@dataclass
class ClaudeConfig:
    enabled: bool = True
    cli_path: str = "claude"
    idle_timeout_seconds: int = DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS
    # Auth model: "oauth" relies on Claude Code's own credential storage;
    # "api_key" injects ANTHROPIC_API_KEY (and optionally ANTHROPIC_BASE_URL)
    # at CLI launch time for API gateway / proxy setups.
    auth_mode: Literal["oauth", "api_key"] = "oauth"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    # ``True`` once the user has saved a Claude auth choice through the
    # Settings UI (or removed the API key, or signed out). Legacy installs
    # — V2 configs that predate the Settings page or have never touched
    # it — load with ``False`` because the field defaults to ``False`` and
    # isn't in their on-disk JSON. ``build_claude_subprocess_env`` reads
    # this to decide whether to honor ``auth_mode`` strictly (strip
    # inherited ``ANTHROPIC_*`` env in OAuth mode) or preserve the
    # legacy env-var-only auth path. Without this flag the schema's
    # ``auth_mode == "oauth"`` default is indistinguishable between
    # "explicit OAuth pick" and "user has never opened Settings".
    auth_mode_set: bool = False


@dataclass
class CodexConfig:
    enabled: bool = True
    cli_path: str = "codex"
    idle_timeout_seconds: int = DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS
    # Auth model: "oauth" defers to whatever ~/.codex/config.toml already
    # has (typically `auth.method = "ChatGPT"`); "api_key" writes the
    # config.toml fields that point Codex at an API key + custom base URL.
    auth_mode: Literal["oauth", "api_key"] = "oauth"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    # Relay recovery marker captured at an OAuth transition:
    # ``{"provider_id": str, "base_url": str}`` recorded from the live
    # ``config.toml`` immediately BEFORE the OAuth cleanup clears the
    # provider pointer and drops the managed section (the only moment
    # the relay's identity is observable — Settings-created managed
    # relays and hand-rolled sections are both visible then). Consumed
    # verbatim, with no ambient-state inference, by the Settings read
    # and the next API-key save; cleared only by an explicit API-key
    # save or sign-out. Repeated OAuth transitions retain it. Older
    # configs load with ``None`` (unknown JSON keys are filtered), so
    # the field degrades safely across releases.
    oauth_relay_marker: Optional[dict] = None


@dataclass
class AVaultConfig:
    cli_path: str = "avault"


@dataclass
class AgentsConfig:
    default_backend: str = DEFAULT_AGENT_BACKEND
    opencode: OpenCodeConfig = field(default_factory=OpenCodeConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    avault: AVaultConfig = field(default_factory=AVaultConfig)


_SettledItem = TypeVar("_SettledItem")


def _collapse_settled_duplicates(
    parsed: list[_SettledItem],
    identity: Callable[[_SettledItem], object],
    as_written: list,
    message: str,
    *,
    repairing: bool,
) -> list[_SettledItem]:
    """Keep a collection of model identifiers unique after spelling is settled.

    `normalized_model_id` is a many-to-one map applied inside the leaf validator,
    while the uniqueness check belongs to the parent holding the collection. So
    every such parent is checking pre-images while the object it builds carries
    post-images, and the difference is not cosmetic: the loaded config keeps both
    entries, `to_payload` writes one spelling for both, and the next load of what
    this one wrote raises. Loading a file must produce something loadable, and
    that round trip is what this restores.

    Duplicates **as written** are a malformed payload either way and raise.
    Duplicates that appear only once spelling is settled depend on where the
    payload came from, which is what `repairing` names:

    - Repairing a payload already on disk, the later entry collapses into the
      earlier. It was already unreachable — an inventory lookup and an exact hop
      comparison both find the first — and raising would fail exactly the load the
      persisted-shape rule requires to succeed.
    - Admitting a payload from a caller, the same pair is a request to name one
      model twice, and silently keeping one half answers with a collection the
      caller did not send. It raises, so the caller learns which spelling survived
      by being told rather than by reading the response.

    Required, and not defaulted, because the answer is a property of the caller
    rather than of the collection: a parser cannot infer whether it is repairing
    history or admitting a request, and a default would let the next call site
    inherit whichever guess this one made.
    """

    if len(set(as_written)) != len(as_written):
        raise ValueError(message)
    if not repairing:
        settled_ids = [identity(item) for item in parsed]
        if len(set(settled_ids)) != len(settled_ids):
            raise ValueError(message)
        return list(parsed)
    collapsed: list[_SettledItem] = []
    seen: set[object] = set()
    for item in parsed:
        settled = identity(item)
        if settled in seen:
            continue
        seen.add(settled)
        collapsed.append(item)
    return collapsed


@dataclass
class ModelHubModelConfig:
    id: str
    provenance: Literal["discovered", "manual"]
    reasoning_efforts: list[str] = field(default_factory=list)
    display_name: Optional[str] = None
    discovered_at: Optional[str] = None
    retired: Optional[bool] = None

    @classmethod
    def from_payload(cls, payload: dict) -> "ModelHubModelConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.sources.models' entries must be objects")
        if set(payload) - {
            "id",
            "display_name",
            "origin",
            "reasoning_efforts",
            "discovered_at",
            "retired",
        }:
            raise ValueError("Config 'model_hub.sources.models' contains unknown fields")
        model_id = payload.get("id")
        origin = payload.get("origin")
        if "reasoning_efforts" not in payload:
            raise ValueError("Config 'model_hub.sources.models.reasoning_efforts' is required")
        reasoning_efforts = payload["reasoning_efforts"]
        display_name = payload.get("display_name")
        discovered_at = payload.get("discovered_at")
        retired = payload.get("retired")
        if not isinstance(model_id, str) or not model_id or _contains_model_hub_credential_material(model_id):
            raise ValueError("Config 'model_hub.sources.models.id' must be a non-empty string")
        if not isinstance(origin, str) or origin not in {"discovered", "manual"}:
            raise ValueError("Config 'model_hub.sources.models.origin' is invalid")
        if (
            not isinstance(reasoning_efforts, list)
            or any(not isinstance(effort, str) or not effort for effort in reasoning_efforts)
            or len(set(reasoning_efforts)) != len(reasoning_efforts)
            or any(_contains_model_hub_credential_material(effort) for effort in reasoning_efforts)
        ):
            raise ValueError(
                "Config 'model_hub.sources.models.reasoning_efforts' must be a unique credential-free array of strings"
            )
        if display_name is not None and (
            not isinstance(display_name, str)
            or _contains_model_hub_credential_material(display_name)
        ):
            raise ValueError("Config 'model_hub.sources.models.display_name' must be a string or null")
        if origin == "manual" and discovered_at is not None:
            raise ValueError("Config manual model entries cannot carry discovered_at")
        if retired is not None and (
            not isinstance(retired, bool) or (origin == "manual" and retired)
        ):
            raise ValueError(
                "Config 'model_hub.sources.models.retired' must be false for manual models"
            )
        # Spelling is settled here, at the one place a payload becomes a model
        # config, so no admission path can invent a second spelling for one
        # model and split its usage across two ledger rows. The length bound
        # stays with the admission surfaces: this constructor also reads files
        # older releases wrote, and rejecting one of those would fail config load.
        from core.handlers.model_hub.identifiers import normalized_model_id

        return cls(
            id=normalized_model_id(model_id),
            provenance=origin,
            reasoning_efforts=list(reasoning_efforts),
            display_name=display_name,
            discovered_at=_validate_optional_datetime(
                discovered_at,
                "model_hub.sources.models.discovered_at",
            ),
            retired=retired,
        )

    def to_payload(self) -> dict:
        payload = {
            "id": self.id,
            "display_name": self.display_name,
            "origin": self.provenance,
            "reasoning_efforts": list(self.reasoning_efforts),
            "discovered_at": self.discovered_at,
        }
        if self.retired is not None:
            payload["retired"] = self.retired
        return payload


@dataclass
class ModelHubSourceStateConfig:
    status: Literal["active", "standby", "cooldown", "needs_action", "error"] = "standby"
    retry_at: Optional[str] = None
    detail_key: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict) -> "ModelHubSourceStateConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.sources.state' must be an object")
        if set(payload) - {"status", "retry_at", "detail_key"}:
            raise ValueError("Config 'model_hub.sources.state' contains unknown fields")
        status = payload.get("status")
        retry_at = payload.get("retry_at")
        detail_key = payload.get("detail_key")
        if not isinstance(status, str) or status not in {
            "active",
            "standby",
            "cooldown",
            "needs_action",
            "error",
        }:
            raise ValueError("Config 'model_hub.sources.state.status' is invalid")
        if detail_key is not None and not isinstance(detail_key, str):
            raise ValueError("Config 'model_hub.sources.state.detail_key' must be a string or null")
        cooldown_keys = {
            None,
            "models.source.cooldown.network",
            "models.source.cooldown.timeout",
            "models.source.cooldown.rate_limited",
            "models.source.cooldown.quota_exhausted",
            "models.source.cooldown.server_error",
        }
        needs_action_keys = {
            "models.source.needs_action.oauth_expired",
            "models.source.needs_action.balance_exhausted",
            "models.source.needs_action.credential_revoked",
            "models.source.needs_action.account_banned",
        }
        if status in {"active", "standby"} and (retry_at is not None or detail_key is not None):
            raise ValueError(f"Config healthy source state '{status}' cannot carry blocker detail")
        if status == "cooldown" and (retry_at is None or detail_key not in cooldown_keys):
            raise ValueError("Config cooldown source state requires retry_at and a known detail key")
        if status == "needs_action" and (retry_at is not None or detail_key not in needs_action_keys):
            raise ValueError("Config needs_action source state requires a known detail key")
        if status == "error" and (retry_at is not None or detail_key != "models.source.error.unclassified"):
            raise ValueError("Config error source state requires the generic error detail key")
        return cls(
            status=status,
            retry_at=_validate_optional_datetime(retry_at, "model_hub.sources.state.retry_at"),
            detail_key=detail_key,
        )

    def to_payload(self) -> dict:
        return {"status": self.status, "retry_at": self.retry_at, "detail_key": self.detail_key}


@dataclass
class ModelHubSourceUsageConfig:
    cycle_used_pct: Optional[float] = None
    month_spend_cents: Optional[int] = None
    currency: Optional[str] = None
    projected_exhaust_at: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict) -> "ModelHubSourceUsageConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.sources.usage' must be an object")
        if set(payload) - {
            "cycle_used_pct",
            "month_spend_cents",
            "currency",
            "projected_exhaust_at",
        }:
            raise ValueError("Config 'model_hub.sources.usage' contains unknown fields")
        cycle_used_pct = payload.get("cycle_used_pct")
        if cycle_used_pct is not None and (
            isinstance(cycle_used_pct, bool)
            or not isinstance(cycle_used_pct, (int, float))
            or not 0 <= cycle_used_pct <= 100
        ):
            raise ValueError("Config 'model_hub.sources.usage.cycle_used_pct' must be between 0 and 100")
        month_spend_cents = payload.get("month_spend_cents")
        currency = payload.get("currency")
        projected_exhaust_at = payload.get("projected_exhaust_at")
        if month_spend_cents is not None and (
            isinstance(month_spend_cents, bool) or not isinstance(month_spend_cents, int) or month_spend_cents < 0
        ):
            raise ValueError("Config 'model_hub.sources.usage.month_spend_cents' must be a non-negative integer")
        if currency is not None and not isinstance(currency, str):
            raise ValueError("Config 'model_hub.sources.usage.currency' must be a string or null")
        return cls(
            cycle_used_pct=cycle_used_pct,
            month_spend_cents=month_spend_cents,
            currency=currency,
            projected_exhaust_at=_validate_optional_datetime(
                projected_exhaust_at,
                "model_hub.sources.usage.projected_exhaust_at",
            ),
        )

    def to_payload(self) -> dict:
        return {
            "cycle_used_pct": self.cycle_used_pct,
            "month_spend_cents": self.month_spend_cents,
            "currency": self.currency,
            "projected_exhaust_at": self.projected_exhaust_at,
        }


@dataclass
class ModelHubSourceConfig:
    id: str
    kind: Literal["subscription", "api_key"]
    vendor: str
    display_name: str
    protocol: Literal["anthropic", "openai_responses", "openai_chat"]
    supply_channel: Literal["native_cli", "hub"]
    billing: Literal["monthly", "metered"]
    state: ModelHubSourceStateConfig
    models: list[ModelHubModelConfig]
    created_at: str = MODEL_HUB_LEGACY_CREATED_AT
    client_nonce: Optional[str] = None
    last_discovered_at: Optional[str] = None
    base_url: Optional[str] = None
    usage: Optional[ModelHubSourceUsageConfig] = None
    credential_ref: Optional[str] = None
    account_label: Optional[str] = None
    masked_credential: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict, *, repairing: bool = False) -> "ModelHubSourceConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.sources' entries must be objects")
        allowed_fields = {
            "id",
            "created_at",
            "client_nonce",
            "last_discovered_at",
            "kind",
            "vendor",
            "display_name",
            "protocol",
            "base_url",
            "supply_channel",
            "billing",
            "state",
            "usage",
            "models",
            "credential_ref",
            "account_label",
            "masked_credential",
        }
        if set(payload) - allowed_fields:
            raise ValueError("Config 'model_hub.sources' contains unknown fields")
        source_id = payload.get("id")
        kind = payload.get("kind")
        vendor = payload.get("vendor")
        display_name = payload.get("display_name")
        protocol = payload.get("protocol")
        supply_channel = payload.get("supply_channel")
        billing = payload.get("billing")
        if not isinstance(source_id, str) or re.fullmatch(r"src_[a-z0-9]{8,}", source_id) is None:
            raise ValueError("Config 'model_hub.sources.id' is invalid")
        if not isinstance(kind, str) or kind not in {"subscription", "api_key"}:
            raise ValueError("Config 'model_hub.sources.kind' is invalid")
        vendor = normalize_model_hub_vendor_id(vendor)
        if not isinstance(display_name, str) or not display_name or len(display_name) > 64:
            raise ValueError("Config 'model_hub.sources.display_name' is invalid")
        if not isinstance(protocol, str) or protocol not in {
            "anthropic",
            "openai_responses",
            "openai_chat",
        }:
            raise ValueError("Config 'model_hub.sources.protocol' is invalid")
        if not isinstance(supply_channel, str) or supply_channel not in {"native_cli", "hub"}:
            raise ValueError("Config 'model_hub.sources.supply_channel' is invalid")
        if not isinstance(billing, str) or billing not in {"monthly", "metered"}:
            raise ValueError("Config 'model_hub.sources.billing' is invalid")
        models_payload = payload.get("models")
        if not isinstance(models_payload, list):
            raise ValueError("Config 'model_hub.sources.models' must be an array")
        model_ids = [model.get("id") for model in models_payload if isinstance(model, dict)]
        if len(model_ids) != len(models_payload):
            raise ValueError("Config 'model_hub.sources.models' entries must be objects")
        if any(not isinstance(model_id, str) for model_id in model_ids):
            raise ValueError("Config 'model_hub.sources.models.id' must be a non-empty string")
        models = _collapse_settled_duplicates(
            [ModelHubModelConfig.from_payload(model) for model in models_payload],
            lambda model: model.id,
            model_ids,
            "Config 'model_hub.sources.models' contains duplicate ids",
            repairing=repairing,
        )
        usage_payload = payload.get("usage")
        base_url = payload.get("base_url")
        credential_ref = payload.get("credential_ref")
        account_label = payload.get("account_label")
        masked_credential = payload.get("masked_credential")
        created_at = payload.get("created_at")
        client_nonce = (
            validate_model_hub_source_client_nonce(payload.get("client_nonce"))
            if "client_nonce" in payload
            else None
        )
        last_discovered_at = payload.get("last_discovered_at")
        base_url = _validate_model_hub_base_url(base_url)
        if credential_ref is not None and not isinstance(credential_ref, str):
            raise ValueError("Config 'model_hub.sources.credential_ref' is invalid")
        if account_label is not None and not isinstance(account_label, str):
            raise ValueError("Config 'model_hub.sources.account_label' is invalid")
        if masked_credential is not None and not isinstance(masked_credential, str):
            raise ValueError("Config 'model_hub.sources.masked_credential' is invalid")
        if kind == "subscription" and (base_url is not None or masked_credential is not None):
            raise ValueError("Config subscription Sources cannot carry API-key fields")
        if kind == "api_key" and supply_channel != "hub":
            raise ValueError("Config API-key Sources must use the hub channel")
        if supply_channel == "native_cli" and credential_ref is not None:
            raise ValueError("Config native Sources cannot carry an engine credential ref")
        if supply_channel == "hub" and (not isinstance(credential_ref, str) or not credential_ref):
            raise ValueError("Config hub Sources require an engine credential ref")
        if kind == "api_key" and usage_payload is not None:
            projected_exhaust_at = usage_payload.get("projected_exhaust_at") if isinstance(usage_payload, dict) else None
            if projected_exhaust_at is not None:
                raise ValueError("Config API-key Sources cannot carry projected exhaustion")
        return cls(
            id=source_id,
            kind=kind,
            vendor=vendor,
            display_name=display_name,
            protocol=protocol,
            supply_channel=supply_channel,
            billing=billing,
            state=ModelHubSourceStateConfig.from_payload(payload.get("state")),
            models=models,
            created_at=(
                _validate_optional_datetime(
                    created_at,
                    "model_hub.sources.created_at",
                )
                or MODEL_HUB_LEGACY_CREATED_AT
            ),
            client_nonce=client_nonce,
            last_discovered_at=_validate_optional_datetime(
                last_discovered_at,
                "model_hub.sources.last_discovered_at",
            ),
            base_url=base_url,
            usage=ModelHubSourceUsageConfig.from_payload(usage_payload) if usage_payload is not None else None,
            credential_ref=credential_ref,
            account_label=account_label,
            masked_credential=masked_credential,
        )

    def to_payload(self) -> dict:
        payload = {
            "id": self.id,
            "created_at": self.created_at,
            "last_discovered_at": self.last_discovered_at,
            "kind": self.kind,
            "vendor": self.vendor,
            "display_name": self.display_name,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "supply_channel": self.supply_channel,
            "billing": self.billing,
            "state": self.state.to_payload(),
            "models": [model.to_payload() for model in self.models],
            "credential_ref": self.credential_ref,
            "account_label": self.account_label,
            "masked_credential": self.masked_credential,
        }
        if self.client_nonce is not None:
            payload["client_nonce"] = self.client_nonce
        if self.usage is not None:
            payload["usage"] = self.usage.to_payload()
        return payload


@dataclass(frozen=True)
class ModelHubRouteHopConfig:
    source_id: str
    model_id: str

    @classmethod
    def from_payload(cls, payload: object) -> "ModelHubRouteHopConfig":
        if not isinstance(payload, dict) or set(payload) != {"source_id", "model_id"}:
            raise ValueError("Config 'model_hub.agents.routes.hops' entries must contain source_id and model_id")
        source_id = payload.get("source_id")
        model_id = payload.get("model_id")
        if not isinstance(source_id, str) or re.fullmatch(r"src_[a-z0-9]{8,}", source_id) is None:
            raise ValueError("Config 'model_hub.agents.routes.hops.source_id' is invalid")
        if (
            not isinstance(model_id, str)
            or not model_id
            or _contains_model_hub_credential_material(model_id)
        ):
            raise ValueError("Config 'model_hub.agents.routes.hops.model_id' is invalid")
        # A hop names a model in some source's inventory, and membership is decided
        # by an exact comparison against that inventory. So the reference has to be
        # spelled by whatever spells the thing it refers to: normalizing one side
        # only would turn a working chain into `model_unsupported` on upgrade.
        from core.handlers.model_hub.identifiers import normalized_model_id

        return cls(source_id=source_id, model_id=normalized_model_id(model_id))

    def to_payload(self) -> dict:
        return {"source_id": self.source_id, "model_id": self.model_id}


@dataclass
class ModelHubRouteConfig:
    hops: tuple[ModelHubRouteHopConfig, ...] = ()

    @classmethod
    def from_payload(cls, payload: object, *, repairing: bool = False) -> "ModelHubRouteConfig":
        if not isinstance(payload, dict) or set(payload) != {"hops"}:
            raise ValueError("Config 'model_hub.agents.routes' entries must contain hops")
        hops = payload.get("hops")
        if not isinstance(hops, list):
            raise ValueError("Config 'model_hub.agents.routes.hops' must be an array")
        return cls(
            hops=tuple(
                _collapse_settled_duplicates(
                    [ModelHubRouteHopConfig.from_payload(hop) for hop in hops],
                    lambda hop: (hop.source_id, hop.model_id),
                    [(hop["source_id"], hop["model_id"]) for hop in hops],
                    "Config 'model_hub.agents.routes.hops' must contain unique pairs",
                    repairing=repairing,
                )
            )
        )

    def to_payload(self) -> dict:
        return {"hops": [hop.to_payload() for hop in self.hops]}


@dataclass
class ModelHubMenuConfig:
    view: Literal["featured", "full"] = "featured"
    checked: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict) -> "ModelHubMenuConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.agents.menu' must be an object")
        if set(payload) - {"view", "checked"}:
            raise ValueError("Config 'model_hub.agents.menu' contains unknown fields")
        view = payload.get("view")
        checked = payload.get("checked")
        if not isinstance(view, str) or view not in {"featured", "full"}:
            raise ValueError("Config 'model_hub.agents.menu.view' is invalid")
        if not isinstance(checked, list) or not all(isinstance(item, str) for item in checked):
            raise ValueError("Config 'model_hub.agents.menu.checked' must be an array of strings")
        if len(set(checked)) != len(checked):
            raise ValueError("Config 'model_hub.agents.menu.checked' must be unique")
        if any(_contains_model_hub_credential_material(item) for item in checked):
            raise ValueError("Config 'model_hub.agents.menu.checked' is invalid")
        return cls(view=view, checked=list(checked))

    def to_payload(self) -> dict:
        return {"view": self.view, "checked": list(self.checked)}


_BACKEND_MODEL_INPUT_MODALITIES = frozenset(
    {"text", "image", "audio", "video", "pdf"}
)
_BACKEND_MODEL_OUTPUT_MODALITIES = frozenset(
    {"text", "image", "audio", "video"}
)


@dataclass
class ModelHubBackendModelConfig:
    id: str
    origin: Literal["builtin", "models_dev", "manual"] = "manual"
    display_name: Optional[str] = None
    models_dev_id: Optional[str] = None
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    input_modalities: list[str] = field(default_factory=list)
    output_modalities: list[str] = field(default_factory=list)
    supports_tools: Optional[bool] = None
    supports_reasoning: Optional[bool] = None
    reasoning_efforts: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: object) -> "ModelHubBackendModelConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.agents.models' entries must be objects")
        allowed = {
            "id",
            "display_name",
            "origin",
            "models_dev_id",
            "context_window",
            "max_output_tokens",
            "input_modalities",
            "output_modalities",
            "supports_tools",
            "supports_reasoning",
            "reasoning_efforts",
            # These are read-only API projection fields. Accepting and dropping
            # them lets a client submit the list it observed without turning
            # server-owned policy into persisted state.
            "locked",
            "routeable",
        }
        if set(payload) - allowed:
            raise ValueError("Config 'model_hub.agents.models' contains unknown fields")
        model_id = payload.get("id")
        origin = payload.get("origin", "manual")
        display_name = payload.get("display_name")
        models_dev_id = payload.get("models_dev_id")
        context_window = payload.get("context_window")
        max_output_tokens = payload.get("max_output_tokens")
        input_modalities = payload.get("input_modalities", [])
        output_modalities = payload.get("output_modalities", [])
        supports_tools = payload.get("supports_tools")
        supports_reasoning = payload.get("supports_reasoning")
        reasoning_efforts = payload.get("reasoning_efforts", [])

        if (
            not isinstance(model_id, str)
            or not model_id.strip()
            or _contains_model_hub_credential_material(model_id)
        ):
            raise ValueError("Config 'model_hub.agents.models.id' is invalid")
        if origin not in {"builtin", "models_dev", "manual"}:
            raise ValueError("Config 'model_hub.agents.models.origin' is invalid")
        for name, value in (
            ("display_name", display_name),
            ("models_dev_id", models_dev_id),
        ):
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or _contains_model_hub_credential_material(value)
            ):
                raise ValueError(f"Config 'model_hub.agents.models.{name}' is invalid")
        for name, value in (
            ("context_window", context_window),
            ("max_output_tokens", max_output_tokens),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"Config 'model_hub.agents.models.{name}' is invalid")
        for name, values, allowed_modalities in (
            (
                "input_modalities",
                input_modalities,
                _BACKEND_MODEL_INPUT_MODALITIES,
            ),
            (
                "output_modalities",
                output_modalities,
                _BACKEND_MODEL_OUTPUT_MODALITIES,
            ),
        ):
            if (
                not isinstance(values, list)
                or any(
                    not isinstance(value, str)
                    or value not in allowed_modalities
                    for value in values
                )
                or len(set(values)) != len(values)
            ):
                raise ValueError(f"Config 'model_hub.agents.models.{name}' is invalid")
        for name, value in (
            ("supports_tools", supports_tools),
            ("supports_reasoning", supports_reasoning),
        ):
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"Config 'model_hub.agents.models.{name}' is invalid")
        if (
            not isinstance(reasoning_efforts, list)
            or any(
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 64
                or _contains_model_hub_credential_material(value)
                for value in reasoning_efforts
            )
            or len(set(reasoning_efforts)) != len(reasoning_efforts)
        ):
            raise ValueError(
                "Config 'model_hub.agents.models.reasoning_efforts' is invalid"
            )
        return cls(
            id=model_id.strip(),
            origin=origin,
            display_name=display_name.strip() if isinstance(display_name, str) else None,
            models_dev_id=models_dev_id.strip() if isinstance(models_dev_id, str) else None,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            input_modalities=list(input_modalities),
            output_modalities=list(output_modalities),
            supports_tools=supports_tools,
            supports_reasoning=supports_reasoning,
            reasoning_efforts=list(reasoning_efforts),
        )

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "origin": self.origin,
            "models_dev_id": self.models_dev_id,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "supports_tools": self.supports_tools,
            "supports_reasoning": self.supports_reasoning,
            "reasoning_efforts": list(self.reasoning_efforts),
        }


def _default_backend_models(backend: str) -> list[ModelHubBackendModelConfig]:
    if backend not in {"claude", "codex"}:
        return []
    from vibe.backend_model_catalog import backend_model_entries, load_bundled_catalog

    return [
        ModelHubBackendModelConfig(
            id=entry["id"],
            origin="builtin",
            display_name=entry.get("label"),
            reasoning_efforts=list(entry.get("reasoning_efforts") or ()),
        )
        for entry in backend_model_entries(backend, load_bundled_catalog())
    ]


@dataclass
class ModelHubAgentSourcesConfig:
    order: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict) -> "ModelHubAgentSourcesConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.agents.sources' must be an object")
        if set(payload) != {"order"}:
            raise ValueError("Config 'model_hub.agents.sources' must contain only order")
        order = payload.get("order")
        if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
            raise ValueError("Config 'model_hub.agents.sources.order' must be an array of strings")
        if len(set(order)) != len(order):
            raise ValueError("Config 'model_hub.agents.sources.order' must be unique")
        return cls(order=list(order))

    def to_payload(self) -> dict:
        return {"order": list(self.order)}


@dataclass
class ModelHubAgentSupplyConfig:
    backend: Literal["claude", "codex", "opencode"]
    mode: Literal["hub", "direct"]
    menu_kind: Literal["fixed", "open"]
    sources: ModelHubAgentSourcesConfig = field(default_factory=ModelHubAgentSourcesConfig)
    routes: dict[str, ModelHubRouteConfig] = field(default_factory=dict)
    menu: Optional[ModelHubMenuConfig] = None
    models: list[ModelHubBackendModelConfig] = field(default_factory=list)

    @classmethod
    def default(cls, backend: str, *, mode: Literal["hub", "direct"]) -> "ModelHubAgentSupplyConfig":
        if backend == "opencode":
            return cls(
                backend="opencode",
                mode=mode,
                menu_kind="open",
                menu=ModelHubMenuConfig(),
            )
        if backend not in {"claude", "codex"}:
            raise ValueError(f"Unsupported Model Hub backend: {backend}")
        return cls(
            backend=backend,
            mode=mode,
            menu_kind="fixed",
            routes={model_id: ModelHubRouteConfig() for model_id in model_hub_fixed_menu_ids(backend)},
            models=_default_backend_models(backend),
        )

    @classmethod
    def from_payload(
        cls,
        payload: dict,
        *,
        expected_backend: Optional[str] = None,
        repairing: bool = False,
    ) -> "ModelHubAgentSupplyConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub.agents' entries must be objects")
        if set(payload) - {
            "backend",
            "mode",
            "menu_kind",
            "sources",
            "routes",
            "menu",
            "models",
        }:
            raise ValueError("Config 'model_hub.agents' contains unknown fields")
        raw_backend = payload.get("backend")
        backend = expected_backend if raw_backend is None else raw_backend
        mode = payload.get("mode")
        menu_kind = payload.get("menu_kind")
        if not isinstance(backend, str) or backend not in {"claude", "codex", "opencode"} or (
            expected_backend is not None and backend != expected_backend
        ):
            raise ValueError("Config 'model_hub.agents.backend' is invalid")
        if not isinstance(mode, str) or mode not in {"hub", "direct"}:
            raise ValueError("Config 'model_hub.agents.mode' is invalid")
        expected_menu_kind = "open" if backend == "opencode" else "fixed"
        if not isinstance(menu_kind, str) or menu_kind != expected_menu_kind:
            raise ValueError("Config 'model_hub.agents.menu_kind' is invalid for backend")
        routes_payload = payload.get("routes", {})
        if not isinstance(routes_payload, dict):
            raise ValueError("Config 'model_hub.agents.routes' must be an object")
        if any(not isinstance(model_id, str) or not model_id for model_id in routes_payload):
            raise ValueError("Config 'model_hub.agents.routes' keys must be non-empty strings")
        menu_payload = payload.get("menu")
        sources_payload = payload.get("sources")
        if backend == "opencode" and menu_payload is None:
            menu_payload = {"view": "featured", "checked": []}
        if backend != "opencode" and menu_payload is not None:
            raise ValueError("Config 'model_hub.agents.menu' is only valid for opencode")
        routes = {
            model_id: ModelHubRouteConfig.from_payload(route, repairing=repairing)
            for model_id, route in routes_payload.items()
        }
        menu = ModelHubMenuConfig.from_payload(menu_payload) if menu_payload is not None else None
        models_payload = payload.get("models")
        if models_payload is None:
            if backend == "opencode":
                legacy_ids = list(menu.checked if menu else ())
                legacy_ids.extend(
                    model_id for model_id in routes if model_id not in legacy_ids
                )
                models = [
                    ModelHubBackendModelConfig(id=model_id, origin="manual")
                    for model_id in legacy_ids
                ]
            else:
                models = _default_backend_models(backend)
                known = {model.id for model in models}
                models.extend(
                    ModelHubBackendModelConfig(id=model_id, origin="manual")
                    for model_id in routes
                    if model_id not in known
                )
        else:
            if not isinstance(models_payload, list):
                raise ValueError("Config 'model_hub.agents.models' must be an array")
            models = [
                ModelHubBackendModelConfig.from_payload(item)
                for item in models_payload
            ]
        if len({model.id for model in models}) != len(models):
            raise ValueError("Config 'model_hub.agents.models' ids must be unique")
        if any(_contains_model_hub_credential_material(model_id) for model_id in routes):
            raise ValueError("Config 'model_hub.agents.routes' contains an invalid model id")
        if backend == "opencode":
            for identifier in (*routes, *(model.id for model in models)):
                canonical_opencode_menu_identity(identifier)
            menu = ModelHubMenuConfig(
                view=menu.view if menu else "featured",
                checked=[model.id for model in models],
            )
        return cls(
            backend=backend,
            mode=mode,
            menu_kind=menu_kind,
            sources=(
                ModelHubAgentSourcesConfig.from_payload(sources_payload)
                if sources_payload is not None
                else ModelHubAgentSourcesConfig()
            ),
            routes=routes,
            menu=menu,
            models=models,
        )

    def to_payload(self) -> dict:
        payload = {
            "backend": self.backend,
            "mode": self.mode,
            "menu_kind": self.menu_kind,
            "sources": self.sources.to_payload(),
            "routes": {
                model_id: route.to_payload()
                for model_id, route in self.routes.items()
            },
            "menu": self.menu.to_payload() if self.menu else None,
        }
        payload["models"] = [model.to_payload() for model in self.models]
        return payload


@dataclass
class ModelHubConfig:
    enabled: bool = False
    sources: list[ModelHubSourceConfig] = field(default_factory=list)
    agents: dict[str, ModelHubAgentSupplyConfig] = field(
        default_factory=lambda: {
            backend: ModelHubAgentSupplyConfig.default(backend, mode="direct") for backend in MODEL_HUB_BACKENDS
        }
    )

    @staticmethod
    def source_eligible_for_backend(
        source: ModelHubSourceConfig,
        backend: str,
    ) -> bool:
        if backend not in MODEL_HUB_BACKENDS:
            return False
        if source.supply_channel == "hub":
            return True
        if source.kind == "api_key":
            return False
        expected_backend = {"anthropic": "claude", "openai": "codex"}.get(source.vendor)
        return expected_backend == backend

    def recommended_source_order(self, backend: str) -> list[str]:
        def sort_key(source: ModelHubSourceConfig) -> tuple[object, ...]:
            if source.kind == "subscription":
                return (
                    0,
                    0 if source.supply_channel == "native_cli" else 1,
                    source.id,
                )
            created_at = datetime.fromisoformat(source.created_at.replace("Z", "+00:00"))
            return (1, created_at.timestamp(), source.id)

        return [
            source.id
            for source in sorted(self.sources, key=sort_key)
            if self.source_eligible_for_backend(source, backend)
        ]

    def effective_source_order(self, backend: str) -> list[str]:
        return list(self.agents[backend].sources.order)

    @classmethod
    def from_payload(cls, payload: dict, *, repairing: bool = False) -> "ModelHubConfig":
        if not isinstance(payload, dict):
            raise ValueError("Config 'model_hub' must be an object")
        if set(payload) - {"enabled", "sources", "agents"}:
            raise ValueError("Config 'model_hub' contains unknown fields")
        enabled = payload.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("Config 'model_hub.enabled' must be a boolean")
        sources_payload = payload.get("sources") or []
        agents_payload = payload.get("agents") or {}
        if not isinstance(sources_payload, list):
            raise ValueError("Config 'model_hub.sources' must be an array")
        if not isinstance(agents_payload, dict):
            raise ValueError("Config 'model_hub.agents' must be an object")
        if set(agents_payload) - set(MODEL_HUB_BACKENDS):
            raise ValueError("Config 'model_hub.agents' contains unknown backends")
        sources = [
            ModelHubSourceConfig.from_payload(source, repairing=repairing)
            for source in sources_payload
        ]
        source_ids = [source.id for source in sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("Config 'model_hub.sources' contains duplicate ids")
        source_nonces = [
            source.client_nonce
            for source in sources
            if source.client_nonce is not None
        ]
        if len(set(source_nonces)) != len(source_nonces):
            raise ValueError("Config 'model_hub.sources' contains duplicate client nonces")
        for backend in MODEL_HUB_BACKENDS:
            native_sources = [
                source
                for source in sources
                if source.supply_channel == "native_cli"
                and cls.source_eligible_for_backend(source, backend)
            ]
            if len(native_sources) > 1:
                raise ValueError(
                    f"Config 'model_hub.sources' contains multiple native sources for '{backend}'"
                )
        agents = {}
        for backend in MODEL_HUB_BACKENDS:
            if backend not in agents_payload:
                raw_agent = ModelHubAgentSupplyConfig.default(backend, mode="direct").to_payload()
            else:
                raw_agent = agents_payload[backend]
                if not isinstance(raw_agent, dict):
                    raise ValueError(f"Config 'model_hub.agents.{backend}' must be an object")
                if "routes" not in raw_agent:
                    raise ValueError(f"Config 'model_hub.agents.{backend}.routes' is required")
            agents[backend] = ModelHubAgentSupplyConfig.from_payload(
                raw_agent,
                expected_backend=backend,
                repairing=repairing,
            )
            expected_menu_ids = tuple(
                model.id for model in agents[backend].models
            )
            missing_route = next(
                (model_id for model_id in expected_menu_ids if model_id not in agents[backend].routes),
                None,
            )
            if missing_route is not None:
                raise ValueError(
                    f"Config 'model_hub.agents.{backend}.routes' is missing menu model '{missing_route}'"
                )
            extra_route = next(
                (
                    model_id
                    for model_id in agents[backend].routes
                    if model_id not in expected_menu_ids
                ),
                None,
            )
            # The retained OpenCode menu endpoint can hide a checked model while
            # preserving its dormant Route for a later re-enable. Fixed-menu
            # backends have never had that compatibility state.
            if extra_route is not None and backend != "opencode":
                raise ValueError(
                    f"Config 'model_hub.agents.{backend}.routes' contains non-menu model '{extra_route}'"
                )
        config = cls(
            enabled=enabled,
            sources=sources,
            agents=agents,
        )
        for backend in MODEL_HUB_BACKENDS:
            configured_sources = agents[backend].sources
            invalid_id = next(
                (
                    source_id
                    for source_id in configured_sources.order
                    if source_id not in source_ids
                    or not config.source_eligible_for_backend(
                        next(source for source in sources if source.id == source_id),
                        backend,
                    )
                ),
                None,
            )
            if invalid_id is not None:
                raise ValueError(
                    f"Config 'model_hub.agents.{backend}.sources.order' contains "
                    f"ineligible or missing source '{invalid_id}'"
                )
            for model_id, route in agents[backend].routes.items():
                for hop in route.hops:
                    source = next((item for item in sources if item.id == hop.source_id), None)
                    if source is None:
                        raise ValueError(
                            f"Config 'model_hub.agents.{backend}.routes.{model_id}' references missing source '{hop.source_id}'"
                        )
                    if not config.source_eligible_for_backend(source, backend):
                        raise ValueError(
                            f"Config 'model_hub.agents.{backend}.routes.{model_id}' references ineligible source '{hop.source_id}'"
                        )
        return config

    def to_payload(self) -> dict:
        return {
            "enabled": self.enabled,
            "sources": [source.to_payload() for source in self.sources],
            "agents": {backend: self.agents[backend].to_payload() for backend in MODEL_HUB_BACKENDS},
        }


@dataclass
class UiConfig:
    setup_host: str = "127.0.0.1"
    setup_port: int = 5123
    open_browser: bool = True
    chat_message_font_size: int = DEFAULT_CHAT_MESSAGE_FONT_SIZE_PX
    # When true, the Web Chat renders each agent turn's intermediate activity
    # (interim ``assistant`` messages + ``tool_call`` summaries) as a collapsible
    # group, and the message mirror streams those rows live. Default off: a strict
    # no-op — the live stream and transcript stay exactly as they are today.
    show_agent_activity: bool = False
    # When true (default), the Activity panel renders tool-call rows; when false,
    # only assistant narration rows show. Pure display filter — step counts,
    # durations, and data collection are unaffected. Independent of
    # ``show_agent_activity`` (which gates the whole panel); this only filters rows
    # within it. Default on so the panel keeps today's full detail unless hidden.
    show_tool_calls: bool = True
    trusted_public_origins: List[str] = field(default_factory=list)
    # Display name appended to the browser tab title ("Avibe - <name>"). When
    # blank the UI falls back to the read-only ``default_instance_name`` field
    # in the /api/config payload (remote-access tunnel name when available,
    # otherwise the machine's system hostname).
    instance_name: str = ""

    def __post_init__(self) -> None:
        """Refuse a value that is not the kind this class declares.

        ``from_payload`` copies this section verbatim and an Editor may now
        persist part of it, so every field here is reachable from a stale
        client or a hand-edited file. Held to the declared kinds rather than to
        the two switches that had call-site checks, which is what closes
        ``open_browser`` and ``setup_port`` — a declared switch and a declared
        port that nothing on this path validated at all.

        The switches keep reading the spellings a hand-edited config may use
        (``"yes"``, ``"0"``, ``1``), which ``test_show_agent_activity_defaults_
        off_and_round_trips`` states; that is why the normalisation happens here
        rather than in a check that runs before it.
        """
        _refuse_values_naming_nothing("ui", self)


@dataclass
class VibeCloudRemoteAccessConfig:
    enabled: bool = False
    backend_url: str = "https://avibe.bot"
    public_url: str = ""
    instance_id: str = ""
    instance_kind: str = ""
    client_id: str = ""
    issuer: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    jwks_uri: str = ""
    redirect_uri: str = ""
    tunnel_token: str = ""
    instance_secret: str = ""
    session_secret: str = ""
    cloudflared_path: str = ""
    transport_protocol: str = "auto"
    auto_recovery: bool = True
    optimization_profile: str = "balanced"
    edge_ip_version: str = "4"
    edge_bind_address: str = ""
    dev_login_hint: str = ""

    def __post_init__(self) -> None:
        """Refuse a value that is not the kind this class declares.

        ``from_payload`` copies this section's free-form fields verbatim, so a
        hand-edited, migrated, or stale-client payload can hold a number where
        a URL or credential belongs. Stated over the declared fields rather
        than over the three the pairing predicate happens to read: any of them
        can reach code that does string work, and ``is_runtime_paired`` is now
        consulted on every ``/api/config`` response, which would turn one
        malformed field into a 500 on every configuration read.

        Refused rather than emptied. Emptying is a repair, and a repair here
        writes a credential the operator never stored: clearing
        ``session_secret`` logs every remote session out, and clearing
        ``instance_secret`` unpairs the instance — answered ``200``, on a route
        an Editor can now reach. Refusing sends a disk load through
        ``V2Config.load``'s recovery instead, which backs the file up, restores
        this section, and warns; the "degrade to not paired" outcome is
        unchanged and now leaves evidence. Enforced here rather than in
        ``from_payload`` so every construction path holds the invariant.

        ``instance_kind`` is the one field exempt from that, because for it
        "unreadable" and "unknown" are the same answer and ``""`` already means
        unknown — the value a pairing this build does not recognise is meant to
        carry. It is server-set at pairing and absent from every write surface,
        so nothing an operator types reaches it.
        """
        if self.instance_kind not in ("personal", "organization"):
            self.instance_kind = ""
        _refuse_values_naming_nothing("remote_access.vibe_cloud", self)

    def runtime_credentials(self) -> tuple[str, str, str] | None:
        """Return ``(backend_url, instance_id, instance_secret)``, or ``None``.

        The single definition of "this pairing can actually reach Avibe
        Cloud". A recovered, migrated, or partially configured instance can
        carry ``enabled`` and an ``instance_id`` while the secret every cloud
        call needs is gone, so identifiers alone are not a readiness signal.
        Runtime services call this to build their client; config projections
        call ``is_runtime_paired`` to decide whether to offer the matching
        control. One predicate, so a surface cannot promise what the runtime
        will refuse.
        """
        if not self.enabled:
            return None
        backend_url = (self.backend_url or "").strip().rstrip("/")
        instance_id = (self.instance_id or "").strip()
        instance_secret = (self.instance_secret or "").strip()
        if not backend_url or not instance_id or not instance_secret:
            return None
        return backend_url, instance_id, instance_secret

    def is_runtime_paired(self) -> bool:
        """Whether cloud-backed features can run against this pairing."""
        return self.runtime_credentials() is not None


@dataclass
class RemoteAccessConfig:
    provider: str = "vibe_cloud"
    vibe_cloud: VibeCloudRemoteAccessConfig = field(default_factory=VibeCloudRemoteAccessConfig)


@dataclass
class UpdateConfig:
    """Configuration for automatic update checking and installation."""

    auto_update: bool = True  # Auto-install updates when idle
    check_interval_minutes: int = 60  # How often to check for updates (0 = disable)
    idle_minutes: int = 30  # Minutes of inactivity before auto-update
    notify_admins: bool = True  # Send update notification to admins when update is available


@dataclass
class PlatformsConfig:
    """Multi-platform enablement metadata.

    ``primary`` remains the compatibility anchor for legacy single-platform
    code paths while ``enabled`` is the new source of truth.
    """

    enabled: list[str] = field(default_factory=lambda: ["slack"])
    primary: str = "slack"

    def validate(self) -> None:
        supported = supported_platform_set()
        normalized: list[str] = []
        for platform in self.enabled:
            if platform not in supported:
                raise ValueError(f"Unsupported enabled platform: {platform}")
            # The in-process workbench is never an enabled IM transport — the
            # controller wires it directly. Strip it from ``enabled`` so a
            # legacy/hand-edited config can't crash the IM factory or strand the
            # primary when a real IM is also enabled.
            if is_workbench_platform(platform):
                continue
            if platform not in normalized:
                normalized.append(platform)
        if not normalized:
            # Workbench-only install: no external IM platform is enabled. The
            # Avibe Workbench (in-process Web UI) is the sole inbound surface,
            # so anchor ``primary`` to it instead of force-inserting a real IM.
            # ``avibe`` is a registered platform but is intentionally NOT added
            # to ``enabled`` — it has no remote runtime and the controller wires
            # it as the in-process client directly (see ``_init_modules``).
            self.primary = WORKBENCH_PLATFORM_ID
            self.enabled = []
            return
        if is_workbench_platform(self.primary):
            # Real IM platforms are enabled, so the workbench can't be the
            # primary transport — retarget to the first real platform.
            self.primary = normalized[0]
        elif self.primary not in supported:
            supported_text = "', '".join(supported_platform_ids())
            raise ValueError(f"Config 'platforms.primary' must be one of: '{supported_text}'")
        elif self.primary not in normalized:
            # ``enabled`` is the source of truth and ``primary`` is now an
            # internal default with no user-facing control. A primary that is
            # not in the enabled set (e.g. a stale value surviving a deep config
            # merge after the platform was disabled) must FOLLOW enabled, not
            # resurrect a removed platform by forcing itself back into the list.
            self.primary = normalized[0]
        self.enabled = normalized


@dataclass
class V2Config:
    mode: str
    version: str
    slack: SlackConfig
    runtime: RuntimeConfig
    agents: AgentsConfig
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    model_hub: ModelHubConfig = field(default_factory=ModelHubConfig)
    platform: str = "slack"
    platforms: PlatformsConfig = field(default_factory=PlatformsConfig)
    discord: Optional[DiscordConfig] = None
    telegram: Optional[TelegramConfig] = None
    lark: Optional[LarkConfig] = None
    wechat: Optional[WeChatConfig] = None
    # Always present: Avibe is in-process and has no credentials, so legacy
    # configs that pre-date the platform still get a usable adapter.
    avibe: AvibeConfig = field(default_factory=AvibeConfig)
    platform_configs: dict[str, BaseIMConfig] = field(default_factory=dict)
    gateway: Optional[GatewayConfig] = None
    ui: UiConfig = field(default_factory=UiConfig)
    remote_access: RemoteAccessConfig = field(default_factory=RemoteAccessConfig)
    audio_asr: AudioAsrConfig = field(default_factory=AudioAsrConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    ack_mode: str = "typing"
    show_duration: bool = False  # Show task duration in result messages
    include_time_info: bool = True  # Prepend current local time to agent messages
    include_user_info: bool = True  # Prepend user identity to agent messages
    reply_enhancements: bool = True  # Enable quick-reply buttons
    show_pages_prompt: bool = True  # Inject Show Pages capability guidance into agent prompts
    language: str = "en"  # Global language setting (see vibe/i18n)
    # Progress UX for editing platforms (Slack/Discord):
    #   "off" (default) no process bubble, "concise" one self-updating bubble,
    #   "verbose" legacy append/split process log.
    agent_progress_style: str = DEFAULT_AGENT_PROGRESS_STYLE
    agent_status_heartbeat_ms: int = 8000  # status-bubble elapsed-timer heartbeat
    agent_status_no_output_ms: int = 180000  # "no output for N min" hint threshold
    # True once the user has finished the setup wizard. This is the explicit
    # gate for ``setup_state().needs_setup`` — it replaces the old heuristic
    # that inferred "setup done" from having a mode plus configured platform
    # credentials (which forced credential-less / workbench-only installs back
    # into the wizard). Legacy configs that predate the flag have it derived in
    # ``from_payload`` from the old condition.
    setup_completed: bool = False
    # Non-persisted diagnostics from disk migration/recovery. They let callers
    # surface a repair notice without weakening the strict write validator.
    load_warnings: ClassVar[tuple[str, ...]] = ()
    recovered_sections: ClassVar[tuple[str, ...]] = ()
    whole_config_recovery: ClassVar[bool] = False

    @property
    def memory_required(self) -> bool | None:
        """Return the persisted Memory requirement, or None when unreadable."""

        if self.whole_config_recovery:
            return None
        return bool(self.memory.enabled)

    @classmethod
    def default(cls) -> "V2Config":
        """Return the minimal safe config used for first-run and recovery."""

        return cls(
            mode="self_host",
            version="v2",
            slack=SlackConfig(bot_token="", app_token=""),
            platforms=PlatformsConfig(enabled=[], primary=WORKBENCH_PLATFORM_ID),
            runtime=RuntimeConfig(default_cwd=str(Path.home() / "work")),
            agents=AgentsConfig(
                opencode=OpenCodeConfig(enabled=True, cli_path="opencode"),
                claude=ClaudeConfig(enabled=True, cli_path="claude"),
                codex=CodexConfig(enabled=False, cli_path="codex"),
            ),
            model_hub=ModelHubConfig(),
            platform=WORKBENCH_PLATFORM_ID,
        )

    @classmethod
    def load(
        cls,
        config_path: Optional[Path] = None,
        *,
        persist_migrations: bool = True,
        _migration_reload_depth: int = 0,
    ) -> "V2Config":
        paths.ensure_data_dirs()
        path = config_path or paths.get_config_path()
        with CONFIG_LOCK:
            if not path.exists():
                raise FileNotFoundError(f"Config not found: {path}")
            raw_bytes = b""
            try:
                raw_bytes = path.read_bytes()
                raw = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                backup = _backup_config_file(path, "invalid-encoding", content=raw_bytes)
                warning = f"Config is not valid UTF-8; using recovery defaults: {exc.reason}"
                logger.error("%s (backup=%s)", warning, backup)
                config = cls.default()
                config.load_warnings = (warning,)
                config.recovered_sections = ()
                config.whole_config_recovery = True
                return config

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            backup = _backup_config_file(path, "invalid-json", content=raw_bytes)
            warning = f"Config JSON could not be parsed; using recovery defaults: {exc.msg}"
            logger.error("%s (backup=%s)", warning, backup)
            config = cls.default()
            config.load_warnings = (warning,)
            config.recovered_sections = ()
            config.whole_config_recovery = True
            return config

        if not isinstance(payload, dict):
            backup = _backup_config_file(path, "invalid-root", content=raw_bytes)
            warning = "Config root is not an object; using recovery defaults"
            logger.error("%s (backup=%s)", warning, backup)
            config = cls.default()
            config.load_warnings = (warning,)
            config.recovered_sections = ()
            config.whole_config_recovery = True
            return config

        migrated_payload, migrated, migration_warnings = _migrate_config_payload_on_load(payload)
        candidate = migrated_payload
        recovery_warnings: list[str] = []
        recovered_sections: set[str] = set()
        whole_config_recovery = False
        while True:
            try:
                config = cls.from_payload(candidate)
                break
            except (TypeError, ValueError) as exc:
                section = _recovery_section_for_error(exc)
                field_name = _recovery_field_for_error(section, exc)
                # Keyed by whatever this pass will actually repair, so the
                # dedupe that stops the loop cannot mistake a second unreadable
                # field of a field-scoped section for no progress and discard
                # the whole file.
                recovered = section if field_name is None else f"{section}.{field_name}"
                if section is None or recovered in recovered_sections:
                    warning = f"Config could not be loaded; using recovery defaults: {exc}"
                    logger.error("%s", warning)
                    config = cls.default()
                    recovery_warnings.append(warning)
                    whole_config_recovery = True
                    break
                if not _reset_recoverable_config_section(candidate, section, field_name):
                    warning = f"Config section '{section}' could not be recovered; using recovery defaults: {exc}"
                    logger.error("%s", warning)
                    config = cls.default()
                    recovery_warnings.append(warning)
                    whole_config_recovery = True
                    break
                recovered_sections.add(recovered)
                recovery_warnings.append(f"Recovered invalid config section '{recovered}': {exc}")

        all_warnings = tuple(dict.fromkeys((*migration_warnings, *recovery_warnings)))
        if (
            persist_migrations
            and migrated
            and not migration_warnings
            and not recovery_warnings
        ):
            persisted_payload = copy.deepcopy(payload)
            persisted_payload["model_hub"] = config.model_hub.to_payload()
            try:
                backup, persistence_warning = _persist_migrated_config_payload(
                    path,
                    raw,
                    persisted_payload,
                )
            except OSError as exc:
                persistence_warning = f"Model Hub config migration could not be persisted: {exc}"
            if persistence_warning:
                all_warnings = tuple(dict.fromkeys((*all_warnings, persistence_warning)))
                logger.warning("%s (%s)", persistence_warning, path)
                if "file changed" in persistence_warning and _migration_reload_depth == 0:
                    winning_config = cls.load(
                        config_path=path,
                        _migration_reload_depth=1,
                    )
                    winning_config.load_warnings = tuple(
                        dict.fromkeys((*winning_config.load_warnings, persistence_warning))
                    )
                    return winning_config
            else:
                logger.info("Migrated Model Hub config in place (backup=%s)", backup)
        elif migration_warnings or recovery_warnings:
            label = "model-hub-migration" if migration_warnings and not recovery_warnings else "recovery"
            backup = _backup_config_file(path, label, content=raw_bytes)
            logger.warning(
                "Started with a recovered config; original file preserved at %s",
                backup,
            )
        config.load_warnings = all_warnings
        config.recovered_sections = tuple(sorted(recovered_sections))
        config.whole_config_recovery = whole_config_recovery
        return config

    @classmethod
    def from_payload(cls, payload: dict) -> "V2Config":
        if not isinstance(payload, dict):
            raise ValueError("Config payload must be an object")

        mode = payload.get("mode")
        if not isinstance(mode, str) or mode not in {"self_host", "saas"}:
            raise ValueError("Config 'mode' must be 'self_host' or 'saas'")

        raw_platform = payload.get("platform")
        platforms_payload = payload.get("platforms")
        if platforms_payload is not None and not isinstance(platforms_payload, dict):
            raise ValueError("Config 'platforms' must be an object")
        if platforms_payload is None:
            if raw_platform is not None and not isinstance(raw_platform, str):
                raise ValueError("Config 'platform' must be a string")
            platform = raw_platform or "slack"
            try:
                get_platform_descriptor(platform)
            except ValueError as err:
                supported_text = "', '".join(supported_platform_ids())
                raise ValueError(f"Config 'platform' must be one of: '{supported_text}'") from err
        else:
            # ``platforms`` is the current source of truth. A stale legacy
            # compatibility field must not disable otherwise valid transports.
            platform = platforms_payload.get("primary") or "slack"
        if platforms_payload:
            enabled_payload = platforms_payload.get("enabled")
            if enabled_payload is not None and not isinstance(enabled_payload, list):
                raise ValueError("Config 'platforms.enabled' must be an array")
            if isinstance(enabled_payload, list) and any(
                not isinstance(item, str) for item in enabled_payload
            ):
                raise ValueError("Config 'platforms.enabled' entries must be strings")
            primary_payload = platforms_payload.get("primary")
            if primary_payload is not None and not isinstance(primary_payload, str):
                raise ValueError("Config 'platforms.primary' must be a string")
            platforms = PlatformsConfig(
                enabled=list(enabled_payload or []),
                primary=primary_payload or platform,
            )
        else:
            platforms = PlatformsConfig(enabled=[platform], primary=platform)
        # When the caller explicitly set 'platform' but did not provide
        # 'platforms', treat it as a legacy single-platform update and
        # sync the new structure so that the old field is not silently
        # overridden by a stale 'platforms' value from a prior merge.
        if "platform" in payload and "platforms" not in payload:
            platforms = PlatformsConfig(enabled=[platform], primary=platform)
        platforms.validate()
        platform = platforms.primary

        platform_configs: dict[str, Optional[BaseIMConfig]] = {}
        for descriptor in platform_descriptors():
            platform_payload = payload.get(descriptor.config_key)
            if descriptor.id == "slack":
                platform_payload = platform_payload or {}
                if isinstance(platform_payload, dict) and "require_mention" not in platform_payload:
                    platform_payload = dict(platform_payload)
                    platform_payload["require_mention"] = False
            if platform_payload is not None and not isinstance(platform_payload, dict):
                raise ValueError(f"Config '{descriptor.config_key}' must be an object")
            if platform_payload is None:
                platform_configs[descriptor.id] = None
                continue

            for credential_field in descriptor.credential_fields:
                credential_value = platform_payload.get(credential_field)
                if credential_value is not None and not isinstance(credential_value, str):
                    raise ValueError(
                        f"Config '{descriptor.config_key}.{credential_field}' must be a string"
                    )

            try:
                platform_configs[descriptor.id] = descriptor.create_config(platform_payload)
            except (AttributeError, TypeError) as exc:
                # Platform validators may call string methods or compare enum
                # values before the dataclass layer can validate their types.
                # Convert those payload errors into a qualified config error so
                # disk loading can recover only the affected platform section.
                raise ValueError(
                    f"Config '{descriptor.config_key}' contains invalid field types"
                ) from exc

        # Avibe runs in-process with no credentials — auto-populate its
        # config when missing so legacy ``platforms.enabled`` lists that
        # mention "avibe" don't trip the validation loop below.
        if platform_configs.get("avibe") is None:
            platform_configs["avibe"] = AvibeConfig()

        # Validate that every enabled platform has its config section present.
        for _ep in platforms.enabled:
            descriptor = get_platform_descriptor(_ep)
            if platform_configs[descriptor.id] is None:
                raise ValueError(f"Config '{descriptor.config_key}' must be provided when {_ep} is enabled")

        gateway_payload = payload.get("gateway")
        if gateway_payload is not None and not isinstance(gateway_payload, dict):
            raise ValueError("Config 'gateway' must be an object")
        gateway = GatewayConfig(**_filter_dataclass_fields(GatewayConfig, gateway_payload)) if gateway_payload else None

        runtime_payload = payload.get("runtime")
        if not isinstance(runtime_payload, dict):
            raise ValueError("Config 'runtime' must be an object")
        runtime = RuntimeConfig(**_filter_dataclass_fields(RuntimeConfig, runtime_payload))

        agents_payload = payload.get("agents")
        if not isinstance(agents_payload, dict):
            raise ValueError("Config 'agents' must be an object")

        opencode_payload = agents_payload.get("opencode") or {}
        if not isinstance(opencode_payload, dict):
            raise ValueError("Config 'agents.opencode' must be an object")

        claude_payload = agents_payload.get("claude") or {}
        if not isinstance(claude_payload, dict):
            raise ValueError("Config 'agents.claude' must be an object")

        codex_payload = agents_payload.get("codex") or {}
        if not isinstance(codex_payload, dict):
            raise ValueError("Config 'agents.codex' must be an object")

        avault_payload = agents_payload.get("avault") or {}
        if not isinstance(avault_payload, dict):
            raise ValueError("Config 'agents.avault' must be an object")

        opencode = OpenCodeConfig(**_filter_dataclass_fields(OpenCodeConfig, opencode_payload))
        claude = ClaudeConfig(**_filter_dataclass_fields(ClaudeConfig, claude_payload))
        codex = CodexConfig(**_filter_dataclass_fields(CodexConfig, codex_payload))
        avault = AVaultConfig(**_filter_dataclass_fields(AVaultConfig, avault_payload))

        agents = AgentsConfig(
            opencode=opencode,
            claude=claude,
            codex=codex,
            avault=avault,
        )

        memory = memory_config_from_payload(payload.get("memory", {}))

        model_hub_payload = payload.get("model_hub")
        if model_hub_payload is None:
            # Existing installs predate Model Hub and remain in Direct mode until
            # the user explicitly opts in after the release capability is enabled.
            model_hub = ModelHubConfig()
        else:
            # The one repairing door, because this is the one caller parsing a
            # document a previous release wrote. Every other entry into these
            # constructors is either a request from a client — which must be told
            # it named a model twice rather than answered with half of what it
            # sent — or a round trip of objects this process already settled. The
            # save paths reach here too, but never with this subtree: the config
            # API drops ``model_hub`` from what the client sends, so the payload
            # under this key is always what was last read off disk.
            model_hub = ModelHubConfig.from_payload(model_hub_payload, repairing=True)

        ui_payload = payload.get("ui") or {}
        if not isinstance(ui_payload, dict):
            raise ValueError("Config 'ui' must be an object")
        ui = UiConfig(**_filter_dataclass_fields(UiConfig, ui_payload))
        # ``UiConfig.__post_init__`` has already held every field to its declared
        # kind, so this is only the range policy: clamping an out-of-range size
        # still honours what the caller asked for.
        ui.chat_message_font_size = max(
            MIN_CHAT_MESSAGE_FONT_SIZE_PX,
            min(MAX_CHAT_MESSAGE_FONT_SIZE_PX, ui.chat_message_font_size),
        )

        remote_access_payload = payload.get("remote_access") or {}
        if not isinstance(remote_access_payload, dict):
            raise ValueError("Config 'remote_access' must be an object")
        remote_access_provider = remote_access_payload.get("provider") or "vibe_cloud"
        if remote_access_provider != "vibe_cloud":
            raise ValueError("Config 'remote_access.provider' must be 'vibe_cloud'")
        vibe_cloud_payload = remote_access_payload.get("vibe_cloud") or {}
        if not isinstance(vibe_cloud_payload, dict):
            raise ValueError("Config 'remote_access.vibe_cloud' must be an object")
        remote_access = RemoteAccessConfig(
            provider=remote_access_provider,
            vibe_cloud=VibeCloudRemoteAccessConfig(
                **_filter_dataclass_fields(VibeCloudRemoteAccessConfig, vibe_cloud_payload)
            ),
        )
        remote_access.vibe_cloud.transport_protocol = str(
            remote_access.vibe_cloud.transport_protocol or "auto"
        ).strip().lower()
        if remote_access.vibe_cloud.transport_protocol not in {"auto", "quic", "http2"}:
            raise ValueError(
                "Config 'remote_access.vibe_cloud.transport_protocol' must be 'auto', 'quic', or 'http2'"
            )
        # ``auto_recovery`` reads the same spellings every other switch does;
        # ``VibeCloudRemoteAccessConfig.__post_init__`` has already resolved it,
        # where the truthy/else it used to have would have read ``"garbage"`` as
        # off and a stale client's ``[1]`` as on.
        remote_access.vibe_cloud.optimization_profile = str(
            remote_access.vibe_cloud.optimization_profile or "balanced"
        ).strip().lower()
        if remote_access.vibe_cloud.optimization_profile not in {
            "stable",
            "balanced",
            "low_latency",
        }:
            raise ValueError(
                "Config 'remote_access.vibe_cloud.optimization_profile' must be "
                "'stable', 'balanced', or 'low_latency'"
            )
        remote_access.vibe_cloud.edge_ip_version = str(
            remote_access.vibe_cloud.edge_ip_version or "4"
        ).strip().lower()
        if remote_access.vibe_cloud.edge_ip_version not in {"auto", "4", "6"}:
            raise ValueError(
                "Config 'remote_access.vibe_cloud.edge_ip_version' must be 'auto', '4', or '6'"
            )
        remote_access.vibe_cloud.edge_bind_address = str(
            remote_access.vibe_cloud.edge_bind_address or ""
        ).strip()
        if remote_access.vibe_cloud.edge_bind_address:
            try:
                remote_access.vibe_cloud.edge_bind_address = str(
                    ipaddress.ip_address(remote_access.vibe_cloud.edge_bind_address)
                )
            except ValueError as exc:
                raise ValueError(
                    "Config 'remote_access.vibe_cloud.edge_bind_address' must be an IP address"
                ) from exc

        audio_asr_payload = payload.get("audio_asr") or {}
        if not isinstance(audio_asr_payload, dict):
            raise ValueError("Config 'audio_asr' must be an object")
        audio_asr_enabled_present = "enabled" in audio_asr_payload
        audio_asr = AudioAsrConfig(**_filter_dataclass_fields(AudioAsrConfig, audio_asr_payload))
        if audio_asr_enabled_present and audio_asr.enabled is False and not audio_asr.enabled_configured:
            audio_asr.enabled_configured = True
        # ``AudioAsrConfig.__post_init__`` has already held every field to its
        # declared kind, so what is left here is range and format policy: a
        # value that names a legal setting is clamped to the nearest legal one.
        audio_asr.timeout_seconds = max(0.1, audio_asr.timeout_seconds)
        if audio_asr.max_file_bytes is not None:
            audio_asr.max_file_bytes = max(1, audio_asr.max_file_bytes)
        if not audio_asr.endpoint_path.startswith("/"):
            raise ValueError("Config 'audio_asr.endpoint_path' must be a path starting with '/'")
        if not audio_asr.model.strip():
            raise ValueError("Config 'audio_asr.model' must be a non-empty string")

        update_payload = payload.get("update") or {}
        if not isinstance(update_payload, dict):
            raise ValueError("Config 'update' must be an object")
        # Backward compat: rename legacy "notify_slack" → "notify_admins"
        if "notify_slack" in update_payload and "notify_admins" not in update_payload:
            update_payload["notify_admins"] = update_payload.pop("notify_slack")
        update = UpdateConfig(**_filter_dataclass_fields(UpdateConfig, update_payload))

        ack_mode = payload.get("ack_mode", "typing")
        if not isinstance(ack_mode, str) or ack_mode not in {"reaction", "message", "typing"}:
            raise ValueError("Config 'ack_mode' must be 'reaction', 'message', or 'typing'")

        # Every preference below answers a wrong-typed value the way
        # ``ack_mode`` above already does — by raising. This module's split is
        # that ``from_payload`` validates and only disk loading recovers (see
        # ``_reset_recoverable_config_section``), so a preference that quietly
        # substituted its default broke the split in both directions: over HTTP
        # the route answered 200 having stored a value the caller never sent
        # (turning an enabled preference off on a request that asked to turn it
        # on), and on disk the recovery entries written for these very fields
        # were unreachable, so a hand-edit degraded with no warning.
        def _declared_bool(name: str, default: bool) -> bool:
            value = payload.get(name, default)
            if not isinstance(value, bool):
                raise ValueError(f"Config '{name}' must be a boolean")
            return value

        show_duration = _declared_bool("show_duration", False)
        include_user_info = _declared_bool("include_user_info", True)
        include_time_info = _declared_bool("include_time_info", True)
        reply_enhancements = _declared_bool("reply_enhancements", True)
        show_pages_prompt = _declared_bool("show_pages_prompt", True)

        language = normalize_language(payload.get("language"), default="en")

        agent_progress_style = payload.get("agent_progress_style", DEFAULT_AGENT_PROGRESS_STYLE)
        if agent_progress_style not in ("concise", "verbose", "off"):
            raise ValueError("Config 'agent_progress_style' must be 'concise', 'verbose', or 'off'")

        def _positive_int(value, default, maximum):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > maximum:
                return default
            return value

        # Cap to sane upper bounds so a fat-fingered value can't silence the
        # heartbeat (heartbeat ≤ 1h, no-output hint ≤ 24h); out-of-range → default.
        # These two keep the fallback the settings above give up: they are not on
        # the Settings write surface, and ``test_status_bubble_settings`` states
        # the tolerance as a contract rather than as an accident.
        agent_status_heartbeat_ms = _positive_int(payload.get("agent_status_heartbeat_ms"), 8000, 3_600_000)
        agent_status_no_output_ms = _positive_int(payload.get("agent_status_no_output_ms"), 180000, 86_400_000)

        # ``setup_completed`` is the explicit setup gate. Read the stored value
        # when present; otherwise leave it ``None`` here and derive it below
        # from the legacy "setup done" heuristic so installs configured before
        # this flag existed are not bounced back into the wizard.
        setup_completed = payload.get("setup_completed")
        if setup_completed is not None and not isinstance(setup_completed, bool):
            raise ValueError("Config 'setup_completed' must be a boolean")

        config = cls(
            platform=platform,
            platforms=platforms,
            mode=mode,
            version=payload.get("version", "v2"),
            slack=platform_configs["slack"],
            discord=platform_configs["discord"],
            telegram=platform_configs["telegram"],
            lark=platform_configs["lark"],
            wechat=platform_configs["wechat"],
            # Default Avibe to an enabled instance when the payload is missing
            # the section — legacy configs predate the platform.
            avibe=platform_configs.get("avibe") or AvibeConfig(),
            platform_configs={key: value for key, value in platform_configs.items() if value is not None},
            runtime=runtime,
            agents=agents,
            memory=memory,
            model_hub=model_hub,
            gateway=gateway,
            ui=ui,
            remote_access=remote_access,
            audio_asr=audio_asr,
            update=update,
            ack_mode=ack_mode,
            show_duration=show_duration,
            include_time_info=include_time_info,
            include_user_info=include_user_info,
            reply_enhancements=reply_enhancements,
            show_pages_prompt=show_pages_prompt,
            language=language,
            agent_progress_style=agent_progress_style,
            agent_status_heartbeat_ms=agent_status_heartbeat_ms,
            agent_status_no_output_ms=agent_status_no_output_ms,
        )

        # Migration: when the payload predates ``setup_completed``, derive it
        # from the legacy heuristic (a mode plus at least one configured
        # platform). Only derive when the key is absent; an explicitly stored
        # value always wins.
        if setup_completed is None:
            setup_completed = bool(config.mode) and bool(config.configured_platforms())
        config.setup_completed = setup_completed

        return config

    def save(self, config_path: Optional[Path] = None) -> None:
        """Persist non-Memory changes while preserving the durable Memory unit.

        WARNING — cross-process lost updates: this writes the object's
        FULL snapshot. If this ``V2Config`` was loaded earlier (another
        request, minutes ago) while a different process (UI API server
        vs controller, both write this file) committed changes since,
        saving here silently reverts them. UI/controller writers must
        instead declare their change through ``update_config_fields``
        (see docs/plans/config-cross-process-write-serialization.md),
        which loads fresh inside the cross-process file lock and only
        applies the caller's fields. Direct load → mutate → ``save()``
        cycles are only acceptable in single-writer contexts.
        """

        if self.load_warnings:
            raise ValueError(
                "Config was loaded with recovery warnings; repair the backed-up "
                "config before saving changes"
            )
        paths.ensure_data_dirs()
        path = config_path or paths.get_config_path()
        with config_file_lock(path):
            try:
                memory = type(self).load(path).memory
            except FileNotFoundError:
                memory = self.memory
            self._write_locked(path, memory=memory)

    def _write_locked(self, path: Path, *, memory: MemoryConfig) -> None:
        """Write an exact snapshot while the config transaction is held."""

        self.platforms.validate()
        memory.validate()
        self.platform = self.platforms.primary
        platform_payload = {}
        for descriptor in platform_descriptors():
            descriptor_config = descriptor.get_config(self)
            config_payload = descriptor_config.__dict__.copy() if descriptor_config else None
            if descriptor.id == "discord" and isinstance(config_payload, dict):
                if not config_payload.get("guild_allowlist") and not config_payload.get("guild_denylist"):
                    config_payload.pop("guild_allowlist", None)
                    config_payload.pop("guild_denylist", None)
            platform_payload[descriptor.config_key] = config_payload
        payload = {
            "platform": self.platform,
            "platforms": {
                "enabled": self.platforms.enabled,
                "primary": self.platforms.primary,
            },
            "mode": self.mode,
            "version": self.version,
            **platform_payload,
            "runtime": {
                "default_cwd": self.runtime.default_cwd,
                "log_level": self.runtime.log_level,
                "show_page_api_timeout_seconds": self.runtime.show_page_api_timeout_seconds,
                "resource_governance": self.runtime.resource_governance,
                "harness_run_sweep_interval_seconds": self.runtime.harness_run_sweep_interval_seconds,
                "harness_run_orphan_grace_seconds": self.runtime.harness_run_orphan_grace_seconds,
                "harness_run_queued_ttl_seconds": self.runtime.harness_run_queued_ttl_seconds,
                "harness_run_hold_ttl_seconds": self.runtime.harness_run_hold_ttl_seconds,
                "harness_prompt_echo": self.runtime.harness_prompt_echo,
                "agent_events_trace_retention_enabled": self.runtime.agent_events_trace_retention_enabled,
                "agent_events_trace_retention_days": self.runtime.agent_events_trace_retention_days,
            },
            "agents": {
                "opencode": self.agents.opencode.__dict__,
                "claude": self.agents.claude.__dict__,
                "codex": self.agents.codex.__dict__,
                "avault": self.agents.avault.__dict__,
            },
            "memory": memory_config_to_payload(
                memory,
                include_secrets=True,
                include_internal=True,
            ),
            "model_hub": self.model_hub.to_payload(),
            "gateway": self.gateway.__dict__ if self.gateway else None,
            "ui": self.ui.__dict__,
            "remote_access": {
                "provider": self.remote_access.provider,
                "vibe_cloud": self.remote_access.vibe_cloud.__dict__,
            },
            "audio_asr": self.audio_asr.__dict__,
            "update": self.update.__dict__,
            "ack_mode": self.ack_mode,
            "show_duration": self.show_duration,
            "include_time_info": self.include_time_info,
            "include_user_info": self.include_user_info,
            "reply_enhancements": self.reply_enhancements,
            "show_pages_prompt": self.show_pages_prompt,
            "language": self.language,
            "agent_progress_style": self.agent_progress_style,
            "agent_status_heartbeat_ms": self.agent_status_heartbeat_ms,
            "agent_status_no_output_ms": self.agent_status_no_output_ms,
            "setup_completed": self.setup_completed,
        }
        _write_config_payload(path, payload)

    def enabled_platforms(self) -> list[str]:
        return list(self.platforms.enabled)

    def platform_has_credentials(self, platform: str) -> bool:
        return get_platform_descriptor(platform).has_credentials(self)

    def configured_platforms(self) -> list[str]:
        return [platform for platform in self.enabled_platforms() if self.platform_has_credentials(platform)]

    def missing_platform_credentials(self) -> list[str]:
        return [platform for platform in self.enabled_platforms() if not self.platform_has_credentials(platform)]

    def has_configured_platform_credentials(self) -> bool:
        return bool(self.configured_platforms())

    def platform_catalog(self) -> list[dict]:
        return platform_catalog_payload()

    def setup_state(self) -> dict:
        configured = self.configured_platforms()
        missing = self.missing_platform_credentials()
        return {
            "needs_setup": not self.setup_completed,
            "configured_platforms": configured,
            "missing_credentials": missing,
        }


class MemoryConfigStaleWrite(RuntimeError):
    """The durable Memory unit no longer matches a writer's snapshot."""


def atomic_update_memory(
    mutator: Callable[[MemoryConfig], MemoryConfig],
    *,
    config_path: Optional[Path] = None,
) -> V2Config:
    """Atomically replace only Memory while preserving every other config field."""

    paths.ensure_data_dirs()
    path = config_path or paths.get_config_path()
    with config_file_lock(path):
        config = V2Config.load(path)
        memory = mutator(deepcopy(config.memory))
        if not isinstance(memory, MemoryConfig):
            raise TypeError("Memory config mutator must return MemoryConfig")
        memory.validate()
        config.memory = memory
        config._write_locked(path, memory=memory)
        return config
