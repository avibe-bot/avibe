"""Search APIs: bounded wire mapping, billing and service-local retry state.

The catalog owns vendor facts. This module never reads a credential, calls a
model engine or writes Source health. Decode/map on a worker; the controller
owns admission and settlement (web-search-services.md §§3–4).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from config.v2_config import (
    KEYLESS_EXA_SOURCE_ID,
    ModelHubConfig,
    ModelHubSourceConfig,
    model_hub_keyless_exa_source,
)
from core.agent_core.messages import HostedCost

from .adapter import SearchResponse
from .events import bounded_diagnostic_detail
from .hosted_executor import Failure, failed_record
from .retry import retry_after_deadline, source_identity

ATTEMPT_SECONDS = 30.0
ZERO = HostedCost(basis="list", usd=0)
BLOCKERS = {
    "service_key_invalid": "models.source.needs_action.credential_revoked",
    "service_balance": "models.source.needs_action.balance_exhausted",
    "service_limit": "models.source.needs_action.spend_limit_reached",
}


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
                    value = "\n".join(part[:bound] for part in value[:bound] if isinstance(part, str))
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
    if sent:
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
        blocker=BLOCKERS.get(code), cooling=code in {"service_unavailable", "service_unreadable"},
        reported_usd=reported,
    )


@dataclass
class _Cooldown:
    identity: tuple
    generation: int
    failures: int = 0
    until: datetime | None = None


class ServiceCooldowns:
    """Independent attempt generations: model calls cannot invalidate search retry state.

    Identity reconciliation retires entries but never recycles a generation. All
    mutations are controller-owned; reads may project the already-held state.
    """

    def __init__(self) -> None:
        self._serial = 0
        self._states: dict[tuple[str, str], _Cooldown] = {}

    def reconcile(self, config: ModelHubConfig) -> None:
        live = {source.id: source_identity(source) for source in config.sources}
        live[KEYLESS_EXA_SOURCE_ID] = source_identity(model_hub_keyless_exa_source())
        self._states = {key: state for key, state in self._states.items() if live.get(key[0]) == state.identity}

    def advance(self, source: ModelHubSourceConfig, service_id: str, *, clear: bool = False) -> int:
        key = (source.id, service_id)
        previous = self._states.get(key)
        self._serial += 1
        same = previous is not None and previous.identity == source_identity(source) and not clear
        self._states[key] = _Cooldown(
            source_identity(source), self._serial,
            previous.failures if same else 0, previous.until if same else None,
        )
        return self._serial

    def generation(self, source: ModelHubSourceConfig, service_id: str) -> int | None:
        state = self._states.get((source.id, service_id))
        return state.generation if state is not None and state.identity == source_identity(source) else None

    def current(self, source: ModelHubSourceConfig, service_id: str, generation: int | None) -> bool:
        return generation is not None and self.generation(source, service_id) == generation

    def until(self, source: ModelHubSourceConfig, service_id: str, now: datetime) -> datetime | None:
        state = self._states.get((source.id, service_id))
        if state is not None and state.identity == source_identity(source) and state.until is not None and state.until > now:
            return state.until
        return None

    def settle(
        self, source: ModelHubSourceConfig, service_id: str, generation: int,
        result: ServiceResult, response: SearchResponse, now: datetime,
    ) -> bool:
        if not self.current(source, service_id, generation):
            return False
        state = self._states[(source.id, service_id)]
        if result.cooling:
            state.failures = min(state.failures + 1, 5)
            advice = None
            if response.http_status in {429, 503}:
                value = next((v for k, v in response.headers.items() if k.lower() == "retry-after"), None)
                advice = retry_after_deadline(value, response.response_received_at or now)
            state.until = advice if advice is not None else now + timedelta(seconds=min(30 * 2 ** (state.failures - 1), 300))
        elif result.record["status"] == "completed":
            state.failures = 0
            state.until = None
        return True
