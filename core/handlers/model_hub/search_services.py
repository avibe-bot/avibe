"""Search APIs: bounded wire mapping, billing and service-local retry state.

The catalog owns vendor facts. This module never reads a credential, calls a
model engine or writes Source health. Decode/map on a worker; the controller
owns admission and settlement (web-search-services.md §§3–4).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from config.v2_config import (
    KEYLESS_EXA_SERVICE_ID,
    ModelHubConfig,
    ModelHubSourceConfig,
    model_hub_builtin_sources,
)
from core.agent_core.messages import HostedCost

from .adapter import SearchResponse
from .events import bounded_diagnostic_detail
from .hosted_executor import Failure, failed_record
from .retry import retry_after_deadline, source_identity

ATTEMPT_SECONDS = 30.0
KEYLESS_EXA_SEARCH_ATTEMPT_SECONDS = 15.0
ZERO = HostedCost(basis="list", usd=0)
BLOCKERS = {
    "service_key_invalid": "models.source.needs_action.credential_revoked",
    "service_balance": "models.source.needs_action.balance_exhausted",
    "service_limit": "models.source.needs_action.spend_limit_reached",
}


_KEYLESS_RATE_LIMIT_RE = re.compile(
    r"(?:rate[- ]?limit|too many requests).{0,160}(?:exceeded|retry|minute|hour|day|free|mcp)?",
    re.IGNORECASE,
)
_KEYLESS_REFUSAL_TAGS = frozenset({"PROHIBITED_CONTENT", "CONTENT_FILTER_ERROR"})
_KEYLESS_TEXT_CONTENT_TYPE_FIELD = "type"
_KEYLESS_TEXT_CONTENT_TYPE = "text"


def _keyless_mcp_content(body: Mapping[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    result = body.get("result")
    if not isinstance(result, Mapping):
        return dict(body), None
    content: Any = result.get("structuredContent")
    if not isinstance(content, Mapping):
        raw_content = result.get("content")
        if isinstance(raw_content, list):
            first_candidate: Mapping[str, Any] | None = None
            for item in raw_content[:8]:
                if not isinstance(item, Mapping) or item.get(_KEYLESS_TEXT_CONTENT_TYPE_FIELD) != _KEYLESS_TEXT_CONTENT_TYPE:
                    continue
                text = item.get("text")
                if not isinstance(text, str) or len(text) > 2 * 1024 * 1024:
                    continue
                try:
                    candidate = json.loads(text)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(candidate, Mapping):
                    continue
                first_candidate = first_candidate or candidate
                if "results" in candidate:
                    content = candidate
                    break
            if not isinstance(content, Mapping):
                content = first_candidate
    if not isinstance(content, Mapping):
        return dict(body), result
    normalized = dict(content)
    metadata = result.get("_meta")
    if isinstance(metadata, Mapping):
        normalized["_meta"] = dict(metadata)
    if result.get("isError") is True:
        normalized["_mcp_is_error"] = True
    return normalized, result


def _keyless_nested_values(value: Any, *, depth: int = 0):
    if depth > 5:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), item
            yield from _keyless_nested_values(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value[:16]:
            yield from _keyless_nested_values(item, depth=depth + 1)


def _keyless_rate_limited(status: int | None, body: Mapping[str, Any] | None) -> bool:
    if status == 429 or not isinstance(body, Mapping):
        return status == 429
    for key, value in _keyless_nested_values(body):
        if key.lower() in {"ai.exa/ratelimited", "ratelimited"} and value is True:
            return True
    error_shaped = body.get("error") is not None or body.get("_mcp_is_error") is True
    if not error_shaped:
        result = body.get("result")
        error_shaped = isinstance(result, Mapping) and result.get("isError") is True
    if not error_shaped:
        return False
    for key, value in _keyless_nested_values(body):
        if key.lower() in {"message", "detail", "text"} and isinstance(value, str):
            if _KEYLESS_RATE_LIMIT_RE.search(value[:512]):
                return True
    return False


def _keyless_refused(body: Mapping[str, Any] | None) -> bool:
    if not isinstance(body, Mapping):
        return False
    return any(
        key.lower() in {"tag", "code", "type"} and isinstance(value, str)
        and value.upper() in _KEYLESS_REFUSAL_TAGS
        for key, value in _keyless_nested_values(body)
    )


def _rate_limit_reset_deadline(value: str | None, received_at: datetime) -> datetime | None:
    if not isinstance(value, str) or len(value) > 32 or not value.isascii() or not value.isdecimal():
        return None
    try:
        deadline = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return deadline if deadline >= received_at else None


def _path(value: Any, path: str) -> Any:
    for key in path.split("."):
        value = value.get(key) if isinstance(value, Mapping) else None
    return value


def _put(value: dict, path: str, item: Any) -> None:
    keys = path.split(".")
    for key in keys[:-1]:
        value = value.setdefault(key, {})
    value[keys[-1]] = item


def search_request(service: Mapping[str, Any], query: str, results: int) -> dict[str, Any]:
    mapping = service["request"]
    request = dict(mapping.get("defaults", {}))
    _put(request, mapping["query"], query)
    _put(request, mapping["results"], results)
    if mapping.get("highlights"):
        _put(request, mapping["highlights"], True)
    return request


@dataclass(frozen=True)
class ServiceResult:
    record: dict[str, Any]
    cost: HostedCost | None
    billable: bool
    uncertain: bool
    authenticated: bool
    blocker: str | None
    cooling: bool
    reported_usd: float | None = None


def map_response(
    service: Mapping[str, Any], response: SearchResponse, *, sent: bool, results: int,
    say: Callable[..., str],
) -> ServiceResult:
    """One outcome and one billing rule, including partial reads and stopped requests.

    Status outranks transport errors: headers already received remain authoritative.
    A malformed non-empty array can be billed even when no row survives mapping.
    """

    body = None
    if response.body is not None:
        try:
            body = json.loads(response.body)
        except (ValueError, UnicodeError, RecursionError):
            pass
    status = response.http_status
    keyless = service.get("service_id") == KEYLESS_EXA_SERVICE_ID
    mcp_result: Mapping[str, Any] | None = None
    if keyless and isinstance(body, Mapping):
        body, mcp_result = _keyless_mcp_content(body)
    mapping = service["result"]
    rows = _path(body, mapping["results"])
    sources: list[dict[str, str]] = []
    if isinstance(rows, list):
        for row in rows:
            url = _path(row, mapping["url"])
            if not isinstance(url, str) or len(url) > 2048:
                continue
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or any(ord(c) < 32 for c in url):
                    continue
            except ValueError:
                continue
            item = {"url": url}
            for field, bound in (("title", 300), ("snippet", 1000), ("page_age", 64)):
                value = _path(row, mapping[field])
                if field == "snippet" and isinstance(value, list):
                    # Exa's highlights, in rank order, never its page text.
                    value = ("\n\n" if keyless else "\n").join(
                        part[:bound] for part in value[:bound] if isinstance(part, str)
                    )
                if isinstance(value, str):
                    item[field] = value[:bound]
            sources.append(item)
            if len(sources) >= results:
                break
    codes = service["outcomes"]
    code = None
    if status in codes["credential"]:
        code = "service_key_invalid"
    elif status in codes["balance"]:
        code = "service_balance"
    elif status in codes["limit"]:
        code = "service_limit"
    elif keyless and _keyless_rate_limited(status, body if isinstance(body, Mapping) else None):
        code = "service_rate_limited"
    elif keyless and _keyless_refused(body if isinstance(body, Mapping) else None):
        code = "service_refused"
    elif keyless and (
        (isinstance(body, Mapping) and body.get("error") is not None)
        or (mcp_result is not None and mcp_result.get("isError") is True)
    ):
        code = "service_unavailable"
    elif keyless and status in service["service_refused_statuses"]:
        # Exa's catalog status set is shared with credentialed Exa. For the
        # keyless path, only the explicit content-filter tags above are a
        # refusal; an unrecognized HTTP error is an unavailable service.
        code = "service_unavailable"
    elif status == 200:
        if not isinstance(rows, list) or (rows and not sources):
            code = "service_unreadable"
    elif status in service["service_refused_statuses"]:
        code = "service_refused"
    else:
        code = "service_unavailable"
    tag = body.get("tag") if isinstance(body, dict) else None
    override = next((rule["code"] for rule in codes.get("tag_overrides", ())
                     if rule["status"] == status and rule["tag"] == tag), None)
    code = override or code
    if code is None:
        record = {"status": "completed", "sources": sources}
    else:
        text = say(f"hostedTools.errorCode.{code}")
        if code == "service_refused":
            # Keep only bounded/redacted provider diagnostics, never a raw response body.
            error = body.get("error") if isinstance(body, dict) else None
            if error is None and mcp_result is not None:
                error = mcp_result.get("error")
                if error is None and mcp_result.get("isError") is True:
                    content = mcp_result.get("content")
                    if isinstance(content, list):
                        error = next((item.get("text") for item in content[:8]
                                      if isinstance(item, Mapping) and isinstance(item.get("text"), str)), None)
            message = error.get("message") if isinstance(error, dict) else error
            message = message if isinstance(message, str) else body.get("message") if isinstance(body, dict) else None
            if not isinstance(message, str) and isinstance(body, dict):
                message = body.get("detail")
                if isinstance(message, dict):
                    message = message.get("error")
                if isinstance(message, list):
                    # Tavily's validation errors: retain vendor messages, never
                    # the echoed request input or arbitrary diagnostic fields.
                    message = "; ".join(row["msg"][:512] for row in message[:5]
                                        if isinstance(row, dict) and isinstance(row.get("msg"), str))
            detail = bounded_diagnostic_detail(message) if isinstance(message, str) else None
            if detail:
                text = f"{text} ({detail})"
        record = failed_record(Failure(text, code))

    billing = service["billing"]
    billable, uncertain = False, False
    if sent and keyless:
        # Keyless Exa's service is free: attempts still enter usage.requests,
        # but never become billable or carry a provider cost estimate.
        billable, uncertain = False, False
    elif sent:
        if status == 200:
            if not billing["nonempty_only"]:
                billable = True
            elif isinstance(rows, list):
                billable = bool(rows)
            else:
                uncertain = True
        elif status is None and response.error not in billing["unbilled_errors"]:
            uncertain = True
    cost = None if uncertain else ZERO
    reported = None
    if billable:
        listed = service["price"]["usd_per_1k"] / 1000
        rule = mapping.get("reported_cost")
        value = _path(body, rule["path"]) if rule else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100 * listed:
            try:
                if math.isfinite(value):
                    reported = float(value)
            except OverflowError:
                pass
        cost = (
            HostedCost(basis="reported", usd=reported, estimate=bool(rule["estimate"]))
            if reported is not None else HostedCost(basis="list", usd=listed)
        )
    return ServiceResult(
        record, cost, billable, uncertain,
        authenticated=status == 200 or code in {"service_balance", "service_limit"},
        blocker=BLOCKERS.get(code), cooling=code in {"service_unavailable", "service_unreadable", "service_rate_limited"},
        reported_usd=reported,
    )


@dataclass
class _Cooldown:
    identity: tuple
    generation: tuple[int, int, int]
    epoch: int
    failures: int = 0
    until: datetime | None = None
    failure_version: int = 0


class ServiceCooldowns:
    """Independent attempt generations: model calls cannot invalidate search retry state.

    Identity reconciliation retires entries but never recycles a generation. All
    mutations are controller-owned; reads may project the already-held state.
    """

    def __init__(self) -> None:
        self._serial = 0
        self._failure_version = 0
        self._states: dict[tuple[str, str], _Cooldown] = {}

    def reconcile(self, config: ModelHubConfig) -> None:
        live = {source.id: source_identity(source) for source in config.sources}
        live.update({source.id: source_identity(source) for source in model_hub_builtin_sources()})
        self._states = {key: state for key, state in self._states.items() if live.get(key[0]) == state.identity}

    def advance(self, source: ModelHubSourceConfig, service_id: str, *, clear: bool = False) -> tuple[int, int, int]:
        key = (source.id, service_id)
        previous = self._states.get(key)
        same = previous is not None and previous.identity == source_identity(source) and not clear
        epoch = previous.epoch if same else (previous.epoch + 1 if previous is not None else 1)
        self._serial += 1
        generation = (epoch, self._serial, self._failure_version)
        self._states[key] = _Cooldown(
            source_identity(source), generation, epoch,
            previous.failures if same else 0,
            previous.until if same else None,
            self._failure_version,
        )
        return generation

    def generation(self, source: ModelHubSourceConfig, service_id: str) -> tuple[int, int, int] | None:
        state = self._states.get((source.id, service_id))
        return state.generation if state is not None and state.identity == source_identity(source) else None

    def current(self, source: ModelHubSourceConfig, service_id: str, generation: tuple[int, int, int] | None) -> bool:
        return generation is not None and self.generation(source, service_id) == generation

    def same_epoch(self, source: ModelHubSourceConfig, service_id: str, generation: tuple[int, int, int] | None) -> bool:
        """Allow concurrent in-flight calls to settle, but not a cleared epoch."""

        state = self._states.get((source.id, service_id))
        return (
            generation is not None
            and state is not None
            and state.identity == source_identity(source)
            and state.epoch == generation[0]
        )

    def until(self, source: ModelHubSourceConfig, service_id: str, now: datetime) -> datetime | None:
        state = self._states.get((source.id, service_id))
        if state is not None and state.identity == source_identity(source) and state.until is not None and state.until > now:
            return state.until
        return None

    def settle(
        self, source: ModelHubSourceConfig, service_id: str, generation: tuple[int, int, int],
        result: ServiceResult, response: SearchResponse, now: datetime,
    ) -> bool:
        if not self.same_epoch(source, service_id, generation):
            return False
        state = self._states[(source.id, service_id)]
        if result.cooling:
            state.failures = min(state.failures + 1, 5)
            received_at = response.response_received_at or now
            advice = None
            value = next((v for k, v in response.headers.items() if k.lower() == "retry-after"), None)
            advice = retry_after_deadline(value, received_at)
            reset = next((v for k, v in response.headers.items() if k.lower() == "x-ratelimit-reset"), None)
            reset_deadline = _rate_limit_reset_deadline(reset, received_at)
            if reset_deadline is not None and (advice is None or reset_deadline > advice):
                advice = reset_deadline
            base_seconds = 60 if source.vendor == "jina-reader" else 30
            state.until = advice if advice is not None else now + timedelta(seconds=min(base_seconds * 2 ** (state.failures - 1), 300))
            self._failure_version += 1
            state.failure_version = self._failure_version
        elif result.record["status"] == "completed":
            active = state.until is not None and state.until > now
            # A call admitted before a concurrent failure must not clear the
            # shared cooldown that the failure published after its admission.
            published_after_admission = state.failure_version != generation[2]
            if not active or not published_after_admission:
                state.failures = 0
                state.until = None
                state.failure_version = self._failure_version
        return True
