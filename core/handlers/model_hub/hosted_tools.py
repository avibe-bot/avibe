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
from datetime import datetime, timezone
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
    for key in ("hosted_wire", "asset_release_tag", "release_tag", "version"):
        stamp = payload.get(key)
        if isinstance(stamp, str) and stamp:
            return stamp
    raise ValueError("the engine manifest names no release")


def engine_stamp() -> str:
    """The engine wire evidence is stamped with: the shipped manifest's ``hosted_wire``, else its asset release.

    ``hosted_wire`` names the oldest engine release whose hosted passthrough is
    byte-identical to this one; the engine release gate carries it forward
    across such upgrades and sets it to the new asset release otherwise, so a
    byte-identical upgrade keeps evidence current and anything else makes it
    stale. A manifest without it is stamped with its asset release (a patched
    rebuild of the same upstream version gets a new one), which is the value
    the gate first carries forward. Read once, when Model Hub's service is
    built; a manifest that cannot be read is the fixed unknown engine, whose
    evidence never matches a real release and never raises on a request.
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


def runs_on(source: ModelHubSourceConfig, model_id: str, tool: str, *, spec_for: SpecLookup) -> Optional[HostedToolSpec]:
    """The declaration ``tool`` runs with on this (Source, model), or None where it may not run at all.

    One rule for every place that asks: the Tools-tab candidates, a chosen
    executor, the executor a turn binds, a hop's native mode and a probe's
    target. The Source is a hub Source with an eligible credential and an
    untranslated path, it serves the model as its admission rule says (an
    API-key Source serves any model it does not list as a retired tombstone; a
    subscription only its listed models), and the overlay declares the tool for
    it.
    """

    if source.supply_channel != "hub" or not credential_eligible(source) or not untranslated(source):
        return None
    listed = next((model for model in source.models if model.id == model_id), None)
    if (listed.retired if listed is not None else source.kind != "api_key"):
        return None
    return spec_for(source.vendor, source.protocol, model_id, tool)


def _runnable_spec(
    context: _Context, source: ModelHubSourceConfig, model_id: str, tool: str
) -> Optional[HostedToolSpec]:
    spec = runs_on(source, model_id, tool, spec_for=context.spec_for)
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
    frozen: Mapping[str, Optional[tuple[str, str]]] | None = None,
) -> dict[str, dict[str, Any]]:
    """``HopResolution.hosted_tools`` for a Vibey hop served by ``source``/``model_id``.

    ``frozen`` is the turn's binding from its first resolution: which tools are
    on and the executor bound to each. A later resolution in the same turn
    keeps it, since settings apply from the next turn, and recomputes only what
    belongs to the hop (mode, declaration, include, replay).
    """

    context = _Context(evidence, spec_for, engine, shape, now)
    resolved: dict[str, dict[str, Any]] = {}
    for tool in MODEL_HUB_HOSTED_TOOLS:
        toggle = config.hosted_tools.get(tool)
        if frozen is not None:
            if tool not in frozen:
                continue
        elif toggle is None or not toggle.enabled:
            continue
        spec = _runnable_spec(context, source, model_id, tool)
        entry: dict[str, Any] = {"mode": "native" if spec is not None else "proxy"}
        if spec is not None:
            entry["declaration"] = spec.declaration
            entry["include"] = list(spec.include)
        if frozen is not None:
            bound = frozen[tool]
            entry["executor"] = {"source_id": bound[0], "model_id": bound[1]} if bound is not None else None
        else:
            chosen = (
                (toggle.executor.source_id, toggle.executor.model_id)
                if toggle is not None and toggle.executor is not None else None
            )
            entry["executor"] = _executor(context, config, tool, chosen)
        entry["replay"] = (
            "replay" in HOSTED_TOOL_ASPECTS[tool]
            and credential_eligible(source)
            and untranslated(source)
            and _verified(context, source, model_id, EvidenceFeature.hosted(tool, "replay"))
        )
        resolved[tool] = entry
    return resolved


def _utc_text(moment: Optional[datetime]) -> Optional[str]:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if moment is not None else None


def _evidence_payload(view) -> dict[str, Any]:
    record = view.record
    return {
        "status": view.status,
        "stale": view.stale,
        "basis": record.basis if record is not None else None,
        "observed_at": record.payload()["observed_at"] if record is not None else None,
        "detail": record.detail if record is not None else None,
        "changes_at": _utc_text(view.changes_at),
    }


def _price_payload(price: Any) -> dict[str, Any] | None:
    """The overlay's list price for the cost hint; None when no list price is known, never zero."""

    if price is None:
        return None
    return {
        "unit": price.unit,
        "usd_per_1k": price.usd_per_1k,
        "extra": [{"unit": part.unit, "usd_per_1k": part.usd_per_1k} for part in price.extra],
        "as_of": price.as_of.isoformat(),
        "url": price.url,
    }


def _candidate_targets(
    config: ModelHubConfig, tool: str, *, spec_for: SpecLookup
) -> list[tuple[ModelHubSourceConfig, str, HostedToolSpec]]:
    """The (Source, model)s the Tools tab lists for ``tool``, with its declaration on each, Vibey's Source order first.

    Every model a Source lists that ``runs_on`` admits. A passthrough model an
    API-key Source may also serve is not listed: there is nothing to show for it.
    """

    by_id = {source.id: source for source in config.sources}
    ordered = [by_id[source_id] for source_id in config.agents["vibey"].sources.order if source_id in by_id]
    ordered += [source for source in config.sources if source not in ordered]
    return [
        (source, model.id, spec)
        for source in ordered
        for model in source.models
        if (spec := runs_on(source, model.id, tool, spec_for=spec_for)) is not None
    ]


def hosted_tools_status(
    config: ModelHubConfig,
    *,
    evidence: CapabilityEvidenceStore,
    spec_for: SpecLookup,
    engine: str,
    now: datetime,
    probe_estimate: Callable[[ModelHubSourceConfig, str, str], Optional[dict[str, Any]]],
    shape: int = HOSTED_PROBE_SHAPE,
) -> dict[str, Any]:
    """What the Tools tab reads: each tool's toggle and where it can run, with its evidence.

    ``candidates`` lists every (Source, model) the overlay offers the tool on,
    on an eligible credential and an untranslated path, with what probing it
    would cost (``probe_estimate``, None when unknown). ``reverify`` counts
    hosted-tool probe evidence that needs a user re-verify: ``engine_upgraded``
    for evidence taken on another engine or probe shape (it routes as unknown),
    ``aged`` for evidence that is only old (it still routes).
    """

    tools = []
    # Every (Source, model, tool) the Tools tab lists, so a re-verify it counts is one it can offer.
    listed: set[tuple[str, str, str]] = set()
    for tool in MODEL_HUB_HOSTED_TOOLS:
        toggle = config.hosted_tools.get(tool)
        candidates = []
        for source, model_id, spec in _candidate_targets(config, tool, spec_for=spec_for):
            listed.add((source.id, model_id, tool))
            views = {
                aspect: evidence.view(
                    source, model_id, EvidenceFeature.hosted(tool, aspect), engine=engine, shape=shape, now=now
                )
                for aspect in HOSTED_TOOL_ASPECTS[tool]
            }
            candidates.append(
                {
                    "source_id": source.id,
                    "model_id": model_id,
                    "call": _evidence_payload(views["call"]),
                    "replay": _evidence_payload(views["replay"]) if "replay" in views else None,
                    "billing": source.billing,
                    "price": _price_payload(spec.price),
                    "probe_estimate": probe_estimate(source, model_id, tool),
                }
            )
        chosen = (toggle.executor.source_id, toggle.executor.model_id) if toggle and toggle.executor else None
        tools.append(
            {
                "name": tool,
                "enabled": bool(toggle and toggle.enabled),
                "executor": toggle.executor.to_payload() if toggle and toggle.executor else None,
                # What a turn would bind now, by the same rule hop resolution uses.
                "bound": _executor(_Context(evidence, spec_for, engine, shape, now), config, tool, chosen),
                "candidates": candidates,
            }
        )
    live = {source.id: source for source in config.sources}
    reverify = {"engine_upgraded": 0, "aged": 0}
    for record in evidence.records():
        source = live.get(record.source_id)
        if (
            record.feature.kind != "hosted_tool"
            or record.basis != "probe"
            or source is None
            or (record.source_id, record.model_id, record.feature.name) not in listed
        ):
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

    The executor must be a ``hosted_tool_target``. Whether it is verified is
    evidence, decided at every resolution, never here.
    """

    if tool not in MODEL_HUB_HOSTED_TOOLS:
        raise HostedToolSettingRefused("invalid_parameter", 400)
    try:
        setting = ModelHubHostedToolConfig.from_payload(payload, tool=tool)
    except ValueError:
        raise HostedToolSettingRefused("invalid_parameter", 400) from None
    executor = setting.executor
    if executor is not None:
        hosted_tool_target(config, tool, executor.source_id, executor.model_id, spec_for=spec_for)
    return setting


def hosted_tool_target(
    config: ModelHubConfig, tool: str, source_id: str, model_id: str, *, spec_for: SpecLookup
) -> tuple[ModelHubSourceConfig, HostedToolSpec]:
    """The Source a tool may run on with this model, as executor or for a probe, and its declaration there (``runs_on``)."""

    source = next((item for item in config.sources if item.id == source_id), None)
    if source is None:
        raise HostedToolSettingRefused("source_not_found", 404)
    spec = runs_on(source, model_id, tool, spec_for=spec_for)
    if spec is None:
        raise HostedToolSettingRefused("invalid_parameter", 400)
    return source, spec
