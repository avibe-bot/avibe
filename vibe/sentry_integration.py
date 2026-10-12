from __future__ import annotations

import json
import logging
import os
import platform
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from config.v2_config import V2Config
from config import paths

logger = logging.getLogger(__name__)

# Fill this with the real project DSN to make Sentry default-on across deployments.
DEFAULT_SENTRY_DSN = "https://b97175a2d2325951f1861e8a4386f840@o4511104395051008.ingest.us.sentry.io/4511104396820480"
DEFAULT_TRACES_SAMPLE_RATE = 0.0
DEFAULT_PROFILES_SAMPLE_RATE = 0.0
DEPLOYMENT_ENV_VAR = "VIBE_DEPLOYMENT_ENV"

_SENSITIVE_KEY_PARTS = (
    "access_key",
    "aes_key",
    "api_key",
    "app_token",
    "authorization",
    "bot_token",
    "client_secret",
    "cookie",
    "credential",
    "dsn",
    "management_key",
    "passphrase",
    "password",
    "private_key",
    "secret",
    "signing_secret",
    "token",
    "webhook",
    "workspace_token",
)
# Generic names a credential also travels under, matched only as the whole key so
# ``cache_key`` or ``author`` stay readable. Model Hub sends ``{"key": ...}`` to
# validate a credential before it is stored anywhere.
_SENSITIVE_EXACT_KEYS = ("auth", "key")
# Fields that carry a request's query string; dropped whole.
_DROPPED_KEYS = frozenset({"http.query", "http.fragment", "query_string"})
_REDACTED = "[Filtered]"


def _normalized_key(key: str) -> str:
    # ``api_key``, ``apiKey``, ``x-api-key`` and ``API-KEY`` name the same credential.
    return re.sub(r"[^a-z0-9]", "", key.lower())


_SENSITIVE_KEY_TOKENS = tuple(_normalized_key(part) for part in _SENSITIVE_KEY_PARTS)


def _text_key_pattern(token: str) -> str:
    # A normalized key as it may be written in text: any case, with an optional
    # ``_``, ``-`` or ``.`` between its characters.
    return "[_.-]?".join(re.escape(char) for char in token)


# A sensitive key rendered into free text: a dataclass repr, a header line,
# ``key=value``, or JSON, including JSON nested inside a JSON string. The key
# starts at a word boundary and is length-bounded so long word runs stay linear.
# A quoted value ends at its first unescaped closing delimiter; when none is
# found, everything after the opening delimiter is redacted rather than kept. An
# unquoted value led by an auth scheme (``Bearer <token>``) includes the token.
_SENSITIVE_KEY_VALUE_RE = re.compile(
    r"(?i)(?<![\w.-])(?P<kq>\\*[\"']?)"
    r"(?:[\w.-]{0,40}(?:" + "|".join(_text_key_pattern(token) for token in _SENSITIVE_KEY_TOKENS) + r")[\w.-]{0,40}"
    r"|" + "|".join(_text_key_pattern(key) for key in _SENSITIVE_EXACT_KEYS) + r")"
    r"(?P=kq)\s*[:=]\s*"
    r"(?:(?P<vq>\\*[\"'])(?:(?P<quoted>.{0,8192}?)(?:(?<!\\)(?P=vq)|\Z)|(?P<capped>.{8192}))"
    r"|(?P<bare>(?:(?:basic|bearer|bot|digest|token)\s+)?(?:[^\s\"'\\]|\\(?![\"']))+))",
    re.DOTALL,
)


def _redact_key_value(match: re.Match[str]) -> str:
    group = next(name for name in ("quoted", "capped", "bare") if match.group(name) is not None)
    start, end = match.span(group)
    offset = match.start()
    text = match.group(0)
    return text[: start - offset] + _REDACTED + text[end - offset :]


# Any URL keeps only its origin. A path, query, fragment or userinfo carries
# capabilities no key name announces: a Slack response_url, a Discord
# interaction token, WeChat CDN parameters, a Telegram bot token, a proxy
# password. An authority that is not a plain host[:port] is dropped whole.
_URL_RE = re.compile(
    r"(?i)\b(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<authority>[^\s/?#\"'<>\\`]*)(?P<rest>[^\s\"'<>\\`]*)"
)
_URL_HOST_RE = re.compile(r"(?i)(?:[a-z0-9.-]+|\[[0-9a-f:.]+\])(?::\d{1,5})?")
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}"


def _url_origin(match: re.Match[str]) -> str:
    _, at, host = match.group("authority").rpartition("@")
    if not _URL_HOST_RE.fullmatch(host):
        return match.group("scheme") + _REDACTED
    origin = match.group("scheme") + (_REDACTED + at if at else "") + host
    rest = match.group("rest")
    marker = "/" + _REDACTED
    if rest.startswith(marker) and not rest[len(marker) :].rstrip(_URL_TRAILING_PUNCTUATION):
        # Already reduced: scrubbing runs on a breadcrumb and again on its event.
        return origin + rest
    path = rest.rstrip(_URL_TRAILING_PUNCTUATION)
    return origin + (marker if path else "") + rest[len(path) :]


_STRING_REPLACEMENTS = (
    (_URL_RE, _url_origin),
    (re.compile(r"(?i)(authorization[:=]\s*)(bearer\s+)?\S+"), r"\1" + _REDACTED),
    (re.compile(r"(?i)(token=)[^&\s]+"), r"\1" + _REDACTED),
    (re.compile(r"(?i)(cookie[:=]\s*)\S+"), r"\1" + _REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]+\b"), _REDACTED),
    (re.compile(r"\bxapp-[A-Za-z0-9-]+\b"), _REDACTED),
    (_SENSITIVE_KEY_VALUE_RE, _redact_key_value),
    # A Telegram bot token outside a URL (``bot<id>:<secret>``).
    (re.compile(r"\b(bot)?\d{5,}:[A-Za-z0-9_-]{30,}"), r"\1" + _REDACTED),
)
# Credential values Avibe stores. Shorter values are too likely to collide with
# ordinary text to redact by value.
_KNOWN_SECRET_MIN_LENGTH = 8
_KNOWN_SECRETS_LOCK = threading.Lock()
_known_secrets: tuple[tuple[tuple[str, int, int, int], ...] | None, re.Pattern[str] | None] = (None, None)
_ONE_DAY_SECONDS = 60 * 60 * 24
_NOISY_EVENT_TTL_SECONDS = _ONE_DAY_SECONDS
_NOISY_EVENT_CACHE_LIMIT = 128
_NOISY_EVENT_LAST_SEEN: dict[str, float] = {}
_SENTRY_EVENT_CACHE_LOCK = threading.RLock()
_EVENT_RATE_WINDOW_SECONDS = _ONE_DAY_SECONDS * 2
_EVENT_RATE_LIMIT_PER_WINDOW = 1
_EVENT_RATE_CACHE_LIMIT = 2048
_EVENT_RATE_STATE: dict[str, tuple[float, int]] = {}
_NETWORK_EXCEPTION_TYPES = {
    "ClientConnectorDNSError",
    "ClientConnectorError",
    "ClientOSError",
    "ConnectionResetError",
    "ConnectionTimeoutError",
    "ServerDisconnectedError",
    "TimeoutError",
}


def _redact_string(value: str, *, minimize_urls: bool = True) -> str:
    redacted = value
    for pattern, replacement in _STRING_REPLACEMENTS:
        if pattern is _URL_RE and not minimize_urls:
            continue
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized in _SENSITIVE_EXACT_KEYS or any(token in normalized for token in _SENSITIVE_KEY_TOKENS)


def scrub_data(value: Any, *, minimize_urls: bool = True) -> Any:
    if isinstance(value, dict):
        scrubbed: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and (key in _DROPPED_KEYS or _is_sensitive_key(key)):
                scrubbed[key] = _REDACTED
            else:
                scrubbed[key] = scrub_data(item, minimize_urls=minimize_urls)
        return scrubbed
    if isinstance(value, list):
        return [scrub_data(item, minimize_urls=minimize_urls) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_data(item, minimize_urls=minimize_urls) for item in value)
    if isinstance(value, str):
        return _redact_string(value, minimize_urls=minimize_urls)
    return value


def _minimize_breadcrumb(crumb: dict[str, Any]) -> dict[str, Any]:
    if crumb.get("type") != "subprocess" and crumb.get("category") != "subprocess":
        return crumb
    # A command line carries whatever its caller passed; keep the program name.
    words = crumb.get("message").split() if isinstance(crumb.get("message"), str) else []
    return {**crumb, "message": os.path.basename(words[0]) if words else None, "data": {}}


def _minimize_event(event: dict[str, Any]) -> dict[str, Any]:
    minimized = dict(event)
    request = event.get("request")
    if isinstance(request, dict):
        # An incoming request's query, body, cookies, headers and environment
        # stay local; the transaction already names the route.
        minimized["request"] = {key: request[key] for key in ("method", "url") if key in request}
    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict) and isinstance(breadcrumbs.get("values"), list):
        minimized["breadcrumbs"] = {
            **breadcrumbs,
            "values": [
                _minimize_breadcrumb(crumb) if isinstance(crumb, dict) else crumb for crumb in breadcrumbs["values"]
            ],
        }
    return minimized


def _collect_secret_values(
    node: Any, found: set[str], *, value_keys: frozenset[str] = frozenset(), sensitive: bool = False
) -> None:
    if isinstance(node, dict):
        for key, item in node.items():
            item_sensitive = sensitive or (isinstance(key, str) and (key in value_keys or _is_sensitive_key(key)))
            _collect_secret_values(item, found, value_keys=value_keys, sensitive=item_sensitive)
    elif isinstance(node, list):
        for item in node:
            _collect_secret_values(item, found, value_keys=value_keys, sensitive=sensitive)
    elif sensitive and isinstance(node, str) and len(node) >= _KNOWN_SECRET_MIN_LENGTH:
        found.add(node)


def _known_secret_files() -> list[tuple[Path, frozenset[str]]]:
    """Files where Avibe stores credentials, with any extra keys naming a secret."""

    model_hub_state = paths.get_runtime_dir() / "model-hub" / "state"
    files = [(paths.get_config_path(), frozenset())]
    for pattern in ("instances/*/runtime-secrets.json", "auth/*.json", "oauth-staging/*.json"):
        files.extend((path, frozenset()) for path in model_hub_state.glob(pattern))
    # An API-key credential keeps its secret under ``value``.
    files.extend((path, frozenset({"value"})) for path in model_hub_state.glob("credentials/*.json"))
    return files


def _known_secret_pattern() -> re.Pattern[str] | None:
    """Match each credential Avibe stores, whatever text surrounds it.

    Patterns cannot see a secret in prose, an unfamiliar URL, or an extra layer of
    escaping; the exact value can. Files are read as plain JSON (no config
    migration or logging inside a Sentry hook) and re-read only when one changes.
    """

    global _known_secrets
    files: list[tuple[Path, frozenset[str], tuple[str, int, int, int]]] = []
    for path, value_keys in _known_secret_files():
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((path, value_keys, (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)))
    signature = tuple(sorted(entry[2] for entry in files))
    with _KNOWN_SECRETS_LOCK:
        cached_signature, cached_pattern = _known_secrets
        if cached_signature == signature:
            return cached_pattern
        values: set[str] = set()
        for path, value_keys, _ in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # Keep the last good set until the file reads cleanly again.
                return cached_pattern
            _collect_secret_values(data, values, value_keys=value_keys)
        variants = values | {quote(value, safe="") for value in values}
        pattern = (
            re.compile("|".join(re.escape(value) for value in sorted(variants, key=len, reverse=True)))
            if variants
            else None
        )
        _known_secrets = (signature, pattern)
        return pattern


def _redact_matches(value: Any, pattern: re.Pattern[str]) -> Any:
    if isinstance(value, dict):
        return {key: _redact_matches(item, pattern) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_matches(item, pattern) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_matches(item, pattern) for item in value)
    if isinstance(value, str):
        return pattern.sub(_REDACTED, value)
    return value


def _safe_float(raw: Optional[str], fallback: float) -> float:
    if raw is None or raw == "":
        return fallback
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid Sentry sample rate %r, falling back to %s", raw, fallback)
        return fallback
    if 0.0 <= value <= 1.0:
        return value
    logger.warning("Out-of-range Sentry sample rate %r, falling back to %s", raw, fallback)
    return fallback


def _safe_positive_float(raw: Optional[str], fallback: float) -> float:
    if raw is None or raw == "":
        return fallback
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid Sentry positive float %r, falling back to %s", raw, fallback)
        return fallback
    if value >= 0.0:
        return value
    logger.warning("Out-of-range Sentry positive float %r, falling back to %s", raw, fallback)
    return fallback


def _safe_nonnegative_int(raw: Optional[str], fallback: int) -> int:
    if raw is None or raw == "":
        return fallback
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid Sentry nonnegative integer %r, falling back to %s", raw, fallback)
        return fallback
    if value >= 0:
        return value
    logger.warning("Out-of-range Sentry nonnegative integer %r, falling back to %s", raw, fallback)
    return fallback


def detect_sentry_environment() -> str:
    explicit = os.environ.get("VIBE_SENTRY_ENVIRONMENT") or os.environ.get("SENTRY_ENVIRONMENT")
    if explicit:
        return explicit.strip()

    deployment = os.environ.get(DEPLOYMENT_ENV_VAR)
    if deployment:
        return deployment.strip()

    vibe_home = os.environ.get("AVIBE_HOME", "")
    if "incus-regression" in vibe_home or "three-regression" in vibe_home:
        return "regression"

    if os.environ.get("E2E_TEST_MODE", "").lower() in ("true", "1", "yes"):
        return "integration"

    return "local"


def resolve_sentry_options() -> Optional[dict[str, Any]]:
    env_dsn = os.environ.get("VIBE_SENTRY_DSN")
    if env_dsn is not None:
        dsn = env_dsn.strip()
        if not dsn:
            return None
    else:
        fallback_dsn = os.environ.get("SENTRY_DSN")
        if fallback_dsn is not None:
            dsn = fallback_dsn.strip()
            if not dsn:
                return None
        else:
            dsn = DEFAULT_SENTRY_DSN
    if not dsn:
        return None

    return {
        "dsn": dsn,
        "environment": detect_sentry_environment(),
        "traces_sample_rate": _safe_float(
            os.environ.get("VIBE_SENTRY_TRACES_SAMPLE_RATE"),
            DEFAULT_TRACES_SAMPLE_RATE,
        ),
        "profiles_sample_rate": _safe_float(
            os.environ.get("VIBE_SENTRY_PROFILES_SAMPLE_RATE"),
            DEFAULT_PROFILES_SAMPLE_RATE,
        ),
    }


def build_sentry_contexts(config: V2Config, component: str, environment: str) -> dict[str, dict[str, Any]]:
    vibe_home = str(paths.get_vibe_remote_dir())
    default_agent_name = None
    try:
        from core.vibe_agents import VibeAgentStore

        store = VibeAgentStore()
        try:
            default_agent = store.get_default_agent()
            default_agent_name = default_agent.name if default_agent else None
        finally:
            store.close()
    except Exception:
        default_agent_name = None
    return {
        "runtime": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": sys.platform,
        },
        "host": {
            "hostname": socket.gethostname(),
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor() or None,
            "docker": Path("/.dockerenv").exists(),
        },
        "deployment": {
            "component": component,
            "environment": environment,
            "mode": config.mode,
            "primary_platform": config.platforms.primary,
            "enabled_platforms": config.platforms.enabled,
            "default_agent_name": default_agent_name,
            "cwd": config.runtime.default_cwd,
            "vibe_home": vibe_home,
        },
    }


def _event_text(event: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("message", "transaction"):
        value = event.get(key)
        if isinstance(value, str):
            parts.append(value)

    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        for key in ("formatted", "message"):
            value = logentry.get(key)
            if isinstance(value, str):
                parts.append(value)

    for entry in event.get("entries") or []:
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        for key in ("formatted", "message"):
            value = data.get(key)
            if isinstance(value, str):
                parts.append(value)

    return "\n".join(parts)


def _exception_types(event: dict[str, Any]) -> set[str]:
    values = ((event.get("exception") or {}).get("values") or []) if isinstance(event.get("exception"), dict) else []
    return {value.get("type") for value in values if isinstance(value, dict) and isinstance(value.get("type"), str)}


def _exception_values(event: dict[str, Any]) -> list[str]:
    values = ((event.get("exception") or {}).get("values") or []) if isinstance(event.get("exception"), dict) else []
    return [value.get("value") for value in values if isinstance(value, dict) and isinstance(value.get("value"), str)]


def _noisy_event_fingerprint(event: dict[str, Any]) -> Optional[str]:
    logger_name = str(event.get("logger") or "")
    text = _event_text(event)
    exception_types = _exception_types(event)

    if logger_name.startswith("slack_sdk.socket_mode") and (
        "apps.connections.open" in text
        or "Failed to retrieve WSS URL" in text
        or "Failed to check the current session" in text
    ):
        return "slack-socket-mode-reconnect"

    if logger_name == "Lark" and (
        "processor not found, type: im.message.reaction." in text
        or "receive message loop exit, err: no close frame received or sent" in text
    ):
        return "lark-benign-ws-event"

    if logger_name == "core.watches" and ("No space left on device" in text or "Errno 28" in text):
        return "watch-runtime-disk-full"

    if logger_name == "modules.im.wechat" and "Poll loop error" in text and exception_types & _NETWORK_EXCEPTION_TYPES:
        return "wechat-poll-network"

    return None


def _noise_filters_enabled() -> bool:
    raw = os.environ.get("VIBE_SENTRY_NOISE_FILTERS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _noise_filter_ttl_seconds() -> float:
    return _safe_positive_float(os.environ.get("VIBE_SENTRY_NOISE_TTL_SECONDS"), _NOISY_EVENT_TTL_SECONDS)


def _should_drop_noisy_event(event: dict[str, Any]) -> bool:
    if not _noise_filters_enabled():
        return False

    fingerprint = _noisy_event_fingerprint(event)
    if not fingerprint:
        return False

    ttl_seconds = _noise_filter_ttl_seconds()
    if ttl_seconds <= 0:
        return False

    now = time.monotonic()
    with _SENTRY_EVENT_CACHE_LOCK:
        last_seen = _NOISY_EVENT_LAST_SEEN.get(fingerprint)
        _NOISY_EVENT_LAST_SEEN[fingerprint] = now

        if len(_NOISY_EVENT_LAST_SEEN) > _NOISY_EVENT_CACHE_LIMIT:
            expired_before = now - ttl_seconds
            for key, timestamp in list(_NOISY_EVENT_LAST_SEEN.items()):
                if timestamp < expired_before:
                    _NOISY_EVENT_LAST_SEEN.pop(key, None)

    return last_seen is not None and now - last_seen < ttl_seconds


def _event_rate_window_seconds() -> float:
    return _safe_positive_float(os.environ.get("VIBE_SENTRY_EVENT_RATE_WINDOW_SECONDS"), _EVENT_RATE_WINDOW_SECONDS)


def _event_rate_limit_per_window() -> int:
    return _safe_nonnegative_int(
        os.environ.get("VIBE_SENTRY_EVENT_RATE_LIMIT_PER_WINDOW"),
        _EVENT_RATE_LIMIT_PER_WINDOW,
    )


def _normalize_fingerprint_text(text: str) -> str:
    text = re.sub(r"\b[0-9a-f]{16,}\b", "<hex>", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", text, flags=re.IGNORECASE)
    text = re.sub(r"\bs_\d+\b", "s_<id>", text)
    text = re.sub(r"\b\d{6,}\b", "<num>", text)
    text = re.sub(r"file://\S+", "file://<path>", text)
    return text[:240]


def _normalized_event_text(event: dict[str, Any]) -> str:
    event_text = _event_text(event)
    text = event_text.splitlines()[0] if event_text else ""
    return _normalize_fingerprint_text(text)


def _normalized_exception_values(event: dict[str, Any]) -> str:
    return "|".join(_normalize_fingerprint_text(value) for value in _exception_values(event))


def _event_rate_fingerprint(event: dict[str, Any]) -> str:
    logger_name = str(event.get("logger") or "")
    exception_types = ",".join(sorted(_exception_types(event)))
    exception_values = _normalized_exception_values(event)
    return f"{logger_name}|{exception_types}|{exception_values}|{_normalized_event_text(event)}"


def _prune_event_rate_state(now: float, window_seconds: float) -> None:
    if len(_EVENT_RATE_STATE) <= _EVENT_RATE_CACHE_LIMIT:
        return
    expired_before = now - window_seconds
    for key, (window_started_at, _count) in list(_EVENT_RATE_STATE.items()):
        if window_started_at < expired_before:
            _EVENT_RATE_STATE.pop(key, None)
    overflow = len(_EVENT_RATE_STATE) - _EVENT_RATE_CACHE_LIMIT
    if overflow <= 0:
        return
    oldest_keys = [
        key
        for key, _timestamp in sorted(
            ((key, value[0]) for key, value in _EVENT_RATE_STATE.items()),
            key=lambda item: item[1],
        )[:overflow]
    ]
    for key in oldest_keys:
        _EVENT_RATE_STATE.pop(key, None)


def _should_drop_repeated_event(event: dict[str, Any]) -> bool:
    limit = _event_rate_limit_per_window()
    if limit <= 0:
        return False

    window_seconds = _event_rate_window_seconds()
    if window_seconds <= 0:
        return False

    now = time.monotonic()
    fingerprint = _event_rate_fingerprint(event)
    with _SENTRY_EVENT_CACHE_LOCK:
        window_started_at, count = _EVENT_RATE_STATE.get(fingerprint, (now, 0))
        if now - window_started_at >= window_seconds:
            window_started_at = now
            count = 0

        count += 1
        _EVENT_RATE_STATE[fingerprint] = (window_started_at, count)
        _prune_event_rate_state(now, window_seconds)
    return count > limit


def before_send(event: dict[str, Any], hint: dict[str, Any]) -> Optional[dict[str, Any]]:
    del hint
    # Drop decisions stay in process and match on URL paths (Slack's
    # ``apps.connections.open``) and tell errors on one host apart, so they see
    # the text before URLs are reduced to their origin.
    fingerprinted = scrub_data(event, minimize_urls=False)
    if _should_drop_noisy_event(fingerprinted):
        return None
    if _should_drop_repeated_event(fingerprinted):
        return None
    scrubbed = scrub_data(_minimize_event(event))
    known_secrets = _known_secret_pattern()
    if known_secrets is not None:
        scrubbed = _redact_matches(scrubbed, known_secrets)
    return scrubbed


def before_breadcrumb(crumb: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    del hint
    return scrub_data(_minimize_breadcrumb(crumb))


def init_sentry(config: V2Config, component: str, enable_fastapi: bool = False) -> bool:
    options = resolve_sentry_options()
    if not options:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except Exception as exc:
        logger.warning("Sentry initialization skipped: %s", exc)
        return False

    integrations: list[Any] = [
        LoggingIntegration(
            level=logging.WARNING,
            event_level=logging.ERROR,
        )
    ]
    if enable_fastapi:
        try:
            from sentry_sdk.integrations.fastapi import FastApiIntegration
            from sentry_sdk.integrations.starlette import StarletteIntegration
        except Exception as exc:
            logger.warning("FastAPI Sentry integration unavailable: %s", exc)
        else:
            integrations.append(StarletteIntegration())
            integrations.append(FastApiIntegration())

    from vibe import __version__

    try:
        sentry_sdk.init(
            dsn=options["dsn"],
            environment=options["environment"],
            release=f"avibe@{__version__}",
            integrations=integrations,
            before_send=before_send,
            before_breadcrumb=before_breadcrumb,
            attach_stacktrace=True,
            # Frame locals hold user data the scrubber cannot see into: an Agent's tool
            # environment and file contents, raw config bytes. Tracebacks stay; values do not.
            include_local_variables=False,
            ignore_errors=[KeyboardInterrupt, SystemExit],
            max_breadcrumbs=50,
            sample_rate=1.0,
            traces_sample_rate=options["traces_sample_rate"],
            profiles_sample_rate=options["profiles_sample_rate"],
            send_client_reports=False,
            send_default_pii=True,
            server_name=socket.gethostname(),
        )
        sentry_sdk.set_tag("component", component)
        sentry_sdk.set_tag("mode", config.mode)
        sentry_sdk.set_tag("primary_platform", config.platforms.primary)
        sentry_sdk.set_tag("deployment_environment", options["environment"])
        for name, context in build_sentry_contexts(config, component, options["environment"]).items():
            sentry_sdk.set_context(name, context)
    except Exception as exc:
        logger.warning("Sentry initialization failed for %s: %s", component, exc)
        return False
    logger.info("Sentry initialized for %s (environment=%s)", component, options["environment"])
    return True


def capture_exception(exc: Exception) -> None:
    try:
        import sentry_sdk
    except Exception:
        return
    sentry_sdk.capture_exception(exc)
