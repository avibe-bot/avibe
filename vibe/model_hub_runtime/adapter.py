from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

import aiohttp

from config.v2_config import normalize_model_hub_base_url
from core.handlers.model_hub.adapter import (
    DiscoveredModel,
    EngineHealth,
    EngineStatus,
    ObservationDiscovery,
    ObservationOutcome,
    OAuthFlowState,
    OriginNotAllowedError,
    RawCallOutcome,
    RawOutcomeKind,
    RetainedMaterialDisposition,
    RuntimePlatformUnsupportedError,
    SOURCE_PROTOCOLS,
    SourceObservation,
    SourceBinding,
    make_source_observation,
)
from core.handlers.model_hub.errors import ModelDiscoveryError
from vibe.model_hub_runtime.client import (
    _OFFICIAL_BASE_URLS,
    EngineClient,
    EngineClientError,
    EngineInvokeHandle,
    completed_handle,
    probe_models,
    upstream_api_url,
)
from vibe.model_hub_runtime.api_key_vendors import pinned_api_key_protocol
from vibe.model_hub_runtime.installer import InstallClaimTransition
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore
from vibe.model_hub_runtime.supervisor import (
    EngineSupervisor,
    EngineUnavailableError,
    get_engine_supervisor,
)


_OAUTH_ENDPOINTS = {
    "anthropic": ("/anthropic-auth-url", "anthropic", "claude"),
    "openai": ("/codex-auth-url", "codex", "codex"),
    "codex": ("/codex-auth-url", "codex", "codex"),
}
_WEBUI_OAUTH_VENDORS = frozenset(_OAUTH_ENDPOINTS)
_INSTALL_ALREADY_RUNNING_REASON = "model_hub_engine_install_already_running"
_INSTALL_PLATFORM_UNSUPPORTED_REASON = "model_hub_engine_platform_unsupported"
_INSTALL_RECOVERY_TIMEOUT_REASON = "model_hub_engine_install_lock_timeout"
_INSTALL_RECOVERY_ABANDONED_REASON = "model_hub_engine_install_abandoned"
_INSTALL_RECOVERY_SCHEDULE_FAILED_REASON = "model_hub_engine_install_schedule_failed"
_INSTALL_RECOVERY_WAIT_SECONDS = 30.0
_INSTALL_RECOVERY_INITIAL_DELAY_SECONDS = 0.25
_INSTALL_RECOVERY_MAX_DELAY_SECONDS = 4.0


logger = logging.getLogger(__name__)


class _ProtocolProof(Enum):
    PROVEN = "proven"
    UNPROVEN = "unproven"


class _AuthenticationEvidence(Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class _ProtocolObservationShape(Enum):
    NONE = "none"
    GENERIC_REQUEST_ERROR = "generic_request_error"
    HTTP_404 = "http_404"
    NON_JSON = "non_json"
    UNSTRUCTURED = "unstructured"


@dataclass(frozen=True)
class _ProtocolEvidence:
    protocol: _ProtocolProof
    authentication: _AuthenticationEvidence
    shape: _ProtocolObservationShape = _ProtocolObservationShape.NONE


@dataclass(frozen=True)
class _ProtocolEvidenceRule:
    statuses: frozenset[int]
    protocol: _ProtocolProof
    authentication: _AuthenticationEvidence
    shape: _ProtocolObservationShape = _ProtocolObservationShape.NONE
    top_level_field: str | None = None
    top_level_values: frozenset[str] = frozenset()
    error_identifiers: frozenset[str] = frozenset()
    error_params: frozenset[str] | None = None

    def matches(self, status: int, payload: Mapping[str, Any]) -> bool:
        if status not in self.statuses:
            return False
        if self.top_level_field is not None:
            value = payload.get(self.top_level_field)
            if not isinstance(value, str) or value.strip().lower() not in self.top_level_values:
                return False
        error = payload.get("error")
        if self.error_identifiers or self.error_params is not None:
            if not isinstance(error, dict):
                return False
        if self.error_identifiers:
            identifiers = {
                value.strip().lower() for value in (error.get("type"), error.get("code")) if isinstance(value, str)
            }
            if self.error_identifiers.isdisjoint(identifiers):
                return False
        if self.error_params is not None and error.get("param") not in self.error_params:
            return False
        return True


@dataclass(frozen=True)
class _ProtocolObservationTaxonomy:
    """One protocol's request shape and response evidence table.

    The request path and body are part of the same authority as the response
    taxonomy. OpenAI probes deliberately provide the common ``model`` field
    while omitting the candidate-specific ``input`` or ``messages`` field, so
    each endpoint reaches its own protocol-shaped validation error.
    """

    request_path: str
    request_body: Mapping[str, Any]
    oauth_path: str | None
    evidence_rules: tuple[_ProtocolEvidenceRule, ...]


_SUCCESS_STATUSES = frozenset(range(200, 300))
_REQUEST_ERROR_STATUSES = frozenset({400, 404, 422})
_AUTHENTICATION_ERROR_STATUSES = frozenset({401, 403})
_RATE_LIMIT_STATUSES = frozenset({429})
_SERVER_ERROR_STATUSES = frozenset(range(500, 600))

_REQUEST_ERROR_IDENTIFIERS = frozenset(
    {
        "invalid_parameter",
        "invalid_request_error",
        "validation_error",
    }
)
_MODEL_ERROR_IDENTIFIERS = frozenset({"model_not_found", "not_found_error"})
_AUTHENTICATION_ERROR_IDENTIFIERS = frozenset({"authentication_error", "invalid_api_key", "permission_error"})
_SERVER_ERROR_IDENTIFIERS = frozenset({"api_error", "internal_error", "overloaded", "overloaded_error", "server_error"})
_RATE_LIMIT_ERROR_IDENTIFIERS = frozenset({"rate_limit_error", "rate_limit_exceeded"})

_OPENAI_RESPONSES_PARAMS = frozenset(
    {
        "input",
        "instructions",
        "max_output_tokens",
        "previous_response_id",
        "reasoning",
        "text",
        "truncation",
    }
)
_OPENAI_CHAT_PARAMS = frozenset(
    {
        "messages",
        "max_completion_tokens",
        "max_tokens",
        "response_format",
        "stop",
        "temperature",
        "tool_choice",
        "top_p",
    }
)
_OPENAI_FAMILY_PROTOCOLS = frozenset({"openai_responses", "openai_chat"})
_OPENAI_FAMILY_PARAMS = _OPENAI_RESPONSES_PARAMS | _OPENAI_CHAT_PARAMS
_CREDENTIAL_ERROR_PARAMS = frozenset(
    {
        "api_key",
        "api-key",
        "access_token",
        "access-token",
        "authorization",
        "auth",
        "auth_token",
        "auth-token",
        "bearer_token",
        "bearer-token",
        "token",
        "x-api-key",
        "x_api_key",
        "x-auth-token",
        "x_auth_token",
        "x-token",
    }
)
_PAIRWISE_EXCLUSION_SHAPES = frozenset(
    {
        _ProtocolObservationShape.HTTP_404,
        _ProtocolObservationShape.NON_JSON,
        _ProtocolObservationShape.UNSTRUCTURED,
    }
)
_IDENTIFIER_SEPARATOR_RE = re.compile(r"[^0-9A-Za-z]+")
_CAMEL_CASE_BOUNDARY_RE = re.compile(r"(?<=[0-9a-z])(?=[A-Z])")


def _openai_evidence_rules(
    success_objects: frozenset[str],
    request_params: frozenset[str],
) -> tuple[_ProtocolEvidenceRule, ...]:
    return (
        _ProtocolEvidenceRule(
            statuses=_SUCCESS_STATUSES,
            top_level_field="object",
            top_level_values=success_objects,
            protocol=_ProtocolProof.PROVEN,
            authentication=_AuthenticationEvidence.ACCEPTED,
        ),
        _ProtocolEvidenceRule(
            statuses=_REQUEST_ERROR_STATUSES,
            error_identifiers=_REQUEST_ERROR_IDENTIFIERS,
            error_params=request_params,
            protocol=_ProtocolProof.PROVEN,
            authentication=_AuthenticationEvidence.ACCEPTED,
        ),
        _ProtocolEvidenceRule(
            statuses=_REQUEST_ERROR_STATUSES,
            error_identifiers=_MODEL_ERROR_IDENTIFIERS,
            protocol=_ProtocolProof.UNPROVEN,
            authentication=_AuthenticationEvidence.ACCEPTED,
            shape=_ProtocolObservationShape.GENERIC_REQUEST_ERROR,
        ),
        _ProtocolEvidenceRule(
            statuses=_AUTHENTICATION_ERROR_STATUSES,
            error_params=request_params,
            protocol=_ProtocolProof.PROVEN,
            authentication=_AuthenticationEvidence.REJECTED,
        ),
        _ProtocolEvidenceRule(
            statuses=_AUTHENTICATION_ERROR_STATUSES,
            error_identifiers=_AUTHENTICATION_ERROR_IDENTIFIERS,
            protocol=_ProtocolProof.UNPROVEN,
            authentication=_AuthenticationEvidence.REJECTED,
        ),
        _ProtocolEvidenceRule(
            statuses=_SERVER_ERROR_STATUSES,
            error_identifiers=_SERVER_ERROR_IDENTIFIERS,
            protocol=_ProtocolProof.PROVEN,
            authentication=_AuthenticationEvidence.UNKNOWN,
        ),
        _ProtocolEvidenceRule(
            statuses=_RATE_LIMIT_STATUSES,
            error_identifiers=_RATE_LIMIT_ERROR_IDENTIFIERS,
            protocol=_ProtocolProof.PROVEN,
            authentication=_AuthenticationEvidence.UNKNOWN,
        ),
    )


_PROTOCOL_OBSERVATION_TAXONOMY = {
    "anthropic": _ProtocolObservationTaxonomy(
        request_path="/v1/messages",
        request_body={
            # Fail schema validation before a relay selects a model. A synthetic
            # model can otherwise surface as an availability failure even when
            # the credential and interface are valid.
            "max_tokens": 0,
            "messages": [],
        },
        oauth_path="/v1/messages?beta=true",
        evidence_rules=(
            _ProtocolEvidenceRule(
                statuses=_SUCCESS_STATUSES,
                top_level_field="type",
                top_level_values=frozenset({"message"}),
                protocol=_ProtocolProof.PROVEN,
                authentication=_AuthenticationEvidence.ACCEPTED,
            ),
            _ProtocolEvidenceRule(
                statuses=_REQUEST_ERROR_STATUSES,
                top_level_field="type",
                top_level_values=frozenset({"error"}),
                error_identifiers=_REQUEST_ERROR_IDENTIFIERS | _MODEL_ERROR_IDENTIFIERS,
                protocol=_ProtocolProof.PROVEN,
                authentication=_AuthenticationEvidence.ACCEPTED,
            ),
            _ProtocolEvidenceRule(
                statuses=_AUTHENTICATION_ERROR_STATUSES,
                top_level_field="type",
                top_level_values=frozenset({"error"}),
                error_identifiers=_AUTHENTICATION_ERROR_IDENTIFIERS,
                protocol=_ProtocolProof.PROVEN,
                authentication=_AuthenticationEvidence.REJECTED,
            ),
            _ProtocolEvidenceRule(
                statuses=_SERVER_ERROR_STATUSES,
                top_level_field="type",
                top_level_values=frozenset({"error"}),
                error_identifiers=_SERVER_ERROR_IDENTIFIERS,
                protocol=_ProtocolProof.PROVEN,
                authentication=_AuthenticationEvidence.UNKNOWN,
            ),
            _ProtocolEvidenceRule(
                statuses=_RATE_LIMIT_STATUSES,
                top_level_field="type",
                top_level_values=frozenset({"error"}),
                error_identifiers=_RATE_LIMIT_ERROR_IDENTIFIERS,
                protocol=_ProtocolProof.PROVEN,
                authentication=_AuthenticationEvidence.UNKNOWN,
            ),
        ),
    ),
    "openai_responses": _ProtocolObservationTaxonomy(
        request_path="/v1/responses",
        request_body={"model": "__avibe_model_hub_probe__"},
        oauth_path="/backend-api/codex/responses",
        evidence_rules=_openai_evidence_rules(
            frozenset({"response"}),
            _OPENAI_RESPONSES_PARAMS,
        ),
    ),
    "openai_chat": _ProtocolObservationTaxonomy(
        request_path="/v1/chat/completions",
        request_body={"model": "__avibe_model_hub_probe__"},
        oauth_path=None,
        evidence_rules=_openai_evidence_rules(
            frozenset({"chat.completion", "chat.completion.chunk"}),
            _OPENAI_CHAT_PARAMS,
        ),
    ),
}


def _error_identifiers(error: Mapping[str, Any]) -> frozenset[str]:
    return _payload_identifiers(error)


def _normalized_identifier(value: str) -> str:
    normalized = _CAMEL_CASE_BOUNDARY_RE.sub("_", value.strip())
    normalized = _IDENTIFIER_SEPARATOR_RE.sub("_", normalized).strip("_").lower()
    return normalized


def _payload_identifiers(payload: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(
        normalized
        for value in (payload.get("type"), payload.get("code"))
        if isinstance(value, str)
        if (normalized := _normalized_identifier(value))
    )


def _error_param_name(error: Mapping[str, Any]) -> str | None:
    value = error.get("param")
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _request_error_rejects_credential(
    status: int,
    error: Mapping[str, Any] | None,
) -> bool:
    return (
        status in _REQUEST_ERROR_STATUSES
        and isinstance(error, dict)
        and _error_param_name(error) in _CREDENTIAL_ERROR_PARAMS
    )


def _payload_is_structured(payload: Mapping[str, Any]) -> bool:
    return (
        isinstance(payload.get("error"), dict)
        or isinstance(payload.get("type"), str)
        or isinstance(payload.get("code"), str)
        or isinstance(payload.get("object"), str)
    )


def _anthropic_wrapperless_error_kind(
    status: int,
    payload: Mapping[str, Any],
) -> str | None:
    if "type" in payload:
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    if _request_error_rejects_credential(status, error):
        return "rejected"
    identifiers = _error_identifiers(error)
    if status in _REQUEST_ERROR_STATUSES and not (
        _REQUEST_ERROR_IDENTIFIERS | _MODEL_ERROR_IDENTIFIERS
    ).isdisjoint(identifiers):
        return "accepted"
    if status in _AUTHENTICATION_ERROR_STATUSES and not _AUTHENTICATION_ERROR_IDENTIFIERS.isdisjoint(identifiers):
        return "rejected"
    if status in _SERVER_ERROR_STATUSES and not _SERVER_ERROR_IDENTIFIERS.isdisjoint(identifiers):
        return "unknown"
    if status in _RATE_LIMIT_STATUSES and not _RATE_LIMIT_ERROR_IDENTIFIERS.isdisjoint(identifiers):
        return "unknown"
    return None


def _openai_wrapperless_error_kind(
    status: int,
    payload: Mapping[str, Any],
) -> str | None:
    if isinstance(payload.get("error"), dict):
        return None
    identifiers = _payload_identifiers(payload)
    if not identifiers:
        return None
    if status in _REQUEST_ERROR_STATUSES and not (
        _REQUEST_ERROR_IDENTIFIERS | _MODEL_ERROR_IDENTIFIERS
    ).isdisjoint(identifiers):
        return "accepted"
    if status in _AUTHENTICATION_ERROR_STATUSES and not _AUTHENTICATION_ERROR_IDENTIFIERS.isdisjoint(identifiers):
        return "rejected"
    if status in _SERVER_ERROR_STATUSES and not _SERVER_ERROR_IDENTIFIERS.isdisjoint(identifiers):
        return "unknown"
    if status in _RATE_LIMIT_STATUSES and not _RATE_LIMIT_ERROR_IDENTIFIERS.isdisjoint(identifiers):
        return "unknown"
    return None


def _default_unproven_shape(
    *,
    status: int,
    payload: Mapping[str, Any] | None = None,
    non_json: bool = False,
) -> _ProtocolObservationShape:
    if status == 404:
        return _ProtocolObservationShape.HTTP_404
    if status not in _REQUEST_ERROR_STATUSES:
        return _ProtocolObservationShape.NONE
    if non_json:
        return _ProtocolObservationShape.NON_JSON
    if payload is None or not _payload_is_structured(payload):
        return _ProtocolObservationShape.UNSTRUCTURED
    return _ProtocolObservationShape.NONE


def _parse_protocol_authenticated_evidence(
    protocol: str,
    status: int,
    body: str | bytes,
) -> _ProtocolEvidence:
    """Classify protocol proof and authentication as independent evidence.

    Observation is sequential and stops at the first authenticated proof.
    Status, vendor, URL, and probe order never prove a protocol or credential
    by themselves; a generic response therefore remains unproven even when its
    HTTP status is conventionally associated with authentication or validation.
    A structured authentication error may prove credential rejection without
    distinguishing the attempted OpenAI protocol. Accepted, rejected, and unknown
    are all positive table entries; there is no default authentication result for
    a shaped response. The observation result exposes a non-null protocol if and
    only if a protocol-specific response shape also proves authentication
    acceptance.
    """

    try:
        payload = json.loads(body)
    except (TypeError, UnicodeDecodeError, ValueError):
        return _ProtocolEvidence(
            protocol=_ProtocolProof.UNPROVEN,
            authentication=_AuthenticationEvidence.UNKNOWN,
            shape=_default_unproven_shape(status=status, non_json=True),
        )
    if not isinstance(payload, dict):
        return _ProtocolEvidence(
            protocol=_ProtocolProof.UNPROVEN,
            authentication=_AuthenticationEvidence.UNKNOWN,
            shape=_default_unproven_shape(status=status),
        )

    top_level_identifiers = _payload_identifiers(payload)
    if (
        status in _AUTHENTICATION_ERROR_STATUSES
        and not _AUTHENTICATION_ERROR_IDENTIFIERS.isdisjoint(top_level_identifiers)
    ):
        return _ProtocolEvidence(
            protocol=_ProtocolProof.UNPROVEN,
            authentication=_AuthenticationEvidence.REJECTED,
        )
    error = payload.get("error")
    if _request_error_rejects_credential(status, error):
        return _ProtocolEvidence(
            protocol=_ProtocolProof.UNPROVEN,
            authentication=_AuthenticationEvidence.REJECTED,
        )

    if protocol == "anthropic":
        wrapperless = _anthropic_wrapperless_error_kind(status, payload)
        if wrapperless == "accepted":
            return _ProtocolEvidence(
                protocol=_ProtocolProof.UNPROVEN,
                authentication=_AuthenticationEvidence.ACCEPTED,
                shape=_ProtocolObservationShape.GENERIC_REQUEST_ERROR,
            )
        if wrapperless == "rejected":
            return _ProtocolEvidence(
                protocol=_ProtocolProof.UNPROVEN,
                authentication=_AuthenticationEvidence.REJECTED,
            )
        if wrapperless == "unknown":
            return _ProtocolEvidence(
                protocol=_ProtocolProof.UNPROVEN,
                authentication=_AuthenticationEvidence.UNKNOWN,
            )

    taxonomy = _PROTOCOL_OBSERVATION_TAXONOMY.get(protocol)
    for rule in taxonomy.evidence_rules if taxonomy is not None else ():
        if rule.matches(status, payload):
            return _ProtocolEvidence(
                protocol=rule.protocol,
                authentication=rule.authentication,
                shape=rule.shape,
            )
    if protocol in _OPENAI_FAMILY_PROTOCOLS and status in _REQUEST_ERROR_STATUSES:
        wrapperless = _openai_wrapperless_error_kind(status, payload)
        if wrapperless == "accepted":
            return _ProtocolEvidence(
                protocol=_ProtocolProof.UNPROVEN,
                authentication=_AuthenticationEvidence.ACCEPTED,
                shape=_ProtocolObservationShape.GENERIC_REQUEST_ERROR,
            )
        if isinstance(error, dict):
            identifiers = _error_identifiers(error)
            if (
                not _REQUEST_ERROR_IDENTIFIERS.isdisjoint(identifiers)
                and _AUTHENTICATION_ERROR_IDENTIFIERS.isdisjoint(identifiers)
                and _error_param_name(error) not in _OPENAI_FAMILY_PARAMS
            ):
                return _ProtocolEvidence(
                    protocol=_ProtocolProof.UNPROVEN,
                    authentication=_AuthenticationEvidence.ACCEPTED,
                    shape=_ProtocolObservationShape.GENERIC_REQUEST_ERROR,
                )
    if _response_shape_proves_protocol(protocol, payload):
        return _ProtocolEvidence(
            protocol=_ProtocolProof.PROVEN,
            authentication=_AuthenticationEvidence.UNKNOWN,
        )
    return _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
        shape=_default_unproven_shape(status=status, payload=payload),
    )


async def _probe_protocol_response(
    *,
    vendor: str,
    protocol: str,
    base_url: str | None,
    secret: str,
    timeout: float = 15.0,
) -> _ProtocolEvidence:
    """Require a response from the candidate protocol's distinct request path."""

    root = base_url or _OFFICIAL_BASE_URLS.get(vendor)
    if not root:
        raise EngineClientError("source requires a base URL for protocol observation")
    taxonomy = _PROTOCOL_OBSERVATION_TAXONOMY.get(protocol)
    if taxonomy is None:
        raise EngineClientError("unsupported source protocol")
    try:
        url = upstream_api_url(root, taxonomy.request_path)
    except (TypeError, ValueError):
        raise EngineClientError("source base URL is invalid")
    assert url is not None
    headers = {
        "Authorization": f"Bearer {secret}",
        "Accept": "application/json",
    }
    if protocol == "anthropic":
        headers = {
            "x-api-key": secret,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        }
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.post(
                url,
                headers=headers,
                json=dict(taxonomy.request_body),
                allow_redirects=False,
            ) as response:
                body = await response.content.read(64 * 1024)
                return _parse_protocol_authenticated_evidence(
                    protocol,
                    response.status,
                    body,
                )
    except asyncio.TimeoutError:
        raise EngineClientError("protocol observation timed out", error_type="timeout") from None
    except aiohttp.ClientError:
        raise EngineClientError(
            "protocol observation failed",
            error_type="network_error",
        ) from None


@dataclass(frozen=True)
class _AuthRecord:
    identity: str
    auth_index: str
    name: str
    provider: str
    fingerprint: str
    account_id: str | None = None


def _response_shape_proves_protocol(
    protocol: str,
    body: Mapping[str, Any],
) -> bool:
    """Recognize protocol-specific shapes without inferring authentication."""

    if protocol == "anthropic":
        error = body.get("error")
        return body.get("type") == "message" or (
            body.get("type") == "error"
            and isinstance(error, dict)
            and isinstance(error.get("type"), str)
        )
    error = body.get("error")
    if protocol == "openai_responses":
        return body.get("object") == "response" or (
            isinstance(error, dict) and error.get("param") in _OPENAI_RESPONSES_PARAMS
        )
    if protocol == "openai_chat":
        return body.get("object") in {"chat.completion", "chat.completion.chunk"} or (
            isinstance(error, dict) and error.get("param") in _OPENAI_CHAT_PARAMS
        )
    return False


def _openai_family_elimination_proof(
    responses: Mapping[str, _ProtocolEvidence],
    *,
    considered_protocols: frozenset[str],
    ruled_out_protocols: frozenset[str],
) -> str | None:
    """Prove one OpenAI-family protocol from the pair of responses, not from the URL.

    A request-error row with matched identifiers but no family-distinctive
    ``param`` proves only that one endpoint parsed the synthetic request with
    authentication accepted. The sibling protocol becomes excluded only when its
    own endpoint answers the same source with an unproven shape, so the proof is
    carried by the response pair rather than by either request path alone. That
    pairwise proof is valid only when every other probed protocol was already
    ruled out for this source. A competing protocol can be ruled out by an
    explicit authentication rejection or by a definitive request-error-shaped
    exclusion. Transient upstream failures do not qualify.
    """

    if not {
        protocol for protocol in considered_protocols if protocol not in _OPENAI_FAMILY_PROTOCOLS
    }.issubset(ruled_out_protocols):
        return None

    candidate = responses.get("openai_responses")
    sibling = responses.get("openai_chat")
    if (
        candidate is not None
        and sibling is not None
        and candidate.protocol is _ProtocolProof.UNPROVEN
        and candidate.authentication is _AuthenticationEvidence.ACCEPTED
        and candidate.shape is _ProtocolObservationShape.GENERIC_REQUEST_ERROR
        and sibling.protocol is _ProtocolProof.UNPROVEN
        and sibling.authentication is _AuthenticationEvidence.UNKNOWN
        and sibling.shape in _PAIRWISE_EXCLUSION_SHAPES
    ):
        return "openai_responses"
    if (
        candidate is not None
        and sibling is not None
        and sibling.protocol is _ProtocolProof.UNPROVEN
        and sibling.authentication is _AuthenticationEvidence.ACCEPTED
        and sibling.shape is _ProtocolObservationShape.GENERIC_REQUEST_ERROR
        and candidate.protocol is _ProtocolProof.UNPROVEN
        and candidate.authentication is _AuthenticationEvidence.UNKNOWN
        and candidate.shape in _PAIRWISE_EXCLUSION_SHAPES
    ):
        return "openai_chat"
    return None


def _pairwise_positive_exclusion(evidence: _ProtocolEvidence) -> bool:
    return (
        evidence.protocol is _ProtocolProof.UNPROVEN
        and evidence.authentication is _AuthenticationEvidence.UNKNOWN
        and evidence.shape in _PAIRWISE_EXCLUSION_SHAPES
    )


def _protocol_is_persistable_without_shape_proof(
    *,
    credential_kind: str,
    vendor: str,
    protocol: str,
    protocol_order: Sequence[str],
) -> bool:
    if credential_kind != "api_key":
        return False
    pinned_protocol = pinned_api_key_protocol(vendor)
    if pinned_protocol == protocol:
        return True
    return vendor == "custom" and len(protocol_order) == 1 and protocol_order[0] == protocol


def _anthropic_wrapperless_elimination_proof(
    responses: Mapping[str, _ProtocolEvidence],
    *,
    considered_protocols: frozenset[str],
    ruled_out_protocols: frozenset[str],
) -> str | None:
    """Prove wrapperless Anthropic only from the complete response set.

    A wrapperless ``{"error": {"type": ...}}`` request error still proves only
    that the endpoint accepted the credential and parsed the synthetic request.
    It becomes Anthropic evidence only when both OpenAI-family sibling probes
    were actually part of this observation round and their own responses ruled
    them out. The proof is therefore carried by the response set, not by the
    ``/v1/messages`` path alone.
    """

    if not _OPENAI_FAMILY_PROTOCOLS.issubset(considered_protocols):
        return None
    if not _OPENAI_FAMILY_PROTOCOLS.issubset(ruled_out_protocols):
        return None
    candidate = responses.get("anthropic")
    if (
        candidate is not None
        and candidate.protocol is _ProtocolProof.UNPROVEN
        and candidate.authentication is _AuthenticationEvidence.ACCEPTED
        and candidate.shape is _ProtocolObservationShape.GENERIC_REQUEST_ERROR
    ):
        return "anthropic"
    return None


def _probe_oauth_protocol_response(
    *,
    client: EngineClient,
    auth: _AuthRecord,
    vendor: str,
    protocol: str,
) -> _ProtocolEvidence:
    """Probe one allowlisted OAuth upstream through the engine-held credential."""

    taxonomy = _PROTOCOL_OBSERVATION_TAXONOMY.get(protocol)
    if taxonomy is None:
        raise EngineClientError("unsupported source protocol")
    if vendor == "anthropic" and protocol == "anthropic":
        url = f"https://api.anthropic.com{taxonomy.oauth_path or taxonomy.request_path}"
        headers = {
            "Authorization": "Bearer $TOKEN$",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Anthropic-Version": "2023-06-01",
            "Anthropic-Beta": "oauth-2025-04-20",
            "X-App": "cli",
        }
    elif vendor in {"openai", "codex"} and protocol == "openai_responses":
        url = f"https://chatgpt.com{taxonomy.oauth_path or taxonomy.request_path}"
        headers = {
            "Authorization": "Bearer $TOKEN$",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Originator": "codex-tui",
        }
        if auth.account_id:
            headers["Chatgpt-Account-Id"] = auth.account_id
    else:
        raise EngineClientError(
            "OAuth credential does not support this protocol path",
            status_code=404,
        )
    payload = client.management_request(
        "POST",
        "/api-call",
        payload={
            "auth_index": auth.auth_index,
            "method": "POST",
            "url": url,
            "header": headers,
            "data": json.dumps(taxonomy.request_body, separators=(",", ":")),
        },
    )
    status = payload.get("status_code")
    if not isinstance(status, int) or isinstance(status, bool):
        raise EngineClientError(
            "protocol observation returned an invalid status",
            error_type="invalid_json",
        )
    body = payload.get("body")
    return _parse_protocol_authenticated_evidence(
        protocol,
        status,
        body if isinstance(body, str) else "",
    )


@dataclass
class _OAuthFlow:
    flow_id: str
    source_id: str
    engine_state: str
    vendor: str
    callback_provider: str
    auth_provider: str
    expects: str
    auth_url: str | None
    device_code: str | None
    expires_at_iso: str
    before_auth_fingerprints: dict[str, str]
    state: str = "awaiting_action"
    error_key: str | None = None
    credential_ref: str | None = None
    retained_material_disposition: RetainedMaterialDisposition = RetainedMaterialDisposition.NONE
    retained_credential_ref: str | None = None
    grant_write_possible: bool = False
    retained_material_decided: bool = False
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def snapshot(self) -> OAuthFlowState:
        return OAuthFlowState(
            flow_id=self.flow_id,
            source_id=self.source_id,
            vendor=self.vendor,
            state=self.state,
            auth_url=self.auth_url,
            device_code=self.device_code,
            expects=self.expects,
            instructions_key=f"models.oauth.{self.vendor}.{self.expects}",
            error_key=self.error_key,
            expires_at_iso=self.expires_at_iso,
            credential_ref=self.credential_ref,
            channel="hub",
            retained_material_disposition=self.retained_material_disposition,
            retained_credential_ref=self.retained_credential_ref,
        )


class CLIProxyEngineAdapter:
    """Managed CLIProxyAPI implementation of the frozen EngineAdapter contract."""

    def __init__(
        self,
        *,
        supervisor: EngineSupervisor | None = None,
        state_store: EngineStateStore | None = None,
    ) -> None:
        self.supervisor = supervisor or get_engine_supervisor()
        self.state_store = state_store or self.supervisor.state_store
        self._routing_lock = asyncio.Lock()
        self._installation_lock = asyncio.Lock()
        self._install_task: asyncio.Task[None] | None = None
        self._install_admission: asyncio.Future[EngineStatus] | None = None
        self._install_owner_active = False
        self._start_after_install_task: asyncio.Task[None] | None = None
        self._installation_stopping = False
        self._oauth_flows: dict[str, _OAuthFlow] = {}
        self._active_oauth_providers: set[str] = set()
        self._oauth_lock = threading.RLock()

    async def install(self) -> EngineStatus:
        await self.recover_installation()
        async with self._installation_lock:
            task = self._install_task
            admission = self._install_admission
            if task is not None and not task.done():
                if admission is None:
                    return await self.status()
            else:
                status = await self.status()
                if status.health is not EngineHealth.NOT_INSTALLED:
                    return status
                if self._installation_stopping:
                    raise EngineUnavailableError(
                        "models.engine.install_failed",
                        reason="engine_stopping",
                    )
                admission = asyncio.get_running_loop().create_future()
                self._install_admission = admission
                self._start_install_task_locked(
                    generation=uuid.uuid4().hex,
                    expected_target=None,
                    not_installed=status,
                    admission=admission,
                    claim_owned=False,
                )
        assert admission is not None
        return await asyncio.shield(admission)

    async def recover_installation(self) -> EngineStatus:
        async with self._installation_lock:
            install_task = self._install_task
            if install_task is not None and not install_task.done():
                return await self.status()
            install_state = await asyncio.to_thread(self.supervisor.installer.install_state)
            if not install_state or install_state.get("state") != "installing":
                return await self.status()
            generation = uuid.uuid4().hex
            claimed = await asyncio.to_thread(
                self.supervisor.installer.transition_install_claim,
                InstallClaimTransition.RESUME,
                generation=generation,
                previous_generation=install_state.get("generation"),
                target=install_state["target"],
            )
            if not claimed:
                return await self.status()
            try:
                self._start_install_task_locked(
                    generation=generation,
                    expected_target=install_state["target"],
                    not_installed=None,
                    admission=None,
                    claim_owned=True,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to schedule Model Hub runtime install recovery")
                await self._abandon_install_claim(
                    generation=generation,
                    target=install_state["target"],
                    reason=_INSTALL_RECOVERY_SCHEDULE_FAILED_REASON,
                )
            return await self.status()

    def _start_install_task_locked(
        self,
        *,
        generation: str,
        expected_target: Mapping[str, str] | None,
        not_installed: EngineStatus | None,
        admission: asyncio.Future[EngineStatus] | None,
        claim_owned: bool,
    ) -> None:
        task = asyncio.create_task(
            self._run_installation(
                generation=generation,
                expected_target=expected_target,
                not_installed=not_installed,
                admission=admission,
                claim_owned=claim_owned,
            ),
            name="model-hub-runtime-install",
        )
        self._install_task = task
        self._install_owner_active = True
        task.add_done_callback(self._installation_done)

    def _installation_done(self, task: asyncio.Task[None]) -> None:
        if self._install_task is task:
            self._install_task = None
            self._install_admission = None
            self._install_owner_active = False
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception("Model Hub runtime install task failed")

    def _start_after_install_done(self, task: asyncio.Task[None]) -> None:
        if self._start_after_install_task is task:
            self._start_after_install_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception("Model Hub runtime start after installation failed")

    async def _start_after_install(self, install_task: asyncio.Task[None]) -> None:
        await asyncio.shield(install_task)
        async with self._installation_lock:
            if self._installation_stopping:
                return
            status = await self.status()
            if status.health in {
                EngineHealth.INSTALLING,
                EngineHealth.NOT_INSTALLED,
            }:
                return
            await asyncio.to_thread(self.supervisor.ensure_running)

    async def _run_installation(
        self,
        *,
        generation: str,
        expected_target: Mapping[str, str] | None,
        not_installed: EngineStatus | None,
        admission: asyncio.Future[EngineStatus] | None,
        claim_owned: bool,
    ) -> None:
        loop = asyncio.get_running_loop()
        recovery_deadline: float | None = None
        recovery_delay = _INSTALL_RECOVERY_INITIAL_DELAY_SECONDS
        claimed_target: dict[str, str] | None = (
            dict(expected_target) if expected_target is not None else None
        )

        def persist_claim(target: dict[str, str]) -> None:
            nonlocal claim_owned, claimed_target
            claimed_target = dict(target)
            if expected_target is not None:
                return
            claim_owned = self.supervisor.installer.transition_install_claim(
                InstallClaimTransition.CREATE,
                generation=generation,
                target=target,
            )
            if not claim_owned:
                raise RuntimeError("Model Hub runtime install claim moved during admission")
            assert not_installed is not None
            assert admission is not None
            installing = replace(
                not_installed,
                health=EngineHealth.INSTALLING,
                installed_version=None,
                verified=False,
                listen_port=None,
                error_key=None,
            )
            loop.call_soon_threadsafe(self._resolve_install_admission, admission, installing)

        try:
            while True:
                try:
                    await self.ensure_installed(
                        expected_target=expected_target,
                        on_resolved=persist_claim,
                    )
                    break
                except EngineUnavailableError as exc:
                    if (
                        expected_target is None
                        or exc.reason != _INSTALL_ALREADY_RUNNING_REASON
                    ):
                        raise
                    if self._installation_stopping:
                        await self._abandon_install_claim(
                            generation=generation,
                            target=claimed_target,
                        )
                        return
                    now = loop.time()
                    if recovery_deadline is None:
                        recovery_deadline = now + _INSTALL_RECOVERY_WAIT_SECONDS
                        logger.warning(
                            "Model Hub runtime recovery waiting up to %.1fs for the shared install lock",
                            _INSTALL_RECOVERY_WAIT_SECONDS,
                        )
                    remaining = recovery_deadline - now
                    if remaining <= 0:
                        logger.error(
                            "Model Hub runtime recovery gave up waiting for the shared install lock"
                        )
                        raise EngineUnavailableError(
                            "models.engine.install_failed",
                            reason=_INSTALL_RECOVERY_TIMEOUT_REASON,
                        )
                    await asyncio.sleep(min(recovery_delay, remaining))
                    recovery_delay = min(
                        recovery_delay * 2,
                        _INSTALL_RECOVERY_MAX_DELAY_SECONDS,
                    )
            installed = await asyncio.to_thread(
                self.supervisor.installer.resolve_engine_path,
            )
            if installed is None:
                raise EngineUnavailableError("models.engine.install_failed")
            settled = await asyncio.to_thread(
                self.supervisor.installer.transition_install_claim,
                InstallClaimTransition.SETTLE_SUCCESS,
                generation=generation,
                target=claimed_target,
            )
            self._install_owner_active = False
            if settled:
                await asyncio.to_thread(self.supervisor.note_installation_settled)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._install_owner_active = False
            reason = self._install_failure_reason(exc)
            if claim_owned:
                try:
                    await asyncio.to_thread(
                        self.supervisor.installer.transition_install_claim,
                        InstallClaimTransition.SETTLE_FAILURE,
                        generation=generation,
                        target=claimed_target,
                        reason=reason,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to persist Model Hub runtime install failure")
            elif admission is not None and reason != _INSTALL_PLATFORM_UNSUPPORTED_REASON:
                try:
                    await asyncio.to_thread(
                        self.supervisor.installer.transition_install_claim,
                        InstallClaimTransition.ADMISSION_FAILURE,
                        generation=generation,
                        reason=reason,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to persist Model Hub runtime admission failure")
            if admission is not None:
                self._reject_install_admission(admission, exc)

    async def _abandon_install_claim(
        self,
        *,
        generation: str,
        target: Mapping[str, str] | None,
        reason: str = _INSTALL_RECOVERY_ABANDONED_REASON,
    ) -> None:
        self._install_owner_active = False
        try:
            await asyncio.to_thread(
                self.supervisor.installer.transition_install_claim,
                InstallClaimTransition.ABANDON,
                generation=generation,
                target=target,
                reason=reason,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to abandon Model Hub runtime install claim")

    @staticmethod
    def _resolve_install_admission(
        admission: asyncio.Future[EngineStatus],
        status: EngineStatus,
    ) -> None:
        if not admission.done():
            admission.set_result(status)

    @staticmethod
    def _reject_install_admission(
        admission: asyncio.Future[EngineStatus],
        error: Exception,
    ) -> None:
        if not admission.done():
            admission.set_exception(error)

    @staticmethod
    def _install_failure_reason(error: Exception) -> str:
        if isinstance(error, RuntimePlatformUnsupportedError):
            return _INSTALL_PLATFORM_UNSUPPORTED_REASON
        if isinstance(error, EngineUnavailableError) and error.reason:
            return error.reason
        return "model_hub_engine_install_failed"

    @staticmethod
    def _install_failure(reason: str) -> Exception:
        if reason == _INSTALL_PLATFORM_UNSUPPORTED_REASON:
            return RuntimePlatformUnsupportedError()
        return EngineUnavailableError("models.engine.install_failed", reason=reason)

    async def ensure_installed(
        self,
        *,
        expected_target: Mapping[str, str] | None = None,
        on_resolved: Callable[[dict[str, str]], None] | None = None,
    ) -> EngineStatus:
        async with self._routing_lock:
            if expected_target is None and on_resolved is None:
                install = await asyncio.to_thread(self.supervisor.installer.ensure)
            else:
                install = await asyncio.to_thread(
                    self.supervisor.installer.ensure,
                    expected_target=expected_target,
                    on_resolved=on_resolved,
                )
            if not install.get("ok"):
                reason = str(install.get("reason") or "engine_install_failed")
                raise self._install_failure(reason)
            if install.get("changed"):
                await asyncio.to_thread(self.supervisor.restart_if_running)
            return await self.status()

    async def start(self) -> EngineStatus:
        async with self._installation_lock:
            status = await self.status()
            if status.health is EngineHealth.INSTALLING:
                install_task = self._install_task
                if install_task is not None and not install_task.done():
                    start_task = self._start_after_install_task
                    if start_task is None or start_task.done():
                        start_task = asyncio.create_task(
                            self._start_after_install(install_task),
                            name="model-hub-runtime-start-after-install",
                        )
                        self._start_after_install_task = start_task
                        start_task.add_done_callback(self._start_after_install_done)
                return status
            await asyncio.to_thread(self.supervisor.ensure_running)
            return await self.status()

    async def stop_runtime(self) -> EngineStatus:
        async with self._installation_lock:
            status = await self.status()
            if status.health is EngineHealth.INSTALLING:
                return status
            start_after_install_task = self._start_after_install_task
            if (
                start_after_install_task is not None
                and not start_after_install_task.done()
            ):
                start_after_install_task.cancel()
            await asyncio.to_thread(self.supervisor.disable)
            return await self.status()

    async def stop(self) -> None:
        async with self._installation_lock:
            self._installation_stopping = True
            install_task = self._install_task
            start_after_install_task = self._start_after_install_task
        if install_task is not None:
            try:
                await asyncio.shield(install_task)
            except asyncio.CancelledError:
                if not install_task.cancelled():
                    raise
            except Exception:  # noqa: BLE001
                pass
        if start_after_install_task is not None:
            try:
                await asyncio.shield(start_after_install_task)
            except asyncio.CancelledError:
                if not start_after_install_task.cancelled():
                    raise
            except Exception:  # noqa: BLE001
                pass
        await asyncio.to_thread(self.supervisor.stop)

    async def status(self) -> EngineStatus:
        raw = await asyncio.to_thread(self.supervisor.status)
        status = raw["status"]
        listening = status.get("listening") or {}
        projected = EngineStatus(
            health=EngineHealth(status["health"]),
            installed_version=status.get("installed_version"),
            verified=bool(status.get("verified")),
            listen_host="127.0.0.1",
            listen_port=listening.get("port"),
            last_check_iso=status.get("last_check"),
            host_platform=raw.get("host_platform"),
            error_key=status.get("error_key"),
        )
        if self._install_owner_active:
            return replace(
                projected,
                health=EngineHealth.INSTALLING,
                installed_version=None,
                verified=False,
                listen_port=None,
                error_key=None,
            )
        return projected

    async def gateway_token(self) -> str:
        connection = await asyncio.to_thread(self.supervisor.ensure_running)
        return connection.gateway_token

    async def sync_sources(self, bindings: Sequence[SourceBinding]) -> None:
        async with self._routing_lock:
            previous = await asyncio.to_thread(self.state_store.list_sources)
            was_running = await asyncio.to_thread(self.supervisor.client_if_running) is not None
            await asyncio.to_thread(self.state_store.sync_sources, bindings)
            try:
                await asyncio.to_thread(self.supervisor.restart_if_running)
            except Exception:
                await asyncio.to_thread(self.state_store.replace_sources, previous)
                if was_running:
                    try:
                        await asyncio.to_thread(self.supervisor.ensure_running)
                    except Exception as restore_error:
                        raise EngineStateError(
                            "source sync failed and the previous engine state could not be restored"
                        ) from restore_error
                raise

    async def provision_credential(
        self,
        vendor: str,
        protocol: str,
        secret: str,
        base_url: str | None,
    ) -> str:
        return await asyncio.to_thread(
            self.state_store.store_api_key,
            secret,
            vendor=vendor,
            protocol=protocol,
            base_url=base_url,
        )

    async def retarget_api_key_credential(
        self,
        credential_ref: str,
        vendor: str,
        protocol: str,
        base_url: str | None,
    ) -> str:
        metadata = await asyncio.to_thread(
            self.state_store.credential_metadata,
            credential_ref,
        )
        normalized_vendor = vendor.strip().lower()
        if (
            metadata.get("kind") != "api_key"
            or metadata.get("vendor") != normalized_vendor
            or metadata.get("protocol") != protocol
        ):
            raise EngineStateError("credential does not match retarget request")
        secret = await asyncio.to_thread(
            self.state_store.read_api_key,
            credential_ref,
        )
        return await asyncio.to_thread(
            self.state_store.store_api_key,
            secret,
            vendor=normalized_vendor,
            protocol=protocol,
            base_url=base_url,
        )

    async def credential_supports_refresh(self, credential_ref: str) -> bool:
        metadata = await asyncio.to_thread(
            self.state_store.credential_metadata,
            credential_ref,
        )
        return metadata.get("kind") == "oauth"

    async def provision_transient_credential(
        self,
        vendor: str,
        secret: str,
        base_url: str | None,
    ) -> str:
        """Store an unbound observation key until the observation settles.

        The observation seam determines protocol from upstream responses, so the
        temporary record deliberately uses a neutral engine-store protocol marker;
        ``observe_source`` reads only the opaque ref's secret and never treats that
        marker as a protocol conclusion.
        """

        return await asyncio.to_thread(
            self.state_store.store_api_key,
            secret,
            vendor=vendor,
            protocol="openai_chat",
            base_url=base_url,
        )

    async def revoke_credential(self, credential_ref: str) -> None:
        await asyncio.to_thread(
            self.state_store.assert_credential_unbound,
            credential_ref,
        )
        metadata = await asyncio.to_thread(
            self.state_store.credential_metadata,
            credential_ref,
        )
        auth_name = metadata.get("auth_name") if metadata["kind"] == "oauth" else None
        if auth_name:
            client = await asyncio.to_thread(self.supervisor.client_if_running)
            if client is not None:
                try:
                    await asyncio.to_thread(
                        client.management_request,
                        "DELETE",
                        "/auth-files",
                        query={"name": str(auth_name)},
                        timeout=1.0,
                    )
                except EngineClientError:
                    pass
            await asyncio.to_thread(self.state_store.delete_oauth_auth_file, str(auth_name))
            await asyncio.to_thread(self.state_store.audit_auth_permissions, enforce=True)
        await asyncio.to_thread(self.supervisor.invalidate_configs)
        await asyncio.to_thread(self.state_store.revoke_credential, credential_ref)

    async def cleanup_orphaned_oauth_material(self, credential_ref: str) -> bool:
        try:
            await asyncio.to_thread(
                self.state_store.assert_credential_unbound,
                credential_ref,
            )
            metadata = await asyncio.to_thread(
                self.state_store.credential_metadata_if_present,
                credential_ref,
            )
        except EngineStateError:
            return False
        # Revocation is ordered after both auth-file deletions. Therefore an
        # absent ref proves cleanup already converged, including never-created
        # refs whose postcondition held vacuously.
        if metadata is None:
            return True
        auth_name = metadata.get("auth_name") if metadata["kind"] == "oauth" else None
        if not isinstance(auth_name, str) or not auth_name:
            return False
        client = await asyncio.to_thread(self.supervisor.client_if_running)
        return await self._cleanup_oauth_material(
            client,
            auth_name,
            credential_ref,
        )

    async def discover_models(
        self,
        vendor: str,
        protocol: str,
        base_url: str | None,
        credential_ref: str,
    ) -> Sequence[DiscoveredModel]:
        metadata = await asyncio.to_thread(
            self.state_store.credential_metadata,
            credential_ref,
        )
        normalized_vendor = vendor.strip().lower()
        if metadata["kind"] == "oauth":
            if metadata.get("vendor") != normalized_vendor or base_url is not None:
                raise EngineStateError("credential does not match discovery target")
            client = await asyncio.to_thread(self.supervisor.client)
            payload = await asyncio.to_thread(
                client.management_request,
                "GET",
                "/auth-files/models",
                query={"name": str(metadata["auth_name"])},
            )
            return _discovered_models(payload)
        normalized_base_url = await asyncio.to_thread(
            self.state_store.validate_api_key_target,
            credential_ref,
            vendor=normalized_vendor,
            protocol=protocol,
            base_url=base_url,
        )
        secret = await asyncio.to_thread(self.state_store.read_api_key, credential_ref)
        try:
            return await probe_models(
                vendor=normalized_vendor,
                protocol=protocol,
                base_url=normalized_base_url,
                secret=secret,
            )
        except EngineClientError as exc:
            raise ModelDiscoveryError("model discovery failed") from exc

    async def observe_source(
        self,
        vendor: str,
        base_url: str | None,
        credential_ref: str,
        protocol_order: Sequence[str],
    ) -> SourceObservation:
        """Observe sequentially and stop at the first persistable proof.

        ``protocol_order`` orders attempts only. A protocol-specific response
        with accepted authentication proves the current attempt and terminates
        observation. A shipped vendor catalog pin or a concrete `custom`
        declaration also terminates observation once that exact protocol path
        is reachable and authenticated, even when the response shape itself
        stays generic. A shaped credential rejection is recorded while later
        candidates continue. Vendor, URL, and order never create a conclusion
        on their own; Auto detect still requires response-backed proof, and
        exhausting that path without one remains ambiguous.
        """

        metadata = await asyncio.to_thread(
            self.state_store.credential_metadata,
            credential_ref,
        )
        normalized_vendor = vendor.strip().lower()
        try:
            normalized_base_url = normalize_model_hub_base_url(base_url)
        except (TypeError, ValueError):
            raise EngineStateError("invalid source base URL") from None
        if metadata.get("vendor") != normalized_vendor:
            raise EngineStateError("credential does not match observation target")
        credential_kind = metadata.get("kind")
        secret: str | None = None
        oauth_auth: _AuthRecord | None = None
        if credential_kind == "api_key":
            if metadata.get("base_url") != normalized_base_url:
                raise EngineStateError("credential does not match observation target")
            secret = await asyncio.to_thread(self.state_store.read_api_key, credential_ref)
        elif credential_kind == "oauth":
            if normalized_base_url is not None:
                raise EngineStateError("credential does not match observation target")
            client = await asyncio.to_thread(self.supervisor.client)
            inventory = await asyncio.to_thread(_auth_inventory, client)
            auth_name = str(metadata.get("auth_name") or "")
            matches = [auth for auth in inventory.values() if auth.name == auth_name or auth.identity == auth_name]
            if len(matches) != 1 or not matches[0].auth_index:
                raise EngineStateError("OAuth credential binding is unavailable")
            oauth_auth = matches[0]
        else:
            raise EngineStateError("credential does not match observation target")

        failures: list[EngineClientError] = []
        received_rejection = False
        received_proven_unknown = False
        received_unproven_response = False
        received_accepted_unproven_response = False
        response_evidence_by_protocol: dict[str, _ProtocolEvidence] = {}
        ruled_out_protocols: set[str] = set()
        for protocol in protocol_order:
            if protocol not in SOURCE_PROTOCOLS:
                raise EngineStateError("unsupported source protocol")
            try:
                if credential_kind == "api_key":
                    evidence = await _probe_protocol_response(
                        vendor=normalized_vendor,
                        protocol=protocol,
                        base_url=base_url,
                        secret=secret or "",
                    )
                else:
                    assert oauth_auth is not None
                    evidence = await asyncio.to_thread(
                        _probe_oauth_protocol_response,
                        client=client,
                        auth=oauth_auth,
                        vendor=normalized_vendor,
                        protocol=protocol,
                    )
            except EngineClientError as exc:
                failures.append(exc)
                continue
            if evidence.authentication is _AuthenticationEvidence.REJECTED:
                received_rejection = True
                ruled_out_protocols.add(protocol)
                continue
            proved_protocol: str | None = None
            if evidence.protocol is _ProtocolProof.PROVEN:
                if evidence.authentication is _AuthenticationEvidence.UNKNOWN:
                    received_proven_unknown = True
                    continue
                proved_protocol = protocol
            else:
                received_unproven_response = True
                if evidence.authentication is _AuthenticationEvidence.ACCEPTED:
                    received_accepted_unproven_response = True
                    if _protocol_is_persistable_without_shape_proof(
                        credential_kind=str(credential_kind),
                        vendor=normalized_vendor,
                        protocol=protocol,
                        protocol_order=protocol_order,
                    ):
                        proved_protocol = protocol
                if _pairwise_positive_exclusion(evidence):
                    ruled_out_protocols.add(protocol)
                response_evidence_by_protocol[protocol] = evidence
                if proved_protocol is None:
                    continue
            try:
                if credential_kind == "api_key":
                    models = await probe_models(
                        vendor=normalized_vendor,
                        protocol=proved_protocol,
                        base_url=base_url,
                        secret=secret or "",
                    )
                else:
                    models = await self.discover_models(
                        normalized_vendor,
                        proved_protocol,
                        None,
                        credential_ref,
                    )
            except (EngineClientError, ModelDiscoveryError):
                discovery = ObservationDiscovery.FAILED
                models = ()
            else:
                discovery = ObservationDiscovery.SUCCEEDED
            return make_source_observation(
                outcome=ObservationOutcome.OBSERVED,
                reachable=True,
                authenticated=True,
                protocol=proved_protocol,
                discovery=discovery,
                models=tuple(models),
            )

        considered_protocols = frozenset(protocol_order)
        proved_protocol = _openai_family_elimination_proof(
            response_evidence_by_protocol,
            considered_protocols=considered_protocols,
            ruled_out_protocols=frozenset(ruled_out_protocols),
        ) or _anthropic_wrapperless_elimination_proof(
            response_evidence_by_protocol,
            considered_protocols=considered_protocols,
            ruled_out_protocols=frozenset(ruled_out_protocols),
        )
        if proved_protocol is not None:
            try:
                if credential_kind == "api_key":
                    models = await probe_models(
                        vendor=normalized_vendor,
                        protocol=proved_protocol,
                        base_url=base_url,
                        secret=secret or "",
                    )
                else:
                    models = await self.discover_models(
                        normalized_vendor,
                        proved_protocol,
                        None,
                        credential_ref,
                    )
            except (EngineClientError, ModelDiscoveryError):
                discovery = ObservationDiscovery.FAILED
                models = ()
            else:
                discovery = ObservationDiscovery.SUCCEEDED
            return make_source_observation(
                outcome=ObservationOutcome.OBSERVED,
                reachable=True,
                authenticated=True,
                protocol=proved_protocol,
                discovery=discovery,
                models=tuple(models),
            )

        if received_accepted_unproven_response:
            # A shaped request error already proved the credential was accepted
            # even when the response family never narrowed to one persisted
            # protocol.
            return make_source_observation(
                outcome=ObservationOutcome.AMBIGUOUS,
                reachable=True,
                authenticated=True,
                protocol=None,
                discovery=ObservationDiscovery.NOT_ATTEMPTED,
                models=(),
            )
        if received_rejection:
            return make_source_observation(
                outcome=ObservationOutcome.AUTHENTICATION_FAILED,
                reachable=True,
                authenticated=False,
                protocol=None,
                discovery=ObservationDiscovery.NOT_ATTEMPTED,
                models=(),
            )
        if received_proven_unknown:
            return make_source_observation(
                outcome=ObservationOutcome.ADAPTER_ERROR,
                reachable=True,
                authenticated=None,
                protocol=None,
                discovery=ObservationDiscovery.NOT_ATTEMPTED,
                models=(),
            )
        if received_unproven_response:
            return make_source_observation(
                outcome=ObservationOutcome.AMBIGUOUS,
                reachable=True,
                authenticated=None,
                protocol=None,
                discovery=ObservationDiscovery.NOT_ATTEMPTED,
                models=(),
            )
        if any(error.error_type == "timeout" for error in failures):
            return make_source_observation(
                outcome=ObservationOutcome.TIMEOUT,
                reachable=None,
                authenticated=None,
                protocol=None,
                discovery=ObservationDiscovery.NOT_ATTEMPTED,
                models=(),
            )
        if any(error.error_type in {"network_error", "ConnectionError", "URLError"} for error in failures):
            return make_source_observation(
                outcome=ObservationOutcome.UNREACHABLE,
                reachable=False,
                authenticated=None,
                protocol=None,
                discovery=ObservationDiscovery.NOT_ATTEMPTED,
                models=(),
            )
        return make_source_observation(
            outcome=ObservationOutcome.ADAPTER_ERROR,
            reachable=None,
            authenticated=None,
            protocol=None,
            discovery=ObservationDiscovery.NOT_ATTEMPTED,
            models=(),
        )

    async def start_oauth(self, source_id: str, vendor: str) -> OAuthFlowState:
        await asyncio.to_thread(self.state_store.validate_source_id, source_id)
        normalized_vendor = vendor.strip().lower()
        endpoint = _OAUTH_ENDPOINTS.get(normalized_vendor)
        if endpoint is None:
            raise EngineStateError("OAuth vendor lacks Model Hub response-backed observation")
        engine_endpoint, callback_provider, auth_provider = endpoint
        with self._oauth_lock:
            self._expire_oauth_flows_locked()
            if auth_provider in self._active_oauth_providers:
                raise EngineStateError("an OAuth flow for this provider is already active")
            self._active_oauth_providers.add(auth_provider)
        try:
            client = await asyncio.to_thread(self.supervisor.client)
            before = await asyncio.to_thread(_auth_inventory, client)
            payload = await asyncio.to_thread(
                client.management_request,
                "GET",
                engine_endpoint,
                query={"is_webui": "true"} if normalized_vendor in _WEBUI_OAUTH_VENDORS else None,
            )
            engine_state = str(payload.get("state") or "").strip()
            if not engine_state:
                raise EngineStateError("engine OAuth response omitted state")
            device_code = str(payload.get("user_code") or "").strip() or None
            flow_kind = str(payload.get("flow") or "").strip().lower()
            expects = "none" if device_code or flow_kind == "device" else "paste_callback_url"
            expires_in = max(1, int(payload.get("expires_in") or 300))
            flow = _OAuthFlow(
                flow_id=f"oaf_{secrets.token_hex(12)}",
                source_id=source_id,
                engine_state=engine_state,
                vendor=normalized_vendor,
                callback_provider=callback_provider,
                auth_provider=auth_provider,
                expects=expects,
                auth_url=str(payload.get("url") or payload.get("verification_uri") or "").strip() or None,
                device_code=device_code,
                expires_at_iso=(datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat(),
                before_auth_fingerprints={identity: record.fingerprint for identity, record in before.items()},
                grant_write_possible=expects == "none",
            )
        except Exception:
            with self._oauth_lock:
                self._active_oauth_providers.discard(auth_provider)
            raise
        with self._oauth_lock:
            self._oauth_flows[flow.flow_id] = flow
        return flow.snapshot()

    async def oauth_status(self, flow_id: str) -> OAuthFlowState:
        flow = self._get_flow(flow_id)
        async with flow.operation_lock:
            self._expire_oauth_flow(flow)
            if flow.state in {"success", "failed", "cancelled"}:
                return flow.snapshot()
            try:
                client = await asyncio.to_thread(self.supervisor.client)
                payload = await asyncio.to_thread(
                    client.management_request,
                    "GET",
                    "/get-auth-status",
                    query={"state": flow.engine_state},
                )
                status = str(payload.get("status") or "").strip().lower()
                if status == "ok":
                    await self._complete_oauth(flow, client)
                elif status == "error":
                    self._fail_flow(flow, "models.oauth.upstream_failed")
                elif flow.state != "verifying":
                    flow.state = "awaiting_action"
            except (EngineClientError, EngineUnavailableError):
                self._fail_flow(flow, "models.oauth.engine_unavailable")
            return flow.snapshot()

    async def submit_oauth(self, flow_id: str, value: str) -> OAuthFlowState:
        flow = self._get_flow(flow_id)
        async with flow.operation_lock:
            self._expire_oauth_flow(flow)
            if flow.state in {"success", "failed", "cancelled"}:
                raise EngineStateError("OAuth flow is no longer active")
            if flow.expects == "none":
                raise EngineStateError("this OAuth flow does not accept a submission")
            submitted = value.strip()
            if not submitted:
                raise EngineStateError("OAuth submission is empty")
            # A transport failure after submission begins cannot prove whether
            # the engine wrote grant material.
            flow.grant_write_possible = True
            payload: dict[str, str] = {
                "provider": flow.callback_provider,
                "state": flow.engine_state,
            }
            if submitted.startswith(("http://", "https://")):
                payload["redirect_url"] = submitted
            else:
                payload["code"] = submitted
            try:
                client = await asyncio.to_thread(self.supervisor.client)
                await asyncio.to_thread(
                    client.management_request,
                    "POST",
                    "/oauth-callback",
                    payload=payload,
                )
            except (EngineClientError, EngineUnavailableError):
                self._fail_flow(flow, "models.oauth.submission_failed")
                return flow.snapshot()
            flow.state = "verifying"
            return flow.snapshot()

    async def cancel_oauth(self, flow_id: str) -> None:
        flow = self._get_flow(flow_id)
        async with flow.operation_lock:
            self._expire_oauth_flow(flow)
            if flow.state in {"success", "failed", "cancelled"}:
                return
            try:
                client = await asyncio.to_thread(self.supervisor.client)
                await asyncio.to_thread(
                    client.management_request,
                    "DELETE",
                    "/oauth-session",
                    query={"state": flow.engine_state},
                )
            except (EngineClientError, EngineUnavailableError):
                pass
            self._mark_retention_unknown_if_needed(flow)
            flow.state = "cancelled"
            self._release_provider(flow)

    async def invoke(
        self,
        source_id: str,
        model_id: str,
        request: Mapping[str, Any],
        stream: bool,
        origin: str,
    ) -> EngineInvokeHandle:
        async with self._routing_lock:
            source = await asyncio.to_thread(self.state_store.get_source, source_id)
            if source is None:
                raise EngineStateError("source is not registered")
            if source.allowed_origins and origin not in source.allowed_origins:
                raise OriginNotAllowedError(f"origin {origin!r} is not allowed to use source {source_id!r}")
            try:
                client = await asyncio.to_thread(self.supervisor.client)
            except EngineUnavailableError:
                return completed_handle(
                    RawCallOutcome(
                        kind=RawOutcomeKind.NETWORK_ERROR,
                        http_status=None,
                        error_code="engine_down",
                        redacted_message=None,
                        stream_started=False,
                        model_id=model_id,
                        source_id=source_id,
                    )
                )
        request_protocol = {
            "claude": "anthropic",
            "codex": "openai_responses",
        }.get(origin, getattr(request, "protocol", None) or source.protocol)
        return await client.invoke(
            source,
            model_id,
            request,
            stream=stream,
            request_protocol=request_protocol,
            request_headers=getattr(request, "headers", None),
        )

    async def _complete_oauth(self, flow: _OAuthFlow, client: EngineClient) -> None:
        flow.grant_write_possible = True
        inventory = await asyncio.to_thread(_auth_inventory, client)
        provider_records = [record for record in inventory.values() if record.provider == flow.auth_provider]
        candidates = [
            record
            for record in provider_records
            if flow.before_auth_fingerprints.get(record.identity) != record.fingerprint
        ]
        if not candidates and len(provider_records) == 1:
            candidates = provider_records
        if len(candidates) != 1:
            if not candidates:
                flow.state = "verifying"
                return
            self._set_retained_material(flow, RetainedMaterialDisposition.UNKNOWN)
            self._fail_flow(flow, "models.oauth.ambiguous_engine_binding")
            return
        auth = candidates[0]
        try:
            existing_credential_ref = await asyncio.to_thread(
                self.state_store.oauth_credential_ref,
                auth.name,
            )
        except EngineStateError:
            # The grant changed but duplicate persisted metadata means no single
            # ref can be named safely.
            self._set_retained_material(flow, RetainedMaterialDisposition.UNKNOWN)
            self._fail_flow(flow, "models.oauth.binding_failed")
            return

        existing_source_id: str | None = None
        if existing_credential_ref is not None:
            try:
                existing_credential = await asyncio.to_thread(
                    self.state_store.credential_metadata,
                    existing_credential_ref,
                )
            except EngineStateError:
                self._set_retained_material(flow, RetainedMaterialDisposition.UNKNOWN)
                self._fail_flow(flow, "models.oauth.binding_failed")
                return
            value = existing_credential.get("source_id")
            existing_source_id = str(value) if value else None

        try:
            credential_ref = await asyncio.to_thread(
                self.state_store.bind_oauth_credential,
                flow.source_id,
                flow.vendor,
                auth.name,
            )
        except EngineStateError:
            if existing_credential_ref is None or existing_source_id is None:
                self._set_retained_material(flow, RetainedMaterialDisposition.UNKNOWN)
            elif existing_source_id == flow.source_id:
                self._set_retained_material(
                    flow,
                    RetainedMaterialDisposition.FLOW_SOURCE_REF,
                    existing_credential_ref,
                )
            else:
                # The existing ref belongs to another source. Withhold it so a
                # consumer cannot revoke or journal foreign material.
                self._set_retained_material(
                    flow,
                    RetainedMaterialDisposition.FOREIGN_SOURCE_REF,
                )
            self._fail_flow(flow, "models.oauth.binding_failed")
            return

        try:
            credential = await asyncio.to_thread(
                self.state_store.credential_metadata,
                credential_ref,
            )
        except EngineStateError:
            self._set_retained_material(
                flow,
                RetainedMaterialDisposition.FLOW_SOURCE_REF,
                credential_ref,
            )
            self._fail_flow(flow, "models.oauth.binding_failed")
            return
        try:
            await asyncio.to_thread(
                client.management_request,
                "PATCH",
                "/auth-files/fields",
                payload={"name": auth.name, "prefix": credential["prefix"]},
            )
            await asyncio.to_thread(self.state_store.audit_auth_permissions, enforce=True)
        except (EngineClientError, EngineStateError):
            if existing_credential_ref is not None:
                self._set_retained_material(
                    flow,
                    RetainedMaterialDisposition.FLOW_SOURCE_REF,
                    credential_ref,
                )
            else:
                # Never destroy the only cleanup handle while grant material
                # may remain behind it. Both auth-file deletions must be
                # confirmed before revocation can discard the minted ref.
                if auth.identity not in flow.before_auth_fingerprints and await self._cleanup_oauth_material(
                    client,
                    auth.name,
                    credential_ref,
                ):
                    self._set_retained_material(
                        flow,
                        RetainedMaterialDisposition.NONE,
                    )
                else:
                    self._set_retained_material(
                        flow,
                        RetainedMaterialDisposition.ORPHAN_REF,
                        credential_ref,
                    )
            self._fail_flow(flow, "models.oauth.binding_failed")
            return
        flow.credential_ref = credential_ref
        self._set_retained_material(
            flow,
            RetainedMaterialDisposition.FLOW_SOURCE_REF,
            credential_ref,
        )
        flow.state = "success"
        self._release_provider(flow)

    async def _cleanup_oauth_material(
        self,
        client: EngineClient | None,
        auth_name: str,
        credential_ref: str,
    ) -> bool:
        engine_delete_succeeded = client is None
        if client is not None:
            try:
                await asyncio.to_thread(
                    client.management_request,
                    "DELETE",
                    "/auth-files",
                    query={"name": auth_name},
                )
            except EngineClientError:
                engine_delete_succeeded = False
            else:
                engine_delete_succeeded = True

        try:
            await asyncio.to_thread(
                self.state_store.delete_oauth_auth_file,
                auth_name,
            )
        except EngineStateError:
            local_delete_succeeded = False
        else:
            local_delete_succeeded = True

        if not (engine_delete_succeeded and local_delete_succeeded):
            return False
        try:
            await asyncio.to_thread(
                self.state_store.revoke_credential,
                credential_ref,
            )
        except EngineStateError:
            return False
        return True

    def _get_flow(self, flow_id: str) -> _OAuthFlow:
        with self._oauth_lock:
            self._expire_oauth_flows_locked()
            flow = self._oauth_flows.get(flow_id)
        if flow is None:
            raise EngineStateError("OAuth flow is unknown")
        return flow

    def _fail_flow(self, flow: _OAuthFlow, error_key: str) -> None:
        self._mark_retention_unknown_if_needed(flow)
        flow.state = "failed"
        flow.error_key = error_key
        self._release_provider(flow)

    @staticmethod
    def _set_retained_material(
        flow: _OAuthFlow,
        disposition: RetainedMaterialDisposition,
        credential_ref: str | None = None,
    ) -> None:
        carries_ref = disposition in {
            RetainedMaterialDisposition.FLOW_SOURCE_REF,
            RetainedMaterialDisposition.ORPHAN_REF,
        }
        if carries_ref != (credential_ref is not None):
            raise AssertionError("retained material disposition/ref pairing is invalid")
        flow.retained_material_disposition = disposition
        flow.retained_credential_ref = credential_ref
        flow.retained_material_decided = True

    @classmethod
    def _mark_retention_unknown_if_needed(cls, flow: _OAuthFlow) -> None:
        if flow.grant_write_possible and not flow.retained_material_decided:
            cls._set_retained_material(flow, RetainedMaterialDisposition.UNKNOWN)

    def _release_provider(self, flow: _OAuthFlow) -> None:
        with self._oauth_lock:
            self._active_oauth_providers.discard(flow.auth_provider)

    def _expire_oauth_flow(self, flow: _OAuthFlow) -> None:
        with self._oauth_lock:
            self._expire_oauth_flow_locked(flow, datetime.now(timezone.utc))

    def _expire_oauth_flows_locked(self) -> None:
        now = datetime.now(timezone.utc)
        for flow in self._oauth_flows.values():
            if flow.operation_lock.locked():
                continue
            self._expire_oauth_flow_locked(flow, now)

    def _expire_oauth_flow_locked(self, flow: _OAuthFlow, now: datetime) -> None:
        if flow.state in {"success", "failed", "cancelled"}:
            return
        try:
            expires_at = datetime.fromisoformat(flow.expires_at_iso)
        except ValueError:
            expires_at = now
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            self._mark_retention_unknown_if_needed(flow)
            flow.state = "failed"
            flow.error_key = "models.oauth.expired"
            self._active_oauth_providers.discard(flow.auth_provider)


def _auth_inventory(client: EngineClient) -> dict[str, _AuthRecord]:
    payload = client.management_request("GET", "/auth-files")
    files = payload.get("files")
    if not isinstance(files, list):
        raise EngineClientError("engine auth inventory is invalid", error_type="invalid_json")
    inventory: dict[str, _AuthRecord] = {}
    for item in files:
        if not isinstance(item, dict):
            continue
        auth_index = str(item.get("auth_index") or "").strip()
        identity = str(item.get("id") or auth_index or item.get("name") or "").strip()
        name = str(item.get("name") or item.get("id") or "").strip()
        provider = str(item.get("provider") or item.get("type") or "").strip().lower()
        id_token = item.get("id_token")
        account_id = (
            str(id_token.get("chatgpt_account_id") or "").strip() or None if isinstance(id_token, dict) else None
        )
        if identity and name and provider:
            fingerprint = json.dumps(
                {
                    key: item.get(key)
                    for key in (
                        "modtime",
                        "updated_at",
                        "last_refresh",
                        "status",
                        "status_message",
                        "size",
                        "disabled",
                        "unavailable",
                    )
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            inventory[identity] = _AuthRecord(
                identity=identity,
                auth_index=auth_index,
                name=name,
                provider=provider,
                fingerprint=fingerprint,
                account_id=account_id,
            )
    return inventory


def _discovered_models(payload: Mapping[str, Any]) -> tuple[DiscoveredModel, ...]:
    models = payload.get("models")
    if not isinstance(models, list):
        return ()
    result: list[DiscoveredModel] = []
    seen: set[str] = set()
    for item in models:
        value = item.get("id") or item.get("alias") or item.get("name") if isinstance(item, dict) else item
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        supported_parameters = None
        if isinstance(item, dict) and isinstance(item.get("supported_parameters"), list):
            parameters = item["supported_parameters"]
            if all(isinstance(parameter, str) and parameter for parameter in parameters):
                supported_parameters = tuple(dict.fromkeys(parameters))
        result.append(
            DiscoveredModel(
                id=value,
                supported_parameters=supported_parameters,
            )
        )
    return tuple(result)


_adapter: CLIProxyEngineAdapter | None = None


def get_model_hub_engine_adapter() -> CLIProxyEngineAdapter:
    global _adapter
    if _adapter is None:
        _adapter = CLIProxyEngineAdapter()
    return _adapter


def set_model_hub_engine_adapter_for_tests(adapter: CLIProxyEngineAdapter | None) -> None:
    global _adapter
    _adapter = adapter
