from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import math
import os
import re
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Final, Iterable, Literal, Mapping, Sequence

from config import paths
from config.atomic_io import write_atomic
from core.agent_core.messages import PROTOCOLS
from core.command_runner import run_supervised_command
from vibe.claude_model_catalog import (
    DEFAULT_CLAUDE_MODEL_ALIASES,
    get_catalog_path as get_claude_catalog_path,
    load_catalog_models,
)
from vibe.codex_config import get_codex_home
from vibe.codex_launch_failure import CodexLaunchError, launch_failure_from_stderr

if TYPE_CHECKING:
    from storage.lock import MigrationFileLock


logger = logging.getLogger(__name__)


REMOTE_CATALOG_URL_ENV = "AVIBE_BACKEND_MODEL_CATALOG_URL"
DEFAULT_REMOTE_CATALOG_URL = (
    "https://raw.githubusercontent.com/avibe-bot/avibe/master/vibe/data/backend_models.json"
)
REMOTE_CATALOG_REVALIDATE_SECONDS = 5 * 60
REMOTE_CATALOG_FAILURE_TTL_SECONDS = 10 * 60
REMOTE_CATALOG_TIMEOUT_SECONDS = 3.0
REMOTE_CATALOG_USER_AGENT = "avibe/backend-model-catalog"
REMOTE_CATALOG_CACHE_VERSION = 2
CODEX_HUB_CATALOG_TIMEOUT_SECONDS = 15.0
CODEX_HUB_CATALOG_MAX_BYTES = 8 * 1024 * 1024
CODEX_HUB_CATALOG_HISTORY_LIMIT = 1
_CODEX_HUB_CATALOG_NAME = re.compile(r"standard-responses-[0-9a-f]{16}\.json")

_HIDDEN_VISIBILITIES = {"hide", "hidden"}
_VISIBLE_VISIBILITIES = {"visible", "list"}
_SUPPORTED_BACKENDS = {"claude", "codex"}
_SUPPORTED_VISIBILITIES = {*_VISIBLE_VISIBILITIES, *_HIDDEN_VISIBILITIES}
_DEFAULT_REASONING_EFFORTS = {
    "claude": ["low", "medium", "high"],
    "codex": ["minimal", "low", "medium", "high", "xhigh"],
}
REASONING_EFFORT_VOCABULARY: Final[tuple[str, ...]] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
PROTOCOL_REASONING_EFFORT_DEFAULTS: Final[dict[str, tuple[str, ...]]] = {
    "openai_responses": ("minimal", "low", "medium", "high", "xhigh"),
    "openai_chat": ("minimal", "low", "medium", "high", "xhigh"),
    "anthropic": ("low", "medium", "high", "xhigh", "max"),
}
_CODEX_BUILT_IN_MODELS = [
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
]
# Retired Codex ids that only ever came from the legacy list or a Codex CLI
# cache, never the bundled menu. They are blocked here rather than tombstoned in
# the bundled catalog, which would add them to the persisted fixed menu.
_RETIRED_CODEX_MODELS = frozenset(
    {
        "gpt-5.3-codex",
        "gpt-5.2-codex",
        "gpt-5.4-nano",
        "gpt-5.1-codex-max",
        "gpt-5.1-codex-mini",
        "gpt-5.1",
        "gpt-5",
    }
)
_REASONING_LABELS = {
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra High",
    "max": "Max",
    "ultra": "Ultra",
}

# A custom model needs Codex's agent/runtime shape, but it must not inherit
# provider metadata from whichever native model happens to be first.
_CODEX_CUSTOM_SCAFFOLD_KEYS = (
    "shell_type",
    "model_messages",
    "base_instructions",
    "include_skills_usage_instructions",
    "include_plugin_usage_instructions",
    "include_apps_usage_instructions",
    "apply_patch_tool_type",
    "truncation_policy",
    "effective_context_window_percent",
    "node_repl_auto_review_required",
    "node_repl_disabled",
    "use_responses_lite",
    "multi_agent_version",
    "tool_mode",
    "prefer_websockets",
)
_CODEX_CUSTOM_REQUIRED_DEFAULTS: dict[str, Any] = {
    "support_verbosity": False,
    "experimental_supported_tools": [],
    "supports_parallel_tool_calls": False,
}

_REMOTE_LOCK = threading.Lock()
_REMOTE_REFRESH_IN_FLIGHT = False
_REMOTE_MEMORY_CACHE: dict[str, dict[str, Any]] = {}
_REMOTE_REFRESH_COMPLETED: Callable[[], None] | None = None


def get_bundled_catalog_path(repo_root: Path | None = None) -> Path:
    base_dir = repo_root if repo_root is not None else Path(__file__).resolve().parent
    return base_dir / "data" / "backend_models.json"


def get_cached_catalog_path() -> Path:
    return paths.get_state_dir() / "backend_model_catalog.json"


def _codex_hub_catalog_path(catalog: bytes) -> Path:
    digest = hashlib.sha256(catalog).hexdigest()[:16]
    return paths.get_runtime_dir() / "model-hub" / "codex" / f"standard-responses-{digest}.json"


class CodexHubCatalog:
    """One owner's pin on an immutable catalog generation.

    Caches, pending launches and transports acquire independent pins and close
    them at their lifecycle boundary. Finalization is only an abandoned-owner
    fallback: exception tracebacks must not delay ordinary release.
    """

    def __init__(self, path: Path, descriptor: int) -> None:
        self.path = path
        self.descriptor = descriptor
        self._release = weakref.finalize(self, _release_codex_hub_catalog, path, descriptor)

    def retain(self) -> CodexHubCatalog:
        """Acquire an independent pin before giving up this owner's pin."""
        if not self._release.alive:
            raise RuntimeError("Cannot retain a released Codex catalog")
        with _codex_hub_catalog_lock(self.path.parent):
            return _pin_codex_hub_catalog(self.path)

    def close(self) -> None:
        self._release()

    def __enter__(self) -> CodexHubCatalog:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def inherited_subprocess_kwargs(self):
        """Keep the file pinned in the child even if its Avibe parent exits."""
        if not self._release.alive:
            raise RuntimeError("Cannot launch with a released Codex catalog")
        if os.name != "nt":
            yield {"pass_fds": (self.descriptor,)}
            return
        import msvcrt

        # Each spawn gets its own temporary inheritable handle, so simultaneous
        # launches cannot reset one another's inheritance flags.
        descriptor = os.dup(self.descriptor)
        try:
            os.set_inheritable(descriptor, True)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.lpAttributeList = {"handle_list": [msvcrt.get_osfhandle(descriptor)]}
            yield {"startupinfo": startupinfo, "close_fds": True}
        finally:
            os.close(descriptor)


def _codex_hub_catalog_lock(directory: Path, *, timeout_seconds: float = 30.0) -> MigrationFileLock:
    # Lightweight catalog/handler imports must not initialize SQLite storage.
    from storage.lock import MigrationFileLock

    return MigrationFileLock(directory / ".catalog.lock", timeout_seconds=timeout_seconds)


def _open_codex_hub_catalog(path: Path) -> int:
    """Open only an ordinary file without following a candidate symlink."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISREG(named.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError(f"Codex catalog is not an exclusive regular file: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _pin_codex_hub_catalog(path: Path) -> CodexHubCatalog:
    descriptor = _open_codex_hub_catalog(path)
    try:
        if os.name != "nt":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return CodexHubCatalog(path, descriptor)
    except BaseException:
        os.close(descriptor)
        raise


def _prune_codex_hub_catalogs_locked(directory: Path) -> None:
    """Keep every pinned generation and at most one unpinned history file.

    Caller holds the publication lock. Name order only chooses the spare;
    neither timestamps nor age establish that a generation is unused.
    """
    spare = 0
    for path in sorted(directory.iterdir()):
        if not _CODEX_HUB_CATALOG_NAME.fullmatch(path.name):
            continue
        descriptor = None
        try:
            if not stat.S_ISREG(path.lstat().st_mode) or path.lstat().st_nlink != 1:
                continue
            descriptor = _open_codex_hub_catalog(path)
            if os.name != "nt":
                import fcntl

                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
            # On Windows the deletion itself is the non-blocking pin probe:
            # any reader's open handle denies deletion. Close our probe first,
            # while the publication lock still excludes a new reader.
            if os.name == "nt":
                os.close(descriptor)
                descriptor = None
            if spare < CODEX_HUB_CATALOG_HISTORY_LIMIT:
                spare += 1
                continue
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 32:
                # ERROR_SHARING_VIOLATION: a cache/child still owns its pin.
                continue
            logger.warning("Could not reclaim Codex Model Hub catalog %s", path, exc_info=True)
        finally:
            if descriptor is not None:
                os.close(descriptor)


def prune_codex_hub_catalogs(directory: Path) -> None:
    """Best-effort reclamation at publication and reference-release boundaries."""
    from storage.lock import MigrationLockTimeout

    try:
        if not directory.is_dir():
            return
        with _codex_hub_catalog_lock(directory, timeout_seconds=0):
            _prune_codex_hub_catalogs_locked(directory)
    except MigrationLockTimeout:
        # A publisher/cleaner already owns this directory. Its sweep, or the
        # next lifecycle boundary, retries reclamation.
        logger.debug("Codex catalog cleanup deferred while publication is in progress")
    except OSError:
        logger.warning("Could not clean Codex Model Hub catalogs in %s", directory, exc_info=True)


def _release_codex_hub_catalog(path: Path, descriptor: int) -> None:
    # Close, do not LOCK_UN: a spawned app-server inherits the same open file
    # description and must keep its lock even if the Avibe parent goes away.
    os.close(descriptor)
    prune_codex_hub_catalogs(path.parent)


def _codex_hub_catalog_bytes(
    raw_catalog: bytes,
    configured_models: Sequence[Mapping[str, Any]] | None = None,
) -> bytes:
    """Project a complete Codex catalog onto generic Responses semantics."""

    try:
        payload = json.loads(raw_catalog)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Codex returned an invalid bundled model catalog") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ValueError("Codex bundled model catalog has no models list")
    models = payload["models"]
    if not models:
        raise ValueError("Codex bundled model catalog is empty")

    provider_private_defaults: dict[str, object] = {
        "use_responses_lite": False,
        "multi_agent_version": None,
        "tool_mode": None,
        "prefer_websockets": False,
    }
    native_rows: list[dict[str, Any]] = []
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("slug"), str):
            raise ValueError("Codex bundled model catalog contains an invalid model")
        row = dict(model)
        for key, value in provider_private_defaults.items():
            if key in row:
                row[key] = value
        native_rows.append(row)
    if configured_models is None:
        projected = native_rows
    else:
        native_by_slug = {row["slug"]: row for row in native_rows}
        template = native_rows[0]
        projected = []
        for priority, configured in enumerate(configured_models, start=1):
            model_id = configured.get("id")
            if not isinstance(model_id, str) or not model_id:
                raise ValueError("Configured Codex catalog contains an invalid model")
            native_row = native_by_slug.get(model_id)
            if native_row is not None:
                row = dict(native_row)
            else:
                row = {
                    key: template[key]
                    for key in _CODEX_CUSTOM_SCAFFOLD_KEYS
                    if key in template
                }
                # Required in some Codex catalog versions, with no matching
                # user-authored BackendModel field. Codex 0.146.0 also requires
                # supports_parallel_tool_calls.
                row.update(_CODEX_CUSTOM_REQUIRED_DEFAULTS)
            row["slug"] = model_id
            display_name = configured.get("display_name")
            row["display_name"] = (
                display_name
                if isinstance(display_name, str) and display_name
                else model_id
            )
            row["priority"] = priority
            row["visibility"] = "list"
            row["supported_in_api"] = True
            context_window = configured.get("context_window")
            if (
                isinstance(context_window, int)
                and not isinstance(context_window, bool)
                and context_window > 0
            ):
                row["context_window"] = context_window
                # This saved planning value is not an independently known hard
                # ceiling. Codex clamps explicit model_context_window overrides
                # to max_context_window, so duplicating it here silently defeats
                # a user's larger native setting.
                row.pop("max_context_window", None)
                # Codex derives this from the active window when omitted; a
                # bundled value belongs to the native window we just replaced.
                row.pop("auto_compact_token_limit", None)
            input_modalities = configured.get("input_modalities")
            # Empty lists in released rows mean "not overridden". Codex's
            # omitted-modality default accepts text and images; narrowing that
            # to text would discard supplied images merely for lack of metadata.
            # Original-resolution detail is a separate provider capability.
            if native_row is None:
                row["supports_image_detail_original"] = False
            if isinstance(input_modalities, list) and input_modalities:
                supported_modalities = [
                    modality
                    for modality in input_modalities
                    if modality in {"text", "image"}
                ]
                row["input_modalities"] = supported_modalities or ["text"]
                row["supports_image_detail_original"] = "image" in supported_modalities
            efforts = configured.get("reasoning_efforts")
            supports_reasoning = configured.get("supports_reasoning")
            if supports_reasoning is False:
                settled_efforts = ["none"]
            elif isinstance(efforts, list) and efforts:
                settled_efforts = [
                    effort
                    for effort in efforts
                    if isinstance(effort, str) and effort
                ]
            elif native_row is None:
                # The list is required by Codex's schema, but an unknown model
                # has neither an advertised ladder nor a default effort. Codex
                # can still carry the effort explicitly supplied with a turn.
                settled_efforts = []
            else:
                settled_efforts = None
            if settled_efforts is not None:
                native_default = row.get("default_reasoning_level")
                if not settled_efforts:
                    row.pop("default_reasoning_level", None)
                elif not (
                    isinstance(native_default, str)
                    and native_default in settled_efforts
                ):
                    native_default = (
                        "medium"
                        if "medium" in settled_efforts
                        else settled_efforts[0]
                    )
                if settled_efforts:
                    row["default_reasoning_level"] = native_default
                row["supported_reasoning_levels"] = [
                    {
                        "effort": effort,
                        "description": (
                            _REASONING_LABELS.get(effort)
                            or effort.replace("_", " ").title()
                        ),
                    }
                    for effort in settled_efforts
                ]
            for key, value in provider_private_defaults.items():
                if key in row:
                    row[key] = value
            projected.append(row)
    payload["models"] = projected
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()


def _publish_codex_hub_catalog(
    raw_catalog: bytes,
    configured_models: Sequence[Mapping[str, Any]] | None = None,
) -> CodexHubCatalog:
    catalog = _codex_hub_catalog_bytes(raw_catalog, configured_models)
    path = _codex_hub_catalog_path(catalog)
    with _codex_hub_catalog_lock(path.parent):
        try:
            path.lstat()
        except FileNotFoundError:
            write_atomic(path, catalog)
        pinned = _pin_codex_hub_catalog(path)
        try:
            if os.read(pinned.descriptor, len(catalog) + 1) != catalog:
                raise ValueError("Codex catalog digest path contains different bytes")
        except BaseException:
            pinned.close()
            raise
        try:
            _prune_codex_hub_catalogs_locked(path.parent)
        except OSError:
            logger.warning("Could not clean Codex Model Hub catalogs in %s", path.parent, exc_info=True)
        return pinned


def _export_codex_bundled_catalog(
    binary: str,
    base_env: dict[str, str] | None = None,
) -> bytes:
    from vibe.upgrade import get_safe_cwd

    env = dict(base_env or {})
    removed_env = (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "CODEX_API_KEY",
        "AVIBE_MODEL_HUB_TOKEN",
    )
    result = asyncio.run(
        run_supervised_command(
            command=[
                binary,
                "debug",
                "models",
                "--bundled",
                "-c",
                "model_catalog_json=null",
            ],
            cwd=get_safe_cwd(),
            timeout_seconds=CODEX_HUB_CATALOG_TIMEOUT_SECONDS,
            label="Codex model catalog export",
            max_output_bytes=CODEX_HUB_CATALOG_MAX_BYTES,
            extra_env=env,
            remove_env=removed_env,
        )
    )
    if result.timed_out:
        raise RuntimeError("Codex bundled model catalog timed out")
    if result.stdout_truncated:
        raise RuntimeError("Codex bundled model catalog exceeded the safety limit")
    if result.exit_code != 0:
        # Stderr is read only to recognize a CLI that cannot start; it never
        # reaches an error message, a log, or the user.
        cause = launch_failure_from_stderr(result.stderr)
        if cause is not None:
            raise CodexLaunchError(cause)
        raise RuntimeError("Codex could not export its bundled model catalog")
    return result.stdout.encode()


def prepare_codex_hub_catalog(
    binary: str,
    base_env: dict[str, str] | None = None,
    configured_models: Sequence[Mapping[str, Any]] | None = None,
) -> CodexHubCatalog:
    """Prepare the exact binary's catalog immediately before a Hub launch."""

    return _publish_codex_hub_catalog(
        _export_codex_bundled_catalog(binary, base_env),
        configured_models,
    )


#: Spellings that name the same model: a snapshot date or version tag, and a context-window tag.
_MODEL_SNAPSHOT_SUFFIX: Final = re.compile(r"(?:-\d{8}|-\d{4}-\d{2}-\d{2}|@[^/]*)$")
_MODEL_CONTEXT_TAG: Final = re.compile(r"\[[^\]]*\]$")


def catalog_model_id(model_id: str) -> str:
    """Fold the spellings agents send onto the one catalogs list.

    ``anthropic/claude-opus-5[1m]`` and ``claude-opus-4-5-20251101`` become ``claude-opus-5`` and ``claude-opus-4-5``:
    a provider prefix, the context-window tag and the snapshot date name the same model, its levels and its price.
    """

    text = model_id.strip().lower()
    text = text.rsplit("/", 1)[-1]
    text = _MODEL_CONTEXT_TAG.sub("", text)
    text = _MODEL_SNAPSHOT_SUFFIX.sub("", text)
    return text.strip()


def catalog_row_value(index: Mapping[str, tuple[str, ...]], model_id: str) -> tuple[str, ...] | None:
    """A per-model catalog list for ``model_id``: its own row's, else the row's its spelling folds to."""

    value = index.get(model_id)
    return value if value is not None else index.get(catalog_model_id(model_id))


def load_bundled_catalog(path: Path | None = None) -> dict[str, Any]:
    return _read_catalog(path or get_bundled_catalog_path()) or {}


def bundled_catalog_reasoning_efforts_by_model() -> Mapping[str, tuple[str, ...]]:
    """Index bundled reasoning-effort rows without changing their declarations."""

    return _bundled_catalog_lists_by_model("reasoning_efforts")


def bundled_catalog_client_presets_by_model() -> Mapping[str, tuple[str, ...]]:
    """Index bundled client presets: efforts a client maps before sending.

    A preset such as Codex ``ultra`` stays in its row's ``reasoning_efforts``
    because the client's own menu offers it, but it is never a wire tier.
    """

    return _bundled_catalog_lists_by_model("client_presets")


def _bundled_catalog_lists_by_model(
    key: str, catalog: Mapping[str, Any] | None = None
) -> Mapping[str, tuple[str, ...]]:
    values_by_model: dict[str, tuple[str, ...]] = {}
    backends = (load_bundled_catalog() if catalog is None else catalog).get("backends")
    if not isinstance(backends, dict):
        return MappingProxyType(values_by_model)
    for backend in backends.values():
        models = backend.get("models") if isinstance(backend, dict) else None
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            values = model.get(key)
            if (
                isinstance(model_id, str)
                and model_id not in values_by_model
                and isinstance(values, list)
            ):
                values_by_model[model_id] = tuple(values)
    return MappingProxyType(values_by_model)


def bundled_catalog_reasoning_efforts_for_model(
    model_id: str,
) -> tuple[str, ...] | None:
    """Return one bundled catalog row's exact declared effort list."""

    return bundled_catalog_reasoning_efforts_by_model().get(model_id)


@functools.lru_cache(maxsize=4)
def _bundled_client_presets(path: str) -> Mapping[str, tuple[str, ...]]:
    catalog = _read_catalog(Path(path))
    # An unreadable bundle has no presets, like a model without a row.
    return _bundled_catalog_lists_by_model("client_presets", catalog or {})


def bundled_client_presets_once() -> Mapping[str, tuple[str, ...]]:
    """The bundled client-preset index, read once per process.

    The bundled file never changes while a process runs, and config loading and
    cloning (on the controller loop) consult this index, so it must cost no I/O
    after the first read. Keyed by path, so a different bundle is its own entry.
    """

    return _bundled_client_presets(str(get_bundled_catalog_path()))


def without_client_presets(model_ids: Sequence[str], efforts: Sequence[str]) -> list[str]:
    """Efforts as a wire client may send them: without any listed model's client presets.

    Vibey is not the client a preset belongs to, so a preset is never one of its
    tiers (#2539).
    """

    presets = bundled_client_presets_once()
    dropped = {preset for model_id in model_ids for preset in catalog_row_value(presets, model_id) or ()}
    return [effort for effort in efforts if effort not in dropped]


def bundled_catalog_wire_efforts_by_model() -> Mapping[str, tuple[str, ...]]:
    """Index bundled rows' efforts as a Source's wire tiers: each row's list minus its ``client_presets``.

    One catalog read builds both, so a discovery batch still loads the bundled
    file once (#2539).
    """

    catalog = load_bundled_catalog()
    efforts = _bundled_catalog_lists_by_model("reasoning_efforts", catalog)
    presets = _bundled_catalog_lists_by_model("client_presets", catalog)
    return MappingProxyType(
        {
            model_id: tuple(effort for effort in values if effort not in presets.get(model_id, ()))
            for model_id, values in efforts.items()
        }
    )


# The ``providers`` section (model-hub-capability-model.md §4,
# docs/plans/model-hub-contracts/provider-overlay.schema.json): what a provider
# offers per model family and protocol. Priors only; whether a Source carries a
# feature is evidence. Read from the bundled file alone, never the remote copy:
# its declarations go on the wire and must be the ones this build shipped with.
_HOSTED_TOOL_PRICE_UNITS = ("call", "search", "post", "profile")
# An image model is priced per image, and only an image model is.
_IMAGE_PRICE_UNITS = ("image",)
# The tools a chat request may declare natively. Image generation is never one (vibey-hosted-tools.md §7).
OVERLAY_SEARCH_TOOLS: tuple[str, ...] = ("web_search", "x_search")
# The credential kinds an image model's route may be offered on; absent means every kind.
IMAGE_MODEL_KINDS: tuple[str, ...] = ("api_key", "subscription")
# What a call names itself; an image model's fixed request fields may not set them.
IMAGE_CALL_FIELDS: frozenset[str] = frozenset({"model", "prompt", "size", "aspect_ratio", "n", "images", "stream"})
# The protocols whose engine entries serve the images endpoints (vibey-hosted-tools.md §7.4): an image model is
# offered, registered and called on a Source of one of them only. A Google Source registers its image models on an
# internal OpenAI-compatible entry of the same key (vibey-gemini-native.md §3).
IMAGE_PROTOCOLS: frozenset[str] = frozenset({"openai_responses", "openai_chat", "google"})
IMAGE_MODEL_PARAMS: tuple[str, ...] = ("images", "n", "size")
IMAGE_SIZE_FORMS: tuple[str, ...] = ("pixels", "ratio")
# One line in every request's tool block: bounded so a catalog line cannot grow every prompt prefix.
IMAGE_MODEL_DESCRIPTION_MAX_CHARS = 80
_OVERLAY_TOKEN = re.compile(r"\S(?:.*\S)?")
_OVERLAY_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_OVERLAY_URL = re.compile(r"https://[^\s/?#]+(?:[/?#]\S*)?")


@dataclass(frozen=True)
class HostedToolPriceComponent:
    unit: str
    usd_per_1k: float


@dataclass(frozen=True)
class HostedToolPrice:
    """A provider's list price for one hosted tool, with the page it was read from."""

    unit: str
    usd_per_1k: float
    as_of: date
    url: str
    extra: tuple[HostedToolPriceComponent, ...] = ()


@dataclass(frozen=True)
class HostedToolSpec:
    """One hosted tool as a provider offers it on one protocol.

    ``price`` is None when no list price is known, which is never a price of
    zero. ``include`` lists the Responses ``include`` values the tool needs.
    """

    declaration_json: str
    price: HostedToolPrice | None
    include: tuple[str, ...]

    @property
    def declaration(self) -> dict[str, Any]:
        """A fresh copy of the wire declaration, safe to place in a request body."""

        return json.loads(self.declaration_json)

    @property
    def declaration_type(self) -> str:
        """The declaration's tool ``type``, as the provider names it on the wire."""

        return self.declaration["type"]


@dataclass(frozen=True)
class ImageModelPrice:
    """A per-image list price, for the output ``size`` and ``quality`` it applies to (None matches any)."""

    price: HostedToolPrice
    size: str | None = None
    quality: str | None = None


@dataclass(frozen=True)
class ImageModelSpec:
    """One image model a vendor offers to ``image_generate`` (capability model §4).

    Every one runs through the engine's images endpoints (``vibey-hosted-tools.md`` §7.4).
    ``params`` and ``size_forms`` are sorted, so anything built from them is stable.
    ``kinds`` are the credential kinds its route was verified on. ``request`` holds the
    fixed fields every call to it sends (an output format, base64 results).
    ``tokens_per_image`` lets the pricing module estimate a model with no per-image price.
    """

    id: str
    description: str
    params: tuple[str, ...]
    size_forms: tuple[str, ...]
    kinds: tuple[str, ...] = IMAGE_MODEL_KINDS
    request_json: str = "{}"
    prices: tuple[ImageModelPrice, ...] = ()
    tokens_per_image: int | None = None

    @property
    def request(self) -> dict[str, Any]:
        return json.loads(self.request_json)


@dataclass(frozen=True)
class ProviderModelOverlay:
    """One model family of a provider; ``wire_efforts`` None defers to models.dev."""

    match_kind: Literal["prefix", "id"]
    match_value: str
    wire_efforts: tuple[str, ...] | None
    hosted_tools: Mapping[str, Mapping[str, HostedToolSpec]]


@dataclass(frozen=True)
class ProviderVendorOverlay:
    """One vendor of the ``providers`` section: its model families, executor models and image models."""

    models: tuple[ProviderModelOverlay, ...]
    executor_models: tuple[str, ...] = ()
    image_models: tuple[ImageModelSpec, ...] = ()


def bundled_provider_overlay() -> Mapping[str, tuple[ProviderModelOverlay, ...]]:
    """Each vendor's model families from the bundled ``providers`` section."""

    return MappingProxyType({vendor: entry.models for vendor, entry in bundled_provider_vendors().items()})


def provider_executor_models(vendor: str) -> tuple[str, ...]:
    """The vendor's lightweight models that run proxied search calls, preferred first (vibey-hosted-tools.md §6).

    A Source runs the first of them it lists that runs the tool; each names its dated snapshots too.
    """

    entry = bundled_provider_vendors().get(vendor.strip().lower())
    return entry.executor_models if entry is not None else ()


def provider_image_models(vendor: str) -> tuple[ImageModelSpec, ...]:
    """The image models the vendor offers to ``image_generate``, in catalog order."""

    entry = bundled_provider_vendors().get(vendor.strip().lower())
    return entry.image_models if entry is not None else ()


@functools.lru_cache(maxsize=1)
def bundled_provider_vendors() -> Mapping[str, ProviderVendorOverlay]:
    """Each vendor's entry from the bundled ``providers`` section.

    An invalid vendor entry is left out with a warning, and a ``providers``
    value that is not an object disables only the overlay; backend rows never
    depend on this section.
    """

    try:
        payload = json.loads(get_bundled_catalog_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return MappingProxyType({})
    if not isinstance(payload, dict) or "providers" not in payload:
        return MappingProxyType({})
    providers = payload["providers"]
    if not isinstance(providers, dict):
        logger.warning("Backend model catalog providers section is not an object; the provider overlay is disabled")
        return MappingProxyType({})
    overlay: dict[str, ProviderVendorOverlay] = {}
    for vendor, raw_vendor in providers.items():
        try:
            overlay[_overlay_vendor_id(vendor)] = _overlay_vendor(raw_vendor)
        except ValueError as exc:
            logger.warning("Provider overlay entry %r is disabled: %s", vendor, exc)
    return MappingProxyType(overlay)


def provider_overlay_entry(vendor: str, model_id: str) -> ProviderModelOverlay | None:
    """Return the first model family of ``vendor`` that matches ``model_id``.

    Google names a model inside its ``models/`` collection (``models/gemini-4-argon``), the name a Gemini Source keeps;
    a family matches the model's own id.
    """

    from core.handlers.model_hub.identifiers import normalized_model_id

    model = normalized_model_id(model_id).removeprefix("models/")
    vendor_entry = bundled_provider_vendors().get(vendor.strip().lower())
    for entry in vendor_entry.models if vendor_entry is not None else ():
        if (entry.match_kind == "id" and model == entry.match_value) or (
            entry.match_kind == "prefix" and model.startswith(entry.match_value)
        ):
            return entry
    return None


def hosted_tool_spec(vendor: str, protocol: str, model_id: str, tool: str) -> HostedToolSpec | None:
    """Return the matching family's declaration, price and include for one tool, on a Source of ``vendor``."""

    prior = prior_vendor(vendor, protocol, model_id)
    entry = provider_overlay_entry(prior, model_id) if prior is not None else None
    if entry is None:
        return None
    return entry.hosted_tools.get(protocol, {}).get(tool)


# The vendor of a Source added as a compatible endpoint (自定义, model-hub-vendor-preset-protocol.md): it names no
# vendor, so the overlay has no entry for it.
CUSTOM_VENDOR: Final = "custom"


# Protocols besides its catalog one that a vendor's routes were verified on: Gemini's image route runs on Google's
# OpenAI-compatible surface, which a Chat relay serving Gemini also speaks (vibey-gemini-native.md §3).
_ALSO_VERIFIED_ON: Final[Mapping[str, tuple[str, ...]]] = {"gemini": ("openai_chat",)}


def prior_vendors(vendor: str, protocol: str) -> tuple[str, ...]:
    """The overlay vendors whose priors a Source of ``vendor`` and ``protocol`` may take (vibey-hosted-tools.md §3.1).

    A catalog vendor takes its own. A ``custom`` Source names none, so it may take those of every overlay vendor
    whose catalog protocol it speaks, in overlay order: a route is offered only on the protocol its vendor's was
    verified on, since the engine refuses the rest locally and a local refusal is never learned.
    """

    vendor = vendor.strip().lower()
    if vendor != CUSTOM_VENDOR:
        return (vendor,)
    from vibe.model_hub_runtime.api_key_vendors import pinned_api_key_protocol

    return tuple(
        name for name in bundled_provider_vendors()
        if pinned_api_key_protocol(name) == protocol or protocol in _ALSO_VERIFIED_ON.get(name, ())
    )


def prior_vendor(vendor: str, protocol: str, model_id: str) -> str | None:
    """The vendor whose priors apply to ``model_id`` on a Source of ``vendor`` and ``protocol``; None for none.

    A catalog vendor's own. On a ``custom`` Source, the one overlay vendor that claims the model (a model family of
    its matches it, or it is one of its image models), when the Source may take that vendor's priors at all. A model
    no vendor claims, or several do, takes none: its vendor is not guessed.
    """

    vendor = vendor.strip().lower()
    if vendor != CUSTOM_VENDOR:
        return vendor
    from core.handlers.model_hub.identifiers import normalized_model_id

    model = normalized_model_id(model_id)
    claimers = [
        name
        for name, entry in bundled_provider_vendors().items()
        if provider_overlay_entry(name, model) is not None or any(spec.id == model for spec in entry.image_models)
    ]
    return claimers[0] if len(claimers) == 1 and claimers[0] in prior_vendors(vendor, protocol) else None


def source_executor_models(vendor: str, protocol: str) -> tuple[str, ...]:
    """The executor models a Source of ``vendor`` and ``protocol`` may run proxied searches on, preferred first (§6).

    Its vendor's; on a ``custom`` Source, those of every vendor whose priors it may take, in overlay order.
    """

    return tuple(
        dict.fromkeys(model for name in prior_vendors(vendor, protocol) for model in provider_executor_models(name))
    )


def source_image_models(vendor: str, protocol: str, listed: Iterable[str]) -> tuple[ImageModelSpec, ...]:
    """The image models a Source of ``vendor`` and ``protocol`` may offer, in catalog order (§7.2).

    A catalog vendor's every image model. A ``custom`` Source's are only those it lists (``listed``), each of the
    vendor that claims it: nothing but the endpoint's own inventory says that it serves one.
    """

    if vendor.strip().lower() != CUSTOM_VENDOR:
        return provider_image_models(vendor)
    names = frozenset(listed)
    return tuple(
        spec
        for name in prior_vendors(vendor, protocol)
        for spec in provider_image_models(name)
        if spec.id in names and prior_vendor(vendor, protocol, spec.id) == name
    )


def _overlay_vendor_id(vendor: str) -> str:
    from config.v2_config import normalize_model_hub_vendor_id

    try:
        canonical = normalize_model_hub_vendor_id(vendor)
    except ValueError:
        canonical = None
    if canonical != vendor:
        raise ValueError("the key is not a canonical Source vendor id")
    return vendor


def _overlay_vendor(raw: object) -> ProviderVendorOverlay:
    vendor = _overlay_object(raw, "vendor entry", required=("models",), optional=("executor_models", "image_models"))
    models = vendor["models"]
    if not isinstance(models, list):
        raise ValueError("models must be a list")
    image_models = vendor.get("image_models", [])
    if not isinstance(image_models, list):
        raise ValueError("image_models must be a list")
    executor_models = vendor.get("executor_models", [])
    if not isinstance(executor_models, list) or ("executor_models" in vendor and not executor_models):
        raise ValueError("executor_models must be a non-empty list")
    executor_ids = tuple(_overlay_token(item, "executor_models") for item in executor_models)
    if len(set(executor_ids)) != len(executor_ids):
        raise ValueError("executor_models must not repeat a model")
    return ProviderVendorOverlay(
        models=tuple(_overlay_model(item) for item in models),
        executor_models=executor_ids,
        image_models=tuple(_overlay_image_model(item) for item in image_models),
    )


def _overlay_image_model(raw: object) -> ImageModelSpec:
    model = _overlay_object(
        raw,
        "image model",
        required=("id", "description", "params", "size_forms"),
        optional=("kinds", "request", "prices", "tokens_per_image"),
    )
    description = model["description"]
    if not isinstance(description, str) or not _OVERLAY_TOKEN.fullmatch(description) or (
        len(description) > IMAGE_MODEL_DESCRIPTION_MAX_CHARS
    ):
        raise ValueError(f"image model description must be one line of at most {IMAGE_MODEL_DESCRIPTION_MAX_CHARS} characters")
    kinds = _overlay_tokens(model.get("kinds", list(IMAGE_MODEL_KINDS)), "image model kinds")
    if not kinds or set(kinds) - set(IMAGE_MODEL_KINDS):
        raise ValueError("image model kinds name an unknown credential kind")
    request = model.get("request", {})
    if not isinstance(request, dict) or set(request) & IMAGE_CALL_FIELDS or not all(
        isinstance(value, (str, int, float, bool)) for value in request.values()
    ):
        raise ValueError("image model request holds fixed scalar fields a call does not name")
    params = _overlay_tokens(model["params"], "image model params")
    size_forms = _overlay_tokens(model["size_forms"], "image model size_forms")
    if set(params) - set(IMAGE_MODEL_PARAMS) or set(size_forms) - set(IMAGE_SIZE_FORMS):
        raise ValueError("image model params or size_forms name an unknown value")
    if ("size" in params) != bool(size_forms):
        raise ValueError("image model size_forms are present exactly when it takes size")
    prices = model.get("prices", [])
    if not isinstance(prices, list):
        raise ValueError("image model prices must be a list")
    tokens = model.get("tokens_per_image")
    if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0):
        raise ValueError("image model tokens_per_image must be a positive integer")
    return ImageModelSpec(
        id=_overlay_token(model["id"], "image model id"),
        description=description,
        params=tuple(sorted(params)),
        size_forms=tuple(sorted(size_forms)),
        kinds=tuple(sorted(kinds)),
        request_json=json.dumps(request, sort_keys=True),
        prices=tuple(_overlay_image_price(item) for item in prices),
        tokens_per_image=tokens,
    )


def _overlay_image_price(raw: object) -> ImageModelPrice:
    entry = _overlay_object(raw, "image model price", required=("price",), optional=("size", "quality"))
    price = _overlay_price(entry["price"], "image model price", units=_IMAGE_PRICE_UNITS)
    return ImageModelPrice(
        price=price,
        size=None if entry.get("size") is None else _overlay_token(entry["size"], "image model price size"),
        quality=None if entry.get("quality") is None else _overlay_token(entry["quality"], "image model price quality"),
    )


def _overlay_model(raw: object) -> ProviderModelOverlay:
    model = _overlay_object(raw, "model entry", required=("match",), optional=("wire_efforts", "hosted_tools"))
    match = _overlay_object(model["match"], "match", optional=("prefix", "id"))
    if len(match) != 1:
        raise ValueError("match must name exactly one of prefix or id")
    ((match_kind, match_value),) = match.items()
    wire_efforts = model.get("wire_efforts")
    protocols = _overlay_object(model.get("hosted_tools", {}), "hosted_tools", optional=PROTOCOLS)
    hosted_tools: dict[str, Mapping[str, HostedToolSpec]] = {}
    for protocol, raw_tools in protocols.items():
        tools = _overlay_object(raw_tools, f"hosted_tools.{protocol}", optional=OVERLAY_SEARCH_TOOLS)
        hosted_tools[protocol] = MappingProxyType(
            {tool: _overlay_hosted_tool(spec, f"{protocol}.{tool}") for tool, spec in tools.items()}
        )
    return ProviderModelOverlay(
        match_kind=match_kind,
        match_value=_overlay_token(match_value, f"match.{match_kind}"),
        wire_efforts=None if wire_efforts is None else _overlay_tokens(wire_efforts, "wire_efforts"),
        hosted_tools=MappingProxyType(hosted_tools),
    )


def _overlay_hosted_tool(raw: object, what: str) -> HostedToolSpec:
    spec = _overlay_object(raw, what, required=("declaration",), optional=("price", "include"))
    declaration = spec["declaration"]
    if not isinstance(declaration, dict):
        raise ValueError(f"{what}.declaration must be an object")
    _overlay_token(declaration.get("type"), f"{what}.declaration.type")
    return HostedToolSpec(
        declaration_json=json.dumps(declaration, ensure_ascii=False, allow_nan=False),
        price=_overlay_price(spec["price"], f"{what}.price") if "price" in spec else None,
        include=_overlay_tokens(spec.get("include", []), f"{what}.include"),
    )


def _overlay_price(raw: object, what: str, *, units: tuple[str, ...] = _HOSTED_TOOL_PRICE_UNITS) -> HostedToolPrice:
    # Only a search tool's price has further billed units.
    extra_allowed = ("extra",) if units == _HOSTED_TOOL_PRICE_UNITS else ()
    price = _overlay_object(raw, what, required=("unit", "usd_per_1k", "as_of", "url"), optional=extra_allowed)
    as_of = _overlay_date(price["as_of"], f"{what}.as_of")
    url = price["url"]
    if not isinstance(url, str) or not _OVERLAY_URL.fullmatch(url):
        raise ValueError(f"{what}.url must be an https URL")
    extra = price.get("extra", [])
    if not isinstance(extra, list):
        raise ValueError(f"{what}.extra must be a list")
    unit, usd_per_1k = _overlay_price_component(price, what, units)
    return HostedToolPrice(
        unit=unit,
        usd_per_1k=usd_per_1k,
        as_of=as_of,
        url=url,
        extra=tuple(
            HostedToolPriceComponent(
                *_overlay_price_component(
                    _overlay_object(item, f"{what}.extra", required=("unit", "usd_per_1k")),
                    f"{what}.extra",
                    units,
                )
            )
            for item in extra
        ),
    )


def _overlay_date(value: object, what: str) -> date:
    if isinstance(value, str) and _OVERLAY_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"{what} must be a YYYY-MM-DD date")


def _overlay_price_component(price: Mapping[str, Any], what: str, units: tuple[str, ...]) -> tuple[str, float]:
    unit = price["unit"]
    usd_per_1k = price["usd_per_1k"]
    if not isinstance(unit, str) or unit not in units:
        raise ValueError(f"{what}.unit is not a known price unit")
    if (
        not isinstance(usd_per_1k, (int, float))
        or isinstance(usd_per_1k, bool)
        or not math.isfinite(usd_per_1k)
        or usd_per_1k < 0
    ):
        raise ValueError(f"{what}.usd_per_1k must be a number of at least 0")
    return unit, float(usd_per_1k)


def _overlay_object(
    value: object,
    what: str,
    *,
    required: Sequence[str] = (),
    optional: Sequence[str] = (),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be an object")
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError(f"{what} is missing {', '.join(missing)}")
    unknown = sorted(key for key in value if key not in required and key not in optional)
    if unknown:
        raise ValueError(f"{what} has unknown keys {', '.join(unknown)}")
    return value


def _overlay_tokens(value: object, what: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list")
    tokens = tuple(_overlay_token(item, what) for item in value)
    if len(set(tokens)) != len(tokens):
        raise ValueError(f"{what} repeats a value")
    return tokens


def _overlay_token(value: object, what: str) -> str:
    if not isinstance(value, str) or not _OVERLAY_TOKEN.fullmatch(value):
        raise ValueError(f"{what} must be a nonblank string without surrounding whitespace")
    return value


def load_cached_remote_catalog(
    *,
    schedule_refresh: bool = True,
    source_key: str | None = None,
) -> dict[str, Any]:
    current_source_key = source_key or _remote_catalog_source_key(
        _remote_catalog_url()
    )
    cached = _cached_remote_payload(source_key=current_source_key)
    if schedule_refresh and _remote_cache_stale(
        cached,
        source_key=current_source_key,
    ):
        schedule_remote_catalog_refresh()
    catalog = cached.get("catalog")
    return catalog if isinstance(catalog, dict) else {}


def remote_catalog_token(
    *,
    source_key: str | None = None,
) -> tuple[float | None, float | None]:
    payload = _cached_remote_payload(source_key=source_key)
    fetched_at = payload.get("fetched_at")
    failed_at = payload.get("failed_at")
    return (
        float(fetched_at) if isinstance(fetched_at, (int, float)) else None,
        float(failed_at) if isinstance(failed_at, (int, float)) else None,
    )


def remote_catalog_refresh_pending(
    since: tuple[float | None, float | None],
    *,
    source_key: str | None = None,
) -> bool:
    with _REMOTE_LOCK:
        refresh_in_flight = _REMOTE_REFRESH_IN_FLIGHT
    return refresh_in_flight or remote_catalog_token(source_key=source_key) != since


def set_remote_catalog_refresh_completed(
    callback: Callable[[], None] | None,
) -> None:
    """Set the controller-owned completion signal for this process."""

    global _REMOTE_REFRESH_COMPLETED
    with _REMOTE_LOCK:
        _REMOTE_REFRESH_COMPLETED = callback


def schedule_remote_catalog_refresh() -> bool:
    global _REMOTE_REFRESH_IN_FLIGHT

    with _REMOTE_LOCK:
        if _REMOTE_REFRESH_IN_FLIGHT:
            return False
        _REMOTE_REFRESH_IN_FLIGHT = True

    request_url = _remote_catalog_url()
    source_key = _remote_catalog_source_key(request_url)
    thread = threading.Thread(
        target=_refresh_remote_catalog_worker,
        args=(request_url, source_key),
        name="avibe-model-catalog-refresh",
        daemon=True,
    )
    thread.start()
    return True


def refresh_remote_catalog_now(url: str | None = None) -> dict[str, Any]:
    request_url = _remote_catalog_url(url)
    source_key = _remote_catalog_source_key(request_url)
    previous = _cached_remote_payload(source_key=source_key)
    catalog, validators = _fetch_remote_catalog_response(
        url=request_url,
        etag=previous.get("etag"),
        last_modified=previous.get("last_modified"),
    )
    now = time.time()
    not_modified = catalog is None
    if not_modified:
        catalog = previous.get("catalog")
        if not isinstance(catalog, dict):
            catalog, validators = _fetch_remote_catalog_response(url=request_url)
            not_modified = False
    if not isinstance(catalog, dict):
        raise ValueError("Remote backend model catalog returned no content")

    payload = {
        "fetched_at": previous.get("fetched_at", now) if not_modified else now,
        "checked_at": now,
        "catalog": catalog,
        "error": None,
        "source_key": source_key,
    }
    for key in ("etag", "last_modified"):
        value = validators.get(key)
        if not_modified and not value:
            value = previous.get(key)
        if isinstance(value, str) and value:
            payload[key] = value
    _write_cached_remote_payload(payload, source_key=source_key)
    return catalog


def fetch_remote_catalog(url: str | None = None) -> dict[str, Any]:
    catalog, _validators = _fetch_remote_catalog_response(url=url)
    if catalog is None:
        raise ValueError("Remote backend model catalog returned no content")
    return catalog


def _fetch_remote_catalog_response(
    url: str | None = None,
    *,
    etag: object = None,
    last_modified: object = None,
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    request_url = _remote_catalog_url(url)
    headers = {
        "User-Agent": REMOTE_CATALOG_USER_AGENT,
        "Cache-Control": "no-cache",
    }
    if isinstance(etag, str) and etag:
        headers["If-None-Match"] = etag
    if isinstance(last_modified, str) and last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(request_url, headers=headers)
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed public catalog or explicit operator override
            request,
            timeout=REMOTE_CATALOG_TIMEOUT_SECONDS,
        ) as response:
            catalog = _normalize_catalog(json.loads(response.read().decode("utf-8")), strict=True)
            return catalog, _response_validators(response.headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, _response_validators(exc.headers)
        raise


def _remote_catalog_url(url: str | None = None) -> str:
    return (url or os.environ.get(REMOTE_CATALOG_URL_ENV) or DEFAULT_REMOTE_CATALOG_URL).strip()


def _remote_catalog_source_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _response_validators(headers: object) -> dict[str, str]:
    if not hasattr(headers, "get"):
        return {}
    validators = {}
    for header, key in (("ETag", "etag"), ("Last-Modified", "last_modified")):
        value = headers.get(header)
        if isinstance(value, str) and value:
            validators[key] = value
    return validators


def backend_model_entries(backend: str, catalog: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(catalog, dict):
        return []
    backend_key = (backend or "").strip().lower()
    backends = catalog.get("backends")
    raw_backend = backends.get(backend_key) if isinstance(backends, dict) else None
    if not isinstance(raw_backend, dict):
        return []
    raw_models = raw_backend.get("models")
    if not isinstance(raw_models, list):
        return []
    entries = [_normalize_model_entry(item) for item in raw_models]
    return [entry for entry in entries if entry]


def visible_backend_model_entries(
    backend: str, catalog: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Backend rows a picker may offer, with tombstoned rows dropped.

    Callers that merge catalogs keep the raw entries instead, because
    ``merge_model_sources`` needs to see a tombstone to suppress the same id in
    the lower-priority sources behind it.
    """

    return [entry for entry in backend_model_entries(backend, catalog) if not _model_hidden(entry)]


def unlisted_retired_backend_model_ids(backend: str) -> frozenset[str]:
    """Retired ids no bundled catalog row carries, so no tombstone can name them."""

    return _RETIRED_CODEX_MODELS if (backend or "").strip().lower() == "codex" else frozenset()


def retired_backend_model_ids(backend: str, catalog: dict[str, Any] | None) -> frozenset[str]:
    """Ids this catalog tombstones, i.e. models the backend no longer serves."""

    return frozenset(
        entry["id"] for entry in backend_model_entries(backend, catalog) if _model_hidden(entry)
    )


def backend_model_snapshot(backend: str, *, schedule_refresh: bool = True) -> dict[str, Any]:
    backend_key = (backend or "").strip().lower()
    if backend_key not in _SUPPORTED_BACKENDS:
        return {"ok": False, "backend": backend_key, "error": f"unsupported backend '{backend}'"}

    remote_source_key = _remote_catalog_source_key(_remote_catalog_url())
    refresh_token = remote_catalog_token(source_key=remote_source_key)
    remote_catalog = load_cached_remote_catalog(
        schedule_refresh=schedule_refresh,
        source_key=remote_source_key,
    )
    bundled_catalog = load_bundled_catalog()

    if backend_key == "claude":
        sources = _claude_sources(remote_catalog, bundled_catalog)
        blocked = _claude_blocked_model_ids(remote_catalog, bundled_catalog)
    else:
        local_catalog = _read_codex_models_cache()
        remote_entries = backend_model_entries("codex", remote_catalog)
        blocked = _codex_blocked_model_ids(
            remote_catalog,
            local_catalog,
            bundled_catalog,
        )
        sources = _codex_sources(remote_entries, local_catalog, bundled_catalog)

    merged = merge_model_sources(sources, blocked_model_ids=blocked)
    default_efforts = _DEFAULT_REASONING_EFFORTS[backend_key]
    models = [entry["id"] for entry in merged]
    model_labels = {
        entry["id"]: entry["label"]
        for entry in merged
        if isinstance(entry.get("label"), str) and entry["label"] != entry["id"]
    }
    reasoning_options = {"": _reasoning_option_items(default_efforts)}
    for entry in merged:
        efforts = entry.get("reasoning_efforts") or default_efforts
        reasoning_options[entry["id"]] = _reasoning_option_items(efforts)

    cached_payload = _cached_remote_payload(source_key=remote_source_key)
    notes = []
    error = cached_payload.get("error")
    if isinstance(error, str) and error:
        notes.append(f"remote catalog refresh failed: {error}")

    return {
        "ok": True,
        "backend": backend_key,
        "models": models,
        "model_labels": model_labels,
        "reasoning_options": reasoning_options,
        "sources": [name for name, entries in sources if entries],
        "source": " + ".join(name for name, entries in sources if entries),
        "live": False,
        "notes": notes or None,
        "catalog_refresh_pending": remote_catalog_refresh_pending(
            refresh_token,
            source_key=remote_source_key,
        ),
    }


def backend_builtin_models(
    backend: str,
    *,
    schedule_refresh: bool = True,
) -> list[dict[str, Any]]:
    """Return the backend-owned model snapshot used by Model Hub catalogs."""

    return backend_builtin_snapshot(
        backend,
        schedule_refresh=schedule_refresh,
    )["models"]


def backend_builtin_snapshot(
    backend: str,
    *,
    cli_installed: bool = False,
    schedule_refresh: bool = True,
) -> dict[str, Any]:
    """Read every built-in source once and report whether the baseline is complete."""

    backend_key = (backend or "").strip().lower()
    if backend_key == "opencode":
        return _versioned_builtin_snapshot(complete=True, models=[])
    if backend_key not in _SUPPORTED_BACKENDS:
        return _versioned_builtin_snapshot(complete=False, models=[])

    remote_source_key = _remote_catalog_source_key(_remote_catalog_url())
    cached_remote = _read_cached_remote_payload(
        get_cached_catalog_path(),
        source_key=remote_source_key,
    )
    if schedule_refresh and _remote_cache_stale(
        cached_remote,
        source_key=remote_source_key,
    ):
        schedule_remote_catalog_refresh()
    remote_catalog = cached_remote.get("catalog")
    remote_complete = (
        cached_remote.get("source_key") == remote_source_key
        and isinstance(remote_catalog, dict)
    )
    if not remote_complete:
        remote_catalog = {}

    bundled_catalog = _read_complete_catalog(get_bundled_catalog_path())
    bundled_complete = isinstance(bundled_catalog, dict)
    if not bundled_complete:
        bundled_catalog = {}
    if backend_key == "claude":
        claude_catalog_present = get_claude_catalog_path().is_file()
        local_models = load_catalog_models()
        local_complete = not cli_installed or claude_catalog_present
        sources = _claude_sources(
            remote_catalog,
            bundled_catalog,
            local_models=local_models,
        )
        blocked = _claude_blocked_model_ids(remote_catalog, bundled_catalog)
    else:
        local_catalog, local_catalog_read = _read_codex_models_cache_with_status()
        local_complete = not cli_installed or local_catalog_read
        remote_entries = backend_model_entries("codex", remote_catalog)
        blocked = _codex_blocked_model_ids(
            remote_catalog,
            local_catalog,
            bundled_catalog,
        )
        sources = _codex_sources(remote_entries, local_catalog, bundled_catalog)

    merged = merge_model_sources(sources, blocked_model_ids=blocked)
    return _versioned_builtin_snapshot(
        complete=bundled_complete and remote_complete and local_complete,
        models=[
            {
                "id": entry["id"],
                "display_name": builtin_display_name(backend_key, entry["id"], entry.get("label")),
                "reasoning_efforts": list(
                    entry.get("reasoning_efforts")
                    or _DEFAULT_REASONING_EFFORTS[backend_key]
                ),
            }
            for entry in merged
            if not (backend_key == "claude" and entry["id"] == "default")
        ],
    )


def builtin_display_name(backend: str, model_id: str, label: object) -> str | None:
    """The name a built-in row stores in the Model Hub, or None for its id.

    A Claude label is presentation, not a name: `format_claude_model_label`
    appends ` [1M]` for the native picker. Stored as a name it would freeze
    that suffix onto the rows added after a Hub adoption, while the rows the
    adoption imported keep their bare ids.
    """
    if not isinstance(label, str) or label == model_id:
        return None
    if backend == "claude":
        from modules.agents.opencode.utils import format_claude_model_label

        if label == format_claude_model_label(model_id):
            return None
    return label


def _versioned_builtin_snapshot(
    *,
    complete: bool,
    models: list[dict[str, Any]],
) -> dict[str, Any]:
    content = {"complete": complete, "models": models}
    generation = hashlib.sha256(
        json.dumps(
            content,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {**content, "generation": generation}


def catalog_reasoning_efforts_for_model(backend: str, model: str | None) -> list[str] | None:
    if not model:
        return None
    snapshot = backend_model_snapshot(backend, schedule_refresh=False)
    if not snapshot.get("ok"):
        return None
    entries = snapshot.get("reasoning_options", {}).get(model)
    if not isinstance(entries, list):
        return None
    efforts = []
    for entry in entries:
        value = entry.get("value") if isinstance(entry, dict) else None
        if isinstance(value, str) and value and value != "__default__":
            efforts.append(value)
    return efforts or None


def merge_model_sources(
    sources: Sequence[tuple[str, Sequence[dict[str, Any]]]],
    *,
    blocked_model_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    blocked = {model.strip() for model in blocked_model_ids if isinstance(model, str) and model.strip()}
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for source_name, raw_entries in sources:
        for entry in _ordered_entries(raw_entries):
            model = entry["id"]
            if model in blocked:
                continue
            if _model_hidden(entry):
                if model not in merged:
                    blocked.add(model)
                continue

            current = merged.get(model)
            if current is None:
                current = {
                    "id": model,
                    "label": entry.get("label") or model,
                    "reasoning_efforts": list(entry.get("reasoning_efforts") or []),
                    "source": source_name,
                }
                merged[model] = current
                order.append(model)
                continue

            if current.get("label") == model and entry.get("label"):
                current["label"] = entry["label"]
            if not current.get("reasoning_efforts") and entry.get("reasoning_efforts"):
                current["reasoning_efforts"] = list(entry["reasoning_efforts"])

    return [merged[model] for model in order]


def _claude_sources(
    remote_catalog: dict[str, Any],
    bundled_catalog: dict[str, Any],
    *,
    local_models: Sequence[str] | None = None,
) -> list[tuple[str, list[dict[str, Any]]]]:
    from modules.agents.opencode.utils import format_claude_model_label

    legacy_entries = []
    for model in [
        *(local_models if local_models is not None else load_catalog_models()),
        *DEFAULT_CLAUDE_MODEL_ALIASES,
    ]:
        entry = {
            "id": model,
            "reasoning_efforts": _legacy_claude_reasoning_efforts(model),
        }
        label = format_claude_model_label(model)
        if label != model:
            entry["label"] = label
        legacy_entries.append(entry)
    return [
        ("remote", backend_model_entries("claude", remote_catalog)),
        ("bundled", backend_model_entries("claude", bundled_catalog)),
        ("legacy", legacy_entries),
    ]


def _codex_sources(
    remote_entries: list[dict[str, Any]],
    local_catalog: list[dict[str, Any]],
    bundled_catalog: dict[str, Any],
) -> list[tuple[str, list[dict[str, Any]]]]:
    visible_local = [entry for entry in local_catalog if not _model_hidden(entry)]
    local_by_id = {entry["id"]: entry for entry in visible_local}
    bundled_entries = backend_model_entries("codex", bundled_catalog)
    built_in = [
        {"id": model, "reasoning_efforts": _DEFAULT_REASONING_EFFORTS["codex"]}
        for model in _CODEX_BUILT_IN_MODELS
    ]
    return [
        (
            "remote",
            _overlay_local_reasoning_efforts(
                [entry for entry in remote_entries if not _model_hidden(entry)],
                local_by_id,
            ),
        ),
        ("bundled", _overlay_local_reasoning_efforts(bundled_entries, local_by_id)),
        ("local", visible_local),
        ("legacy", built_in),
    ]


def _overlay_local_reasoning_efforts(
    catalog_entries: list[dict[str, Any]],
    local_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    overlaid = []
    for entry in catalog_entries:
        merged = dict(entry)
        local_efforts = local_by_id.get(entry["id"], {}).get("reasoning_efforts")
        if local_efforts:
            merged["reasoning_efforts"] = list(local_efforts)
        overlaid.append(merged)
    return overlaid


def _read_codex_models_cache() -> list[dict[str, Any]]:
    return _read_codex_models_cache_with_status()[0]


def _read_codex_models_cache_with_status() -> tuple[list[dict[str, Any]], bool]:
    cache_path = get_codex_home() / "models_cache.json"
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return [], False
    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        return [], False
    entries = [_normalize_model_entry(item) for item in raw_models]
    return [entry for entry in entries if entry], True


def _legacy_claude_reasoning_efforts(model: str) -> list[str]:
    from modules.agents.opencode.utils import build_claude_reasoning_options

    return [
        entry["value"]
        for entry in build_claude_reasoning_options(model)
        if entry.get("value") != "__default__"
    ]


def _reasoning_option_items(efforts: Iterable[object]) -> list[dict[str, str]]:
    options = [{"value": "__default__", "label": "(Default)"}]
    for effort in _dedupe_str_values(efforts):
        options.append({"value": effort, "label": _REASONING_LABELS.get(effort, effort.capitalize())})
    return options


def _model_hidden(entry: dict[str, Any]) -> bool:
    visibility = entry.get("visibility")
    return isinstance(visibility, str) and visibility.strip().lower() in _HIDDEN_VISIBILITIES


def _model_explicitly_visible(entry: dict[str, Any]) -> bool:
    visibility = entry.get("visibility")
    return isinstance(visibility, str) and visibility.strip().lower() in _VISIBLE_VISIBILITIES


def _bundled_retired_model_ids(
    backend: str,
    remote_catalog: dict[str, Any],
    bundled_catalog: dict[str, Any],
) -> set[str]:
    """Bundled retirements outrank a remote catalog cached before they shipped.

    Only a remote row that is explicitly visible may bring a retired id back;
    an old cached row that merely lists the id does not.
    """

    remote_entries = backend_model_entries(backend, remote_catalog)
    explicit_remote_models = {
        entry["id"] for entry in remote_entries if _model_explicitly_visible(entry)
    }
    return {
        model
        for model in retired_backend_model_ids(backend, bundled_catalog)
        if model not in explicit_remote_models
    }


def _claude_blocked_model_ids(
    remote_catalog: dict[str, Any],
    bundled_catalog: dict[str, Any],
) -> set[str]:
    return _bundled_retired_model_ids("claude", remote_catalog, bundled_catalog)


def _codex_blocked_model_ids(
    remote_catalog: dict[str, Any],
    local_catalog: Sequence[dict[str, Any]],
    bundled_catalog: dict[str, Any],
) -> set[str]:
    remote_entries = backend_model_entries("codex", remote_catalog)
    bundled_entries = backend_model_entries("codex", bundled_catalog)
    explicit_catalog_models = {
        entry["id"]
        for entry in [*remote_entries, *bundled_entries]
        if _model_explicitly_visible(entry)
    }
    retired = _bundled_retired_model_ids("codex", remote_catalog, bundled_catalog) | {
        model for model in _RETIRED_CODEX_MODELS if model not in explicit_catalog_models
    }
    return retired | {
        entry["id"] for entry in remote_entries if _model_hidden(entry)
    } | {
        entry["id"]
        for entry in local_catalog
        if _model_hidden(entry) and entry["id"] not in explicit_catalog_models
    }


def _ordered_entries(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = []
    for index, entry in enumerate(entries):
        normalized = _normalize_model_entry(entry)
        if not normalized:
            continue
        priority = normalized.get("priority")
        indexed.append((priority if isinstance(priority, int) else 10**9, index, normalized))
    return [entry for _, _, entry in sorted(indexed, key=lambda value: (value[0], value[1]))]


def _cached_remote_payload(*, source_key: str | None = None) -> dict[str, Any]:
    current_source_key = source_key or _remote_catalog_source_key(
        _remote_catalog_url()
    )
    with _REMOTE_LOCK:
        cached = _REMOTE_MEMORY_CACHE.get(current_source_key)
        if isinstance(cached, dict):
            return dict(cached)

    payload = _read_cached_remote_payload(
        get_cached_catalog_path(),
        source_key=current_source_key,
    )
    with _REMOTE_LOCK:
        _REMOTE_MEMORY_CACHE[current_source_key] = dict(payload)
    return payload


def _remote_cache_stale(
    payload: dict[str, Any],
    *,
    source_key: str | None = None,
) -> bool:
    current_source_key = source_key or _remote_catalog_source_key(
        _remote_catalog_url()
    )
    if payload.get("source_key") != current_source_key:
        return True
    fetched_at = payload.get("fetched_at")
    checked_at = payload.get("checked_at")
    last_success_at = checked_at if isinstance(checked_at, (int, float)) else fetched_at
    failed_at = payload.get("failed_at")
    if isinstance(failed_at, (int, float)) and (
        not isinstance(last_success_at, (int, float)) or float(failed_at) >= float(last_success_at)
    ):
        return time.time() - float(failed_at) >= REMOTE_CATALOG_FAILURE_TTL_SECONDS
    if not isinstance(last_success_at, (int, float)):
        return True
    return time.time() - float(last_success_at) >= REMOTE_CATALOG_REVALIDATE_SECONDS


def _refresh_remote_catalog_worker(
    request_url: str | None = None,
    source_key: str | None = None,
) -> None:
    global _REMOTE_REFRESH_IN_FLIGHT
    request_url = request_url or _remote_catalog_url()
    source_key = source_key or _remote_catalog_source_key(request_url)
    try:
        refresh_remote_catalog_now(request_url)
    except Exception as exc:
        previous = _cached_remote_payload(source_key=source_key)
        payload = {
            "failed_at": time.time(),
            "catalog": previous.get("catalog"),
            "error": str(exc),
            "source_key": source_key,
        }
        if isinstance(previous.get("fetched_at"), (int, float)):
            payload["fetched_at"] = previous["fetched_at"]
        if isinstance(previous.get("checked_at"), (int, float)):
            payload["checked_at"] = previous["checked_at"]
        for key in ("etag", "last_modified"):
            value = previous.get(key)
            if isinstance(value, str) and value:
                payload[key] = value
        _write_cached_remote_payload(payload, source_key=source_key)
    finally:
        with _REMOTE_LOCK:
            _REMOTE_REFRESH_IN_FLIGHT = False
            completed = _REMOTE_REFRESH_COMPLETED
        if completed is not None:
            try:
                completed()
            except Exception:
                logger.warning(
                    "Backend model catalog refresh completion callback failed",
                    exc_info=True,
                )


def _read_catalog(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return _normalize_catalog(payload)


def _read_complete_catalog(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _normalize_catalog(payload, strict=True)
    except (
        FileNotFoundError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        return None


def _normalize_remote_cache_record(
    payload: object,
    *,
    source_key: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    normalized: dict[str, Any] = {"source_key": source_key}
    catalog_valid = False
    raw_catalog = payload.get("catalog")
    if isinstance(raw_catalog, dict):
        try:
            normalized["catalog"] = _normalize_catalog(raw_catalog, strict=True)
            catalog_valid = True
        except ValueError:
            pass
    for key in ("fetched_at", "checked_at", "failed_at"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and (key not in {"fetched_at", "checked_at"} or catalog_valid):
            normalized[key] = value
    if catalog_valid:
        for key in ("etag", "last_modified"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                normalized[key] = value
    error = payload.get("error")
    if isinstance(error, str) or error is None:
        normalized["error"] = error
    return normalized


def _read_remote_cache_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_cached_remote_payload(
    path: Path,
    *,
    source_key: str | None = None,
) -> dict[str, Any]:
    current_source_key = source_key or _remote_catalog_source_key(
        _remote_catalog_url()
    )
    payload = _read_remote_cache_file(path)
    raw_sources = payload.get("sources")
    if isinstance(raw_sources, dict):
        raw_record = raw_sources.get(current_source_key)
    elif payload.get("source_key") == current_source_key:
        raw_record = payload
    else:
        return {}
    return _normalize_remote_cache_record(
        raw_record,
        source_key=current_source_key,
    )


def _write_cached_remote_payload(
    payload: dict[str, Any],
    *,
    source_key: str | None = None,
) -> None:
    current_source_key = source_key or (
        payload.get("source_key")
        if isinstance(payload.get("source_key"), str) and payload["source_key"]
        else _remote_catalog_source_key(_remote_catalog_url())
    )
    record = _normalize_remote_cache_record(
        payload,
        source_key=current_source_key,
    )
    stored_record = {
        key: value for key, value in record.items() if key != "source_key"
    }
    with _REMOTE_LOCK:
        path = get_cached_catalog_path()
        previous = _read_remote_cache_file(path)
        raw_sources = previous.get("sources")
        if isinstance(raw_sources, dict):
            stored_sources = dict(raw_sources)
        else:
            legacy_source_key = previous.get("source_key")
            stored_sources = {}
            if isinstance(legacy_source_key, str) and legacy_source_key:
                stored_sources[legacy_source_key] = {
                    key: value
                    for key, value in previous.items()
                    if key != "source_key"
                }
        stored_sources[current_source_key] = stored_record
        stored = {
            "cache_version": REMOTE_CATALOG_CACHE_VERSION,
            "sources": stored_sources,
        }
        write_atomic(path, json.dumps(stored, indent=2, sort_keys=True) + "\n")
        _REMOTE_MEMORY_CACHE[current_source_key] = record


def _normalize_catalog(payload: object, *, strict: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        if strict:
            raise ValueError("Backend model catalog must be an object")
        return {}
    schema_version = payload.get("schema_version")
    if strict and schema_version != 1:
        raise ValueError(f"Unsupported backend model catalog schema version: {schema_version!r}")
    backends = payload.get("backends")
    if not isinstance(backends, dict):
        if strict:
            raise ValueError("Backend model catalog must contain a backends object")
        return {}

    normalized_backends: dict[str, Any] = {}
    for backend, raw_backend in backends.items():
        if not isinstance(backend, str) or not isinstance(raw_backend, dict):
            if strict:
                raise ValueError("Backend model catalog contains an invalid backend entry")
            continue
        backend_key = backend.strip().lower()
        if strict and backend_key not in _SUPPORTED_BACKENDS:
            raise ValueError(f"Backend model catalog contains an unsupported backend: {backend_key}")
        models = raw_backend.get("models")
        if not backend_key or not isinstance(models, list):
            if strict:
                raise ValueError(f"Backend model catalog models must be a list: {backend_key}")
            continue
        if strict:
            for item in models:
                _validate_catalog_model_entry(item, backend_key)
        entries = [_normalize_model_entry(item) for item in models]
        if strict and any(not entry for entry in entries):
            raise ValueError(f"Backend model catalog contains an invalid model entry: {backend_key}")
        normalized_backends[backend_key] = {"models": [entry for entry in entries if entry]}
    if strict and not normalized_backends:
        raise ValueError("Backend model catalog must contain at least one backend")
    return {"schema_version": schema_version or 1, "backends": normalized_backends}


def _validate_catalog_model_entry(item: object, backend: str) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"Backend model catalog contains an invalid model entry: {backend}")
    raw_id = item.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ValueError(f"Backend model catalog contains an invalid model entry: {backend}")

    for key in ("label", "visibility"):
        value = item.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"Backend model catalog contains an invalid {key}: {backend}/{raw_id}")
    visibility = item.get("visibility")
    if isinstance(visibility, str) and visibility.strip().lower() not in _SUPPORTED_VISIBILITIES:
        raise ValueError(f"Backend model catalog contains an invalid visibility: {backend}/{raw_id}")

    priority = item.get("priority")
    if priority is not None and (not isinstance(priority, int) or isinstance(priority, bool)):
        raise ValueError(f"Backend model catalog contains an invalid priority: {backend}/{raw_id}")

    efforts = item.get("reasoning_efforts")
    if efforts is not None:
        if not isinstance(efforts, list) or any(not isinstance(value, str) or not value.strip() for value in efforts):
            raise ValueError(f"Backend model catalog contains invalid reasoning efforts: {backend}/{raw_id}")

    presets = item.get("client_presets")
    if presets is not None and (
        not isinstance(presets, list)
        or any(not isinstance(value, str) or not value.strip() for value in presets)
        or not {value.strip() for value in presets} <= {value.strip() for value in efforts or ()}
    ):
        raise ValueError(f"Backend model catalog contains invalid client presets: {backend}/{raw_id}")


def _normalize_model_entry(item: object) -> dict[str, Any]:
    if isinstance(item, str):
        model = item.strip()
        return {"id": model} if model else {}
    if not isinstance(item, dict):
        return {}
    raw_id = item.get("id") or item.get("slug") or item.get("model") or item.get("value")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return {}
    entry: dict[str, Any] = {"id": raw_id.strip()}
    label = item.get("label") or item.get("display_name") or item.get("name")
    if isinstance(label, str) and label.strip():
        entry["label"] = label.strip()
    if isinstance(item.get("priority"), int):
        entry["priority"] = item["priority"]
    visibility = item.get("visibility")
    if isinstance(visibility, str) and visibility.strip():
        entry["visibility"] = visibility.strip().lower()
    raw_efforts = item.get("reasoning_efforts")
    if raw_efforts is None:
        raw_efforts = item.get("supported_reasoning_levels")
    efforts = _coerce_reasoning_efforts(raw_efforts)
    if raw_efforts is not None:
        entry["reasoning_efforts"] = efforts
    # A preset is a menu effort the client maps before sending, so one the row
    # does not offer is invalid; the strict reader rejects it.
    raw_presets = item.get("client_presets")
    presets = [
        preset
        for preset in _dedupe_str_values(raw_presets if isinstance(raw_presets, list) else ())
        if preset in efforts
    ]
    if presets:
        entry["client_presets"] = presets
    return entry


def _coerce_reasoning_efforts(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    efforts = []
    for value in values:
        efforts.append(value.get("effort") if isinstance(value, dict) else value)
    return _dedupe_str_values(efforts)


def _dedupe_str_values(values: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    normalized = []
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized
