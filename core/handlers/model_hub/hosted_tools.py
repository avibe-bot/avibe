"""Hosted-tool resolution for a Vibey hop (``vibey-hosted-tools.md`` §3.4, §3.5).

Model Hub resolves the capability layers once per launch, and Vibey only reads
the result in ``HopResolution.hosted_tools``. For every tool the user enabled
it states the primary hop's mode, the wire declaration when that mode is
native, the proxy executor bound for the turn, and whether a native payload
from this Source may be replayed to it.

Native needs all of: the toggle on, an eligible credential, an untranslated
path, the overlay's declaration for the hop's (vendor, protocol, model), and
positive ``call`` evidence taken on the current engine and probe shape. Anything
less is the lossless proxy.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from config.v2_config import MODEL_HUB_HOSTED_TOOLS, ModelHubConfig, ModelHubHostedToolConfig, ModelHubSourceConfig
from vibe.backend_model_catalog import HostedToolSpec

from .capability_evidence import HOSTED_TOOL_ASPECTS, UNKNOWN_ENGINE_STAMP, CapabilityEvidenceStore, EvidenceFeature

# The request shape a hosted-tool probe sends. Changing the probe or the native
# request it mirrors bumps this, which makes earlier probe evidence wire-stale.
HOSTED_PROBE_SHAPE = 1


SpecLookup = Callable[[str, str, str, str], Optional[HostedToolSpec]]


def credential_eligible(source: ModelHubSourceConfig) -> bool:
    """Whether a Source may run hosted tools at all: natively, as executor, or for a probe.

    Anthropic restricts OAuth credentials to Claude Code and its own apps
    (``model-hub-tos-review.md``), so a Claude subscription never is.
    """

    return not (source.vendor == "anthropic" and source.kind == "subscription")


def untranslated(source: ModelHubSourceConfig) -> bool:
    """Vibey speaks the Source's own protocol, except Chat Completions to a ``google`` Source."""

    return source.protocol != "google"


@lru_cache(maxsize=1)
def _manifest_engine_stamp(path: Optional[str]) -> str:
    if path:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    else:
        resource = resources.files("vibe").joinpath("model_hub_runtime/cliproxyapi_manifest.json")
        payload = json.loads(resource.read_text(encoding="utf-8"))
    stamp = payload.get("asset_release_tag") or payload.get("release_tag") or payload.get("version")
    if not isinstance(stamp, str) or not stamp:
        raise ValueError("the engine manifest names no release")
    return stamp


def engine_stamp() -> str:
    """The engine release evidence is stamped with: the shipped manifest's asset release.

    Engine upgrades ship as manifest changes, and a patched rebuild of the same
    upstream version gets a new asset release, so this is what a wire change
    looks like from here. Read once, when Model Hub's service is built; a
    manifest that cannot be read is the fixed unknown engine, whose evidence
    never matches a real release and never raises on a request.
    """

    try:
        return _manifest_engine_stamp(os.environ.get("VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH") or None)
    except (OSError, ValueError, TypeError, AttributeError):
        return UNKNOWN_ENGINE_STAMP


@dataclass(frozen=True)
class _Context:
    evidence: CapabilityEvidenceStore
    spec_for: SpecLookup
    engine: str
    shape: int
    now: datetime


def _verified(context: _Context, source: ModelHubSourceConfig, model_id: str, feature: EvidenceFeature) -> bool:
    view = context.evidence.view(source, model_id, feature, engine=context.engine, shape=context.shape, now=context.now)
    return view.status == "verified"


def _runnable_spec(
    context: _Context, source: ModelHubSourceConfig, model_id: str, tool: str
) -> Optional[HostedToolSpec]:
    if not credential_eligible(source) or not untranslated(source):
        return None
    spec = context.spec_for(source.vendor, source.protocol, model_id, tool)
    if spec is None or not _verified(context, source, model_id, EvidenceFeature.hosted(tool, "call")):
        return None
    return spec


def _executor(
    context: _Context, config: ModelHubConfig, tool: str, chosen: Optional[tuple[str, str]]
) -> Optional[dict[str, str]]:
    sources = {source.id: source for source in config.sources}
    candidates: list[tuple[str, str]] = []
    if chosen is not None:
        candidates.append(chosen)
    for source_id in config.agents["vibey"].sources.order:
        source = sources.get(source_id)
        if source is not None:
            candidates.extend((source_id, model.id) for model in source.models)
    for source_id, model_id in candidates:
        source = sources.get(source_id)
        if source is not None and _runnable_spec(context, source, model_id, tool) is not None:
            return {"source_id": source_id, "model_id": model_id}
    return None


def resolve_hosted_tools(
    config: ModelHubConfig,
    *,
    source: ModelHubSourceConfig,
    model_id: str,
    evidence: CapabilityEvidenceStore,
    spec_for: SpecLookup,
    engine: str,
    now: datetime,
    shape: int = HOSTED_PROBE_SHAPE,
) -> dict[str, dict[str, Any]]:
    """``HopResolution.hosted_tools`` for a Vibey hop served by ``source``/``model_id``."""

    context = _Context(evidence, spec_for, engine, shape, now)
    resolved: dict[str, dict[str, Any]] = {}
    for tool in MODEL_HUB_HOSTED_TOOLS:
        toggle = config.hosted_tools.get(tool)
        if toggle is None or not toggle.enabled:
            continue
        spec = _runnable_spec(context, source, model_id, tool)
        entry: dict[str, Any] = {"mode": "native" if spec is not None else "proxy"}
        if spec is not None:
            entry["declaration"] = spec.declaration
            entry["include"] = list(spec.include)
        chosen = (toggle.executor.source_id, toggle.executor.model_id) if toggle.executor is not None else None
        entry["executor"] = _executor(context, config, tool, chosen)
        entry["replay"] = (
            "replay" in HOSTED_TOOL_ASPECTS[tool]
            and credential_eligible(source)
            and untranslated(source)
            and _verified(context, source, model_id, EvidenceFeature.hosted(tool, "replay"))
        )
        resolved[tool] = entry
    return resolved


def _evidence_payload(view) -> dict[str, Any]:
    record = view.record
    return {
        "status": view.status,
        "stale": view.stale,
        "basis": record.basis if record is not None else None,
        "observed_at": record.payload()["observed_at"] if record is not None else None,
        "detail": record.detail if record is not None else None,
    }


def _candidate_sources(config: ModelHubConfig) -> list[ModelHubSourceConfig]:
    """Hub Sources that may run hosted tools, Vibey's Source order first."""

    by_id = {source.id: source for source in config.sources}
    ordered = [by_id[source_id] for source_id in config.agents["vibey"].sources.order if source_id in by_id]
    ordered += [source for source in config.sources if source not in ordered]
    return [
        source
        for source in ordered
        if source.supply_channel == "hub" and credential_eligible(source) and untranslated(source)
    ]


def hosted_tools_status(
    config: ModelHubConfig,
    *,
    evidence: CapabilityEvidenceStore,
    spec_for: SpecLookup,
    engine: str,
    now: datetime,
    shape: int = HOSTED_PROBE_SHAPE,
) -> dict[str, Any]:
    """What the Tools tab reads: each tool's toggle and where it can run, with its evidence.

    ``candidates`` lists every (Source, model) the overlay offers the tool on,
    on an eligible credential and an untranslated path. ``reverify`` counts
    hosted-tool probe evidence that needs a user re-verify: ``engine_upgraded``
    for evidence taken on another engine or probe shape (it routes as unknown),
    ``aged`` for evidence that is only old (it still routes).
    """

    tools = []
    for tool in MODEL_HUB_HOSTED_TOOLS:
        toggle = config.hosted_tools.get(tool)
        candidates = []
        for source in _candidate_sources(config):
            for model in source.models:
                if spec_for(source.vendor, source.protocol, model.id, tool) is None:
                    continue
                views = {
                    aspect: evidence.view(
                        source, model.id, EvidenceFeature.hosted(tool, aspect), engine=engine, shape=shape, now=now
                    )
                    for aspect in HOSTED_TOOL_ASPECTS[tool]
                }
                candidates.append(
                    {
                        "source_id": source.id,
                        "model_id": model.id,
                        "call": _evidence_payload(views["call"]),
                        "replay": _evidence_payload(views["replay"]) if "replay" in views else None,
                    }
                )
        tools.append(
            {
                "name": tool,
                "enabled": bool(toggle and toggle.enabled),
                "executor": toggle.executor.to_payload() if toggle and toggle.executor else None,
                "candidates": candidates,
            }
        )
    live = {source.id: source for source in config.sources}
    reverify = {"engine_upgraded": 0, "aged": 0}
    for record in evidence.records():
        source = live.get(record.source_id)
        if record.feature.kind != "hosted_tool" or record.basis != "probe" or source is None:
            continue
        stale = evidence.view(
            source, record.model_id, record.feature, engine=engine, shape=shape, now=now
        ).stale
        if stale == "wire":
            reverify["engine_upgraded"] += 1
        elif stale == "age":
            reverify["aged"] += 1
    return {"tools": tools, "reverify": reverify}


class HostedToolSettingRefused(ValueError):
    """A Tools-tab write the API refuses, with its machine error and HTTP status."""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


def parse_hosted_tool_setting(
    config: ModelHubConfig, tool: str, payload: object, *, spec_for: SpecLookup
) -> ModelHubHostedToolConfig:
    """Validate one ``PUT /api/models/hosted-tools/<tool>`` body against the current config.

    The executor must be a hub Source with that model, an eligible credential,
    an untranslated path, and the overlay's declaration for the tool. Whether
    it is verified is evidence, decided at every resolution, never here.
    """

    if tool not in MODEL_HUB_HOSTED_TOOLS:
        raise HostedToolSettingRefused("invalid_parameter", 400)
    try:
        setting = ModelHubHostedToolConfig.from_payload(payload, tool=tool)
    except ValueError:
        raise HostedToolSettingRefused("invalid_parameter", 400) from None
    executor = setting.executor
    if executor is None:
        return setting
    source = next((item for item in config.sources if item.id == executor.source_id), None)
    if source is None:
        raise HostedToolSettingRefused("source_not_found", 404)
    if (
        source not in _candidate_sources(config)
        or executor.model_id not in {model.id for model in source.models}
        or spec_for(source.vendor, source.protocol, executor.model_id, tool) is None
    ):
        raise HostedToolSettingRefused("invalid_parameter", 400)
    return setting
