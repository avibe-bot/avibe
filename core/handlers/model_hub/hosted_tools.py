"""Hosted-tool facts and the Tools read (``vibey-hosted-tools.md`` §3).

Nothing here probes or guesses. A search tool is ``assumed`` on a hop when the
overlay offers it there (``runs_on``) and no current refusal exists; Vibey then
decides what each request declares. Search tools that are not native run
through the executor chain, the Sources whose vendor offers the tool, each on
its vendor's lightweight model. ``image_generate`` names its model, chosen from
the image models the user's Sources make available. A ``custom`` Source names no
vendor: each model it serves takes the priors of the vendor that claims it
(``prior_vendor``), so a compatible relay runs what that vendor's own key would.

Every function is pure over a config snapshot and a ``HostedEvidence`` reader,
so it is safe off the loop and decides the same way wherever it is asked.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Final, Literal, Mapping, Optional

from config.v2_config import (
    KEYLESS_EXA_ACTIVE,
    KEYLESS_EXA_SOURCE_ID,
    WEB_FETCH_ACTIVE,
    model_hub_builtin_sources,
    MODEL_HUB_HOSTED_TOOLS,
    MODEL_HUB_SEARCH_TOOLS,
    ModelHubConfig,
    ModelHubHostedToolConfig,
    ModelHubImageModelChoice,
    ModelHubSearchServiceSettings,
    ModelHubSourceConfig,
    hosted_tool_setting,
    is_model_hub_model_id,
    is_model_hub_source_id,
    model_hub_executor_order,
    model_hub_keyless_exa_source,
    model_hub_search_service,
)
from vibe.backend_model_catalog import (
    IMAGE_PROTOCOLS,
    HostedToolSpec,
    ImageModelSpec,
    bundled_provider_vendors,
    hosted_tool_spec,
    provider_image_models,
    provider_overlay_entry,
    source_executor_models,
    source_image_models,
)

from .capability_evidence import (
    HOSTED_TOOL_ASPECTS,
    CapabilityEvidenceStore,
    EvidenceFeature,
    EvidenceRecord,
    identity_digest,
    utc_text,
    wire_stamp,
)
from .hosted_turn import IMAGE_TOOL, ExecutorSlot, TurnHostedTools

SpecLookup = Callable[[str, str, str, str], Optional[HostedToolSpec]]
# The engine release when the engine manifest cannot be read. It names no release, so a refusal learned under it
# never matches a real one.
UNKNOWN_ENGINE_RELEASE = "unknown-engine"
NATIVE_REPLAY_PROTOCOLS: frozenset[str] = frozenset({"anthropic", "openai_responses"})

# S2b-2 activates the read and chain together; release with S6 disclosure. Not a user toggle.
SEARCH_SERVICES_ACTIVE: Final = True

Assumption = Literal["assumed", "unsupported"]


def credential_eligible(source: ModelHubSourceConfig) -> bool:
    """Whether a Source may run hosted tools at all: natively, as executor, or for images.

    Anthropic restricts OAuth credentials to Claude Code and its own apps
    (``model-hub-tos-review.md``), so a Claude subscription never is.
    """

    return not (source.vendor == "anthropic" and source.kind == "subscription")


def untranslated(source: ModelHubSourceConfig) -> bool:
    """Vibey speaks the Source's own protocol, except Chat Completions to a ``google`` Source."""

    return source.protocol != "google"


def _serves(source: ModelHubSourceConfig, model_id: str) -> bool:
    """Whether a hub Source serves this model by its admission rule.

    An API-key Source serves any model it does not list as a retired tombstone; a subscription only its listed
    models.
    """

    listed = next((model for model in source.models if model.id == model_id), None)
    return not listed.retired if listed is not None else source.kind == "api_key"


@lru_cache(maxsize=1)
def _manifest_engine_release(path: Optional[str]) -> str:
    if path:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    else:
        resource = resources.files("vibe").joinpath("model_hub_runtime/cliproxyapi_manifest.json")
        payload = json.loads(resource.read_text(encoding="utf-8"))
    for key in ("asset_release_tag", "release_tag", "version"):
        release = payload.get(key)
        if isinstance(release, str) and release:
            return release
    raise ValueError("the engine manifest names no release")


def engine_stamp() -> str:
    """The engine release evidence is stamped with: the shipped manifest's asset release.

    Engine upgrades ship as manifest changes, and a patched rebuild of the same
    upstream version gets a new asset release. Read once, when Model Hub's
    service is built; a manifest that cannot be read is the fixed unknown
    engine, never raised on a request.
    """

    try:
        return _manifest_engine_release(os.environ.get("VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH") or None)
    except (OSError, ValueError, TypeError, AttributeError):
        return UNKNOWN_ENGINE_RELEASE


def runs_on(source: ModelHubSourceConfig, model_id: str, tool: str, *, spec_for: SpecLookup) -> Optional[HostedToolSpec]:
    """The declaration a search ``tool`` runs with natively on this (Source, model), or None where it cannot.

    One rule for every place that asks: a hop's facts, an executor candidate,
    and admission. The Source is a hub Source with an eligible credential and an
    untranslated path, it serves the model, and the overlay declares the tool
    for it.
    """

    if source.supply_channel != "hub" or not credential_eligible(source) or not untranslated(source):
        return None
    if not _serves(source, model_id):
        return None
    return spec_for(source.vendor, source.protocol, model_id, tool)


@dataclass(frozen=True)
class HostedEvidence:
    """A reader over the evidence store that applies the current wire stamp. Take one per resolution."""

    store: CapabilityEvidenceStore
    engine: str
    avibe: str

    def wire_for(self, source: ModelHubSourceConfig, model_id: str) -> str:
        return wire_stamp(engine=self.engine, avibe=self.avibe, source=source, model_id=model_id)

    def refusal(self, source: ModelHubSourceConfig, model_id: str, feature: EvidenceFeature) -> Optional[EvidenceRecord]:
        return self.store.refusal(source, model_id, feature, wire=self.wire_for(source, model_id))

    def unsupported(self, source: ModelHubSourceConfig, model_id: str, feature: EvidenceFeature) -> bool:
        return self.refusal(source, model_id, feature) is not None


# A provider's dated snapshot of a model id, as `claude-haiku-4-5-20251001` is of `claude-haiku-4-5`.
_DATED_SNAPSHOT: Final = re.compile(r"-\d{8}")


@dataclass(frozen=True)
class HostedToolFacts:
    """What priors and evidence say about one search tool on one hop (§3.4).

    ``declaration`` and ``include`` are set exactly when ``native`` is ``assumed``. ``refused`` says the tool runs on
    the hop but the hop's (Source, model) refused a call, the one ``unsupported`` a proxied call falls back from.
    """

    native: Assumption
    replay: Assumption
    declaration: Optional[Mapping[str, Any]] = None
    include: tuple[str, ...] = ()
    refused: bool = False


def hosted_tool_facts(
    source: ModelHubSourceConfig, model_id: str, tool: str, *, evidence: HostedEvidence, spec_for: SpecLookup
) -> HostedToolFacts:
    spec = runs_on(source, model_id, tool, spec_for=spec_for)
    if spec is None:
        return HostedToolFacts(native="unsupported", replay="unsupported")
    if evidence.unsupported(source, model_id, EvidenceFeature.hosted(tool, "call")):
        return HostedToolFacts(native="unsupported", replay="unsupported", refused=True)
    replay: Assumption = (
        "assumed"
        if "replay" in HOSTED_TOOL_ASPECTS[tool]
        and source.protocol in NATIVE_REPLAY_PROTOCOLS
        and not evidence.unsupported(source, model_id, EvidenceFeature.hosted(tool, "replay"))
        else "unsupported"
    )
    return HostedToolFacts(native="assumed", replay=replay, declaration=spec.declaration, include=tuple(spec.include))


def enabled_tools(config: ModelHubConfig) -> tuple[str, ...]:
    """The tools a turn would declare now, in catalog order: every tool the user has not turned off."""

    return tuple(tool for tool in MODEL_HUB_HOSTED_TOOLS
                 if (tool != "web_fetch" or WEB_FETCH_ACTIVE) and hosted_tool_setting(config, tool).enabled)


def _vibey_order(config: ModelHubConfig) -> list[ModelHubSourceConfig]:
    """Every Source, Vibey's Source order first."""

    by_id = {source.id: source for source in config.sources}
    ordered = [by_id[source_id] for source_id in config.agents["vibey"].sources.order if source_id in by_id]
    return ordered + [source for source in config.sources if source not in ordered]


@dataclass(frozen=True)
class ExecutorCandidate:
    """A Source that can run a proxied search: its vendor's lightweight model and the declaration it runs with."""

    source: ModelHubSourceConfig
    model_id: str
    declaration: Mapping[str, Any]
    include: tuple[str, ...]
    price: Any = None
    kind: str = "model"
    settings: Mapping[str, Any] | None = None


def executor_candidates(
    config: ModelHubConfig, tool: str, *, evidence: HostedEvidence, spec_for: SpecLookup
) -> tuple[ExecutorCandidate, ...]:
    """The executor chain for a search tool, in effective order (§6).

    Sources whose vendor offers the tool natively on the executor model they
    list (``listed_executor_model``), with no current refusal for that pair:
    the user's ``executor_order`` first, then Vibey's Source order.
    """

    if tool == "web_fetch":
        from .fetch_readers import fetch_candidates
        return fetch_candidates(config) if WEB_FETCH_ACTIVE else ()
    if tool not in MODEL_HUB_SEARCH_TOOLS:
        return ()
    candidates: dict[str, ExecutorCandidate] = {}
    for source in _vibey_order(config):
        model_id = listed_executor_model(source, tool, spec_for=spec_for)
        candidate = executor_candidate(source, model_id, tool, evidence=evidence, spec_for=spec_for) if model_id else None
        if candidate is not None:
            candidates[source.id] = candidate
    setting = hosted_tool_setting(config, tool)
    if tool == "web_search" and SEARCH_SERVICES_ACTIVE:
        return _search_chain(config, setting, candidates)
    order = setting.executor_order or ()
    first = [candidates[source_id] for source_id in order if source_id in candidates]
    return tuple(first + [candidate for source_id, candidate in candidates.items() if source_id not in order])


def _search_chain(
    config: ModelHubConfig, setting: ModelHubHostedToolConfig, models: Mapping[str, ExecutorCandidate],
) -> tuple[ExecutorCandidate, ...]:
    """One role per Source; stale/unnamed service roles precede the first model, without a config write."""

    services: dict[str, ExecutorCandidate] = {}
    remaining = dict(models)
    disabled: list[ExecutorCandidate] = []
    ordered = _vibey_order(config)
    keyless = model_hub_keyless_exa_source() if KEYLESS_EXA_ACTIVE else None
    if keyless is not None:
        ordered.append(keyless)
    by_id = {source.id: source for source in ordered}
    first = [by_id[sid] for sid in (setting.service_order or ()) if sid in by_id and by_id[sid].protocol == "search"]
    # Tool-only search Sources keep their saved/source order first. Official
    # model Sources that auto-enroll a search API (currently DeepSeek) follow,
    # then the built-in keyless Exa, and only then model executors. Do not use
    # protocol alone here: DeepSeek is an OpenAI-compatible model Source whose
    # search service still belongs before keyless Exa in the automatic chain.
    tool_services = [
        source for source in ordered
        if source.protocol == "search" and source.id != KEYLESS_EXA_SOURCE_ID
    ]
    auto_model_services = [
        source for source in ordered
        if source.protocol != "search"
        and source.id != KEYLESS_EXA_SOURCE_ID
        and model_hub_search_service(source) is not None
    ]
    model_sources = [
        source for source in ordered
        if source.id != KEYLESS_EXA_SOURCE_ID
        and model_hub_search_service(source) is None
    ]
    sources = first + [source for source in tool_services if source not in first]
    sources += auto_model_services
    if keyless is not None:
        sources.append(keyless)
    sources += model_sources
    for source in sources:
        service = model_hub_search_service(source)
        settings = search_service_settings(setting, source)
        if service is None or settings is None:
            continue
        candidate = ExecutorCandidate(source, service["service_id"], {}, (), kind="service", settings=settings)
        if settings["enabled"]:
            services[source.id] = candidate
            remaining.pop(source.id, None)
        elif source.id not in remaining:
            disabled.append(candidate)
    automatic = {**services, **remaining}
    kinds = setting.executor_order_kinds or {}
    named = [
        automatic[sid] for sid in (setting.executor_order or ())
        if sid in automatic and automatic[sid].kind == kinds.get(sid, "model")
    ]
    named_ids = {candidate.source.id for candidate in named}
    additions = [c for sid, c in services.items() if sid not in named_ids]
    position = next((i for i, c in enumerate(named) if c.kind == "model"), len(named))
    return tuple(named[:position] + additions + named[position:]
                 + [c for sid, c in remaining.items() if sid not in named_ids] + disabled)


def listed_executor_model(source: ModelHubSourceConfig, tool: str, *, spec_for: SpecLookup) -> Optional[str]:
    """The model this Source runs proxied ``tool`` searches on: the first of its executor models it lists that runs
    the tool (§6).

    Only a listed model: an API-key Source admits unlisted models on a turn's
    hop, but an executor call nobody asked for must not guess. A dated snapshot
    of an entry counts as listing it, the latest first; a retired one does not.
    A ``custom`` relay may list several vendors' executor models, and only some
    of them run a given tool, so one that does not is passed over.
    """

    listed = {model.id for model in source.models if not model.retired}
    for entry in source_executor_models(source.vendor, source.protocol):
        snapshots = sorted(model for model in listed if _DATED_SNAPSHOT.fullmatch(model.removeprefix(entry)) and model != entry)
        model_id = entry if entry in listed else snapshots[-1] if snapshots else None
        if model_id is not None and runs_on(source, model_id, tool, spec_for=spec_for) is not None:
            return model_id
    return None


def refused_executors(
    config: ModelHubConfig, tool: str, *, evidence: HostedEvidence, spec_for: SpecLookup
) -> tuple[ModelHubSourceConfig, ...]:
    """The Sources that would run ``tool`` as executors but for a current refusal of it, in Vibey's Source order.

    What sets a call with no executor apart from one with no Source to run it: these Sources exist and their
    upstream refused the tool, so the fix is clearing that refusal, not adding a key.
    """

    refused = []
    for source in _vibey_order(config):
        model_id = listed_executor_model(source, tool, spec_for=spec_for)
        if model_id is not None and evidence.unsupported(source, model_id, EvidenceFeature.hosted(tool, "call")):
            refused.append(source)
    return tuple(refused)


def executor_candidate(
    source: ModelHubSourceConfig, model_id: str, tool: str, *, evidence: HostedEvidence, spec_for: SpecLookup
) -> Optional[ExecutorCandidate]:
    """This (Source, model) as an executor of ``tool``, or None: listed, running the tool, and not refused it.

    The one rule for building a chain and for admitting a turn's frozen slot
    again, so a slot stays good while its model is still listed and runnable,
    even after the Source lists a model its vendor prefers.
    """

    if not any(model.id == model_id and not model.retired for model in source.models):
        return None
    spec = runs_on(source, model_id, tool, spec_for=spec_for)
    if spec is None or evidence.unsupported(source, model_id, EvidenceFeature.hosted(tool, "call")):
        return None
    return ExecutorCandidate(source, model_id, spec.declaration, tuple(spec.include), spec.price)


def executor_vendors(tool: str) -> tuple[str, ...]:
    """The vendors whose Sources can run a proxied search ``tool``: one of their executor models' families declares it."""

    vendors = []
    for vendor, entry in bundled_provider_vendors().items():
        families = (provider_overlay_entry(vendor, model_id) for model_id in entry.executor_models)
        if any(family is not None and any(tool in specs for specs in family.hosted_tools.values()) for family in families):
            vendors.append(vendor)
    return tuple(sorted(vendors))


@dataclass(frozen=True)
class ImageChoice:
    """One image model ``image_generate`` may name, on the Source that serves it, under that Source's identity."""

    source_id: str
    model_id: str
    description: str
    params: tuple[str, ...]
    size_forms: tuple[str, ...]
    spec: ImageModelSpec
    identity: str = ""


@dataclass(frozen=True)
class ImageChoices:
    """The image models available now: the default first, the rest sorted by id. Empty when none is."""

    models: tuple[ImageChoice, ...]
    default: Optional[ImageChoice]
    saved_default: Optional[ModelHubImageModelChoice] = None
    saved_default_available: bool = False


def image_models_of(source: ModelHubSourceConfig) -> tuple[ImageModelSpec, ...]:
    """The image models this Source may offer by its vendor, or on a ``custom`` Source by its listing (§7.2)."""

    return source_image_models(source.vendor, source.protocol, (model.id for model in source.models if not model.retired))


def _serves_image(source: ModelHubSourceConfig, spec: ImageModelSpec) -> bool:
    """The image model's route was verified on this kind of credential, and the Source serves the model.

    Only a Source whose protocol's engine entry serves the images endpoints is offered one: the engine refuses the
    rest on its own, and that refusal is never learned, so it would be offered on every turn. For the same reason a
    model must be one of ``image_models_of`` the Source: the engine registers its image models by that same rule
    (``source_image_models``), and a model it did not register is refused locally.
    """

    return (
        source.supply_channel == "hub"
        and source.protocol in IMAGE_PROTOCOLS
        and source.kind in spec.kinds
        and credential_eligible(source)
        and _serves(source, spec.id)
        and any(offered.id == spec.id for offered in image_models_of(source))
    )


def image_choices(config: ModelHubConfig, *, evidence: HostedEvidence) -> ImageChoices:
    """The ``image_generate`` enum (§7.2): each available image model once, the default first.

    A pair with a current refusal is not available. An id two Sources offer is
    served by the first in Vibey's order, unless the saved default names the
    other. The default is the saved one if available, else the same model id on
    another Source, else the first available model in catalog order.
    """

    saved = hosted_tool_setting(config, "image_generate").default_image
    available: list[ImageChoice] = []
    for source in _vibey_order(config):
        for spec in image_models_of(source):
            if not _serves_image(source, spec) or evidence.unsupported(
                source, spec.id, EvidenceFeature.hosted("image_generate", "call")
            ):
                continue
            available.append(ImageChoice(
                source.id, spec.id, spec.description, spec.params, spec.size_forms, spec, identity_digest(source),
            ))
    if not available:
        return ImageChoices((), None, saved, False)
    saved_choice = next(
        (
            choice for choice in available
            if saved is not None and (choice.source_id, choice.model_id) == (saved.source_id, saved.model_id)
        ),
        None,
    )
    by_id: dict[str, ImageChoice] = {}
    for choice in available:
        by_id.setdefault(choice.model_id, choice)
    if saved_choice is not None:
        by_id[saved_choice.model_id] = saved_choice
    default = saved_choice or (by_id.get(saved.model_id) if saved is not None else None) or by_id[available[0].model_id]
    rest = sorted((choice for model_id, choice in by_id.items() if model_id != default.model_id), key=lambda c: c.model_id)
    return ImageChoices((default, *rest), default, saved, saved_choice is not None)


def admitted_image(config: ModelHubConfig, choice: ImageChoice, *, evidence: HostedEvidence) -> ModelHubSourceConfig | None:
    """The Source a frozen enum entry runs on, if this snapshot still admits it (§7.4): None otherwise.

    The same Source under the same identity, still serving the model on a verified credential kind, with no
    current refusal of the pair.
    """

    source = next((item for item in config.sources if item.id == choice.source_id), None)
    if (
        source is None
        or identity_digest(source) != choice.identity
        or not _serves_image(source, choice.spec)
        or evidence.unsupported(source, choice.model_id, EvidenceFeature.hosted("image_generate", "call"))
    ):
        return None
    return source


def freeze_turn_tools(config: ModelHubConfig, *, evidence: HostedEvidence, spec_for: SpecLookup) -> TurnHostedTools:
    """What a Vibey turn's first resolution freezes (§3.4). With no image model, ``image_generate`` is not declared."""

    enabled = enabled_tools(config)
    images = image_choices(config, evidence=evidence).models if IMAGE_TOOL in enabled else ()
    tools = frozenset(tool for tool in enabled if tool != IMAGE_TOOL or images)
    executors = {
        tool: tuple(
            ExecutorSlot(
                candidate.source.id, candidate.model_id, identity_digest(candidate.source),
                kind=candidate.kind,
                results=candidate.settings.get("results") if candidate.settings else None,
                enabled=candidate.settings["enabled"] if candidate.settings else True,
                label=candidate.source.display_name[:64] if SEARCH_SERVICES_ACTIVE and tool == "web_search" else "",
            )
            for candidate in executor_candidates(config, tool, evidence=evidence, spec_for=spec_for)
        )
        for tool in tools
        if tool != IMAGE_TOOL
    }
    return TurnHostedTools(
        tools, executors, images,
        prefer_native=hosted_tool_setting(config, "web_search").prefer_native if SEARCH_SERVICES_ACTIVE else None,
    )


def hop_hosted_tools(
    turn: TurnHostedTools,
    source: ModelHubSourceConfig,
    model_id: str,
    *,
    evidence: HostedEvidence,
    spec_for: SpecLookup,
) -> dict[str, dict[str, Any]]:
    """``HopResolution.hosted_tools`` for one hop of a turn (C-6 item 7).

    A search tool's entry is the hop's own facts; the image entry is the
    turn's frozen enum, with ``params`` and ``size_forms`` as sorted lists so
    the tool schema Vibey renders from them is stable across turns.
    """

    entries: dict[str, dict[str, Any]] = {}
    for tool in sorted(turn.tools - {IMAGE_TOOL}):
        facts = (
            HostedToolFacts(native="unsupported", replay="unsupported")
            if tool == "web_search" and turn.prefer_native is False
            else hosted_tool_facts(source, model_id, tool, evidence=evidence, spec_for=spec_for)
        )
        entry: dict[str, Any] = {"native": facts.native, "replay": facts.replay}
        if facts.native == "assumed":
            entry["declaration"] = dict(facts.declaration or {})
            entry["include"] = list(facts.include)
        if facts.refused:
            entry["refused"] = True
        entries[tool] = entry
    if turn.images:
        entries[IMAGE_TOOL] = {
            "models": [
                {
                    "source_id": choice.source_id,
                    "model_id": choice.model_id,
                    "description": choice.description,
                    "params": sorted(choice.params),
                    "size_forms": sorted(choice.size_forms),
                }
                for choice in turn.images
            ],
            "default": {"source_id": turn.images[0].source_id, "model_id": turn.images[0].model_id},
        }
    return entries


def search_service_settings(
    setting: ModelHubHostedToolConfig, source: ModelHubSourceConfig
) -> dict[str, Any] | None:
    """Effective per-Source settings, including catalog defaults and obsolete-value clamping."""

    service = model_hub_search_service(source)
    if service is None:
        return None
    return setting.services.get(source.id, ModelHubSearchServiceSettings()).effective(service)


def search_executor_kind(source: ModelHubSourceConfig, setting: ModelHubHostedToolConfig) -> str:
    """The role an order save records, not a chain/placement decision (S2 owns those)."""

    settings = search_service_settings(setting, source)
    if settings is None:
        return "model"
    if settings["enabled"] or not listed_executor_model(source, "web_search", spec_for=hosted_tool_spec):
        return "service"
    # Opting a model Source out of its search API preserves its existing model executor, if any.
    return "model"


# --- The Tools read (§3.3) ---------------------------------------------------------------------------------------


def _service_candidate_payload(
    source: ModelHubSourceConfig, setting: ModelHubHostedToolConfig, *, trial_pending: bool,
    cooldown_until: str | None,
) -> dict[str, Any]:
    service = model_hub_search_service(source)
    assert service is not None
    return {
        "kind": "service",
        "source_id": source.id,
        "model_id": service["service_id"],
        # A model Source may have monthly model billing; its separate search API is still priced per search.
        "billing": "metered",
        "relay": False,
        "price": {**service["price"], "extra": [dict(part) for part in service["price"]["extra"]]},
        "settings": search_service_settings(setting, source),
        "state": source.state.to_payload(),
        "trial_pending": trial_pending,
        "cooldown_until": cooldown_until,
    }


def _service_vendors(config: ModelHubConfig) -> list[dict[str, Any]]:
    from vibe.model_hub_runtime.api_key_vendors import api_key_vendor_catalog

    vendors: list[dict[str, Any]] = []
    for vendor in api_key_vendor_catalog():
        service = vendor.search
        if service is None or (vendor.built_in and not KEYLESS_EXA_ACTIVE):
            continue
        source_ids = [
            source.id for source in config.sources
            if source.vendor == vendor.id and model_hub_search_service(source) is not None
        ]
        if vendor.built_in and KEYLESS_EXA_ACTIVE:
            source_ids.append(KEYLESS_EXA_SOURCE_ID)
        vendors.append({
            "id": vendor.id,
            "name": vendor.label,
            "price": {**service["price"], "extra": [dict(part) for part in service["price"]["extra"]]},
            "free_tier": "Free; IP rate-limited" if vendor.built_in else None,
            "facts": {
                "tool_only": service["tool_only"],
                "auto_enroll": bool(service.get("auto_enroll")),
                "free_check": service["free_check"] is not None,
                **({"built_in": True, "requires_setup": False} if vendor.built_in else {}),
            },
            "results_max": service["limits"]["results_max"],
            "source_ids": source_ids,
        })
    return vendors


def _price_payload(price: Any) -> dict[str, Any] | None:
    """An overlay list price; None when no list price is known, never zero."""

    if price is None:
        return None
    return {
        "unit": price.unit,
        "usd_per_1k": price.usd_per_1k,
        "extra": [{"unit": part.unit, "usd_per_1k": part.usd_per_1k} for part in price.extra],
        "as_of": price.as_of.isoformat(),
        "url": price.url,
    }


def is_relay(source: ModelHubSourceConfig) -> bool:
    """Whether an API-key Source talks to an endpoint other than its vendor's own, whose bill Avibe cannot know."""

    from vibe.model_hub_runtime.api_key_vendors import is_official_api_key_endpoint

    if source.kind != "api_key" or not source.base_url:
        return False
    return not is_official_api_key_endpoint(source.vendor, source.base_url)


def _status(
    config: ModelHubConfig, tool: str, evidence: HostedEvidence
) -> dict[str, Any]:
    """The status lines' data: the latest success, the current refusals, and the per-pair successes."""

    live = {source.id: source for source in config.sources}
    live.update({source.id: source for source in model_hub_builtin_sources()})
    refused: list[dict[str, Any]] = []
    successes: list[tuple[datetime, str, str, Optional[str]]] = []
    for record in evidence.store.records():
        source = live.get(record.source_id)
        if (
            record.feature.kind != "hosted_tool"
            or record.feature.name != tool
            or source is None
            or record.source_identity != identity_digest(source)
        ):
            continue
        if record.status == "unsupported":
            if record.wire != evidence.wire_for(source, record.model_id):
                continue
            refused.append(
                {
                    "source_id": record.source_id,
                    "model_id": record.model_id,
                    "aspect": record.feature.aspect,
                    "at": utc_text(record.observed_at),
                    "reason": record.detail,
                }
            )
        else:
            successes.append((record.observed_at, record.source_id, record.model_id, record.mode))
    # Memory holds every success at once, so the newest identity-valid one is the latest.
    newest = max(successes, key=lambda item: item[0], default=None)
    verified = [
        {"source_id": source_id, "model_id": model_id, "mode": mode, "at": utc_text(at)}
        for at, source_id, model_id, mode in successes
    ]
    return {
        "latest_ok": (
            {"source_id": newest[1], "model_id": newest[2], "mode": newest[3], "at": utc_text(newest[0])}
            if newest is not None
            else None
        ),
        "refused": sorted(refused, key=lambda item: (item["at"], item["source_id"], item["model_id"])),
        "verified": sorted(verified, key=lambda item: (item["source_id"], item["model_id"])),
    }


def hosted_tools_status(
    config: ModelHubConfig,
    *,
    evidence: HostedEvidence,
    spec_for: SpecLookup,
    image_price: Callable[[ModelHubSourceConfig, ImageModelSpec], Optional[dict[str, Any]]],
    search_trial_pending: Callable[[str], bool] = lambda _source_id: False,
    search_cooldown_until: Callable[[str, str], str | None] = lambda _sid, _service: None,
) -> dict[str, Any]:
    """What Models → Tools reads (``hosted-tools-status.schema.json``): settings, saved and effective, and status."""

    sources = {source.id: source for source in config.sources}
    tools: list[dict[str, Any]] = []
    for tool in MODEL_HUB_HOSTED_TOOLS:
        if tool == "web_fetch":
            if WEB_FETCH_ACTIVE:
                from .fetch_readers import fetch_status
                tools.append(fetch_status(config, status=_status(config, tool, evidence), cooldown_until=search_cooldown_until))
            continue
        setting = hosted_tool_setting(config, tool)
        entry: dict[str, Any] = {"name": tool, "enabled": setting.enabled, "status": _status(config, tool, evidence)}
        if tool in MODEL_HUB_SEARCH_TOOLS:
            chain = executor_candidates(config, tool, evidence=evidence, spec_for=spec_for)
            entry["executor_order"] = list(setting.executor_order) if setting.executor_order is not None else None
            entry["effective_order"] = [candidate.source.id for candidate in chain]
            entry["executor_candidates"] = [
                {
                    "kind": "model",
                    "source_id": candidate.source.id,
                    "model_id": candidate.model_id,
                    "billing": candidate.source.billing,
                    "relay": is_relay(candidate.source),
                    "price": _price_payload(candidate.price),
                }
                if candidate.kind == "model" else _service_candidate_payload(
                    candidate.source, setting,
                    trial_pending=search_trial_pending(candidate.source.id),
                    cooldown_until=search_cooldown_until(candidate.source.id, candidate.model_id),
                )
                for candidate in chain
            ]
            if tool == "web_search":
                entry["prefer_native"] = setting.prefer_native
                entry["services"] = {
                    source_id: saved.clamped(service).to_payload()
                    for source_id, saved in setting.services.items()
                    if (
                        source_id in sources
                        and (service := model_hub_search_service(sources[source_id])) is not None
                    ) or (
                        source_id == KEYLESS_EXA_SOURCE_ID
                        and (service := model_hub_search_service(model_hub_keyless_exa_source())) is not None
                    )
                }
                entry["service_order"] = [
                    source_id for source_id in (setting.service_order or ())
                    if source_id in sources or source_id == KEYLESS_EXA_SOURCE_ID
                ]
                if SEARCH_SERVICES_ACTIVE:
                    entry["service_vendors"] = _service_vendors(config)
        else:
            choices = image_choices(config, evidence=evidence)
            saved = choices.saved_default
            entry["default_model"] = (
                {**saved.to_payload(), "available": choices.saved_default_available} if saved is not None else None
            )
            entry["effective_default"] = (
                {"source_id": choices.default.source_id, "model_id": choices.default.model_id}
                if choices.default is not None
                else None
            )
            entry["models"] = [
                {
                    "source_id": choice.source_id,
                    "model_id": choice.model_id,
                    "description": choice.description,
                    "billing": sources[choice.source_id].billing,
                    "price": image_price(sources[choice.source_id], choice.spec),
                }
                for choice in choices.models
            ]
            # A catalog fact, not the user's Sources: the vendors a key could add an image model from.
            entry["image_offered_by"] = sorted(
                vendor for vendor in bundled_provider_vendors() if provider_image_models(vendor)
            )
        tools.append(entry)
    return {"tools": tools}


class HostedToolSettingRefused(ValueError):
    """A Tools-tab write the API refuses, with its machine error and HTTP status."""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


def apply_hosted_tool_put(config: ModelHubConfig, tool: str, payload: object) -> ModelHubHostedToolConfig:
    """One ``PUT /api/models/hosted-tools/<tool>`` body applied to the saved setting, field by field.

    An absent field is unchanged; ``null`` resets ``executor_order`` to automatic
    or ``default_model`` to the default. Values are saved as given, never
    resolved: an order naming a Source that is gone, or a default that is not
    available now, stays saved and reads as such.
    """

    if tool not in MODEL_HUB_HOSTED_TOOLS:
        raise HostedToolSettingRefused("hosted_tool_not_found", 404)
    allowed = {"enabled"} | ({"executor_order"} if tool in (*MODEL_HUB_SEARCH_TOOLS, "web_fetch") else {"default_model"})
    if tool == "web_fetch":
        allowed.add("services")
    if tool == "web_search":
        allowed |= {"prefer_native", "services"}
    if not isinstance(payload, dict) or not payload or set(payload) - allowed:
        raise HostedToolSettingRefused("invalid_parameter", 400)
    changes: dict[str, Any] = {}
    try:
        if "enabled" in payload:
            # Only the saved choices reset with null; a switch is a boolean.
            if not isinstance(payload["enabled"], bool):
                raise ValueError("enabled must be a boolean")
            changes["enabled"] = payload["enabled"]
        if "prefer_native" in payload:
            if not isinstance(payload["prefer_native"], bool):
                raise ValueError("prefer_native must be a boolean")
            changes["prefer_native"] = payload["prefer_native"]
        if "services" in payload:
            if tool == "web_fetch":
                from .fetch_readers import patch_fetch_services
                changes["services"] = patch_fetch_services(config, payload["services"])
            else:
                changes["services"] = _patch_search_services(config, payload["services"])
        if "executor_order" in payload:
            order = payload["executor_order"]
            changes["executor_order"] = None if order is None else model_hub_executor_order(order, tool=tool)
        if "default_model" in payload:
            default = payload["default_model"]
            changes["default_image"] = None if default is None else ModelHubImageModelChoice.from_payload(default, tool=tool)
    except ValueError:
        raise HostedToolSettingRefused("invalid_parameter", 400) from None
    setting = replace(hosted_tool_setting(config, tool), **changes)
    if tool == "web_fetch" and "executor_order" in payload:
        from .fetch_readers import fetch_order_roles
        setting = fetch_order_roles(config, setting)
    if tool == "web_search" and "executor_order" in payload:
        live = {source.id: source for source in config.sources}
        live[KEYLESS_EXA_SOURCE_ID] = model_hub_keyless_exa_source()
        setting = replace(setting, executor_order_kinds=(
            {source_id: search_executor_kind(live[source_id], setting)
             for source_id in setting.executor_order if source_id in live}
            if setting.executor_order is not None else None
        ))
    return setting


def _patch_search_services(config: ModelHubConfig, payload: object) -> dict[str, ModelHubSearchServiceSettings]:
    if not isinstance(payload, dict):
        raise ValueError("services must be a Source-keyed patch")
    saved = dict(hosted_tool_setting(config, "web_search").services)
    live = {source.id: source for source in config.sources}
    for source_id, patch in payload.items():
        if not is_model_hub_source_id(source_id):
            raise ValueError("services keys must be Source ids")
        if patch is None:
            saved.pop(source_id, None)
            continue
        source = live.get(source_id) or (model_hub_keyless_exa_source() if source_id == KEYLESS_EXA_SOURCE_ID else None)
        service = model_hub_search_service(source) if source is not None else None
        if service is None or not isinstance(patch, dict) or set(patch) - {"results", "enabled"}:
            raise ValueError("services must name a search-capable Source and known fields")
        merged = saved.get(source_id, ModelHubSearchServiceSettings()).clamped(service).to_payload()
        for key, value in patch.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        parsed = ModelHubSearchServiceSettings.from_payload(merged)
        if parsed.results is not None and parsed.results > service["limits"]["results_max"]:
            raise ValueError("results exceeds the catalog maximum")
        if parsed.to_payload():
            saved[source_id] = parsed
        else:
            saved.pop(source_id, None)
    return saved


def clear_request(tool: str, payload: object) -> Callable[[EvidenceRecord], bool]:
    """One ``POST /api/models/hosted-tools/<tool>/clear`` body read as the refusals it clears.

    Each field is optional and, when present, a Source id or a canonical model id; ``model_id`` needs
    ``source_id``. Nothing is nullable: a malformed body is refused, never widened into a broader clear.
    """

    if tool not in MODEL_HUB_HOSTED_TOOLS:
        raise HostedToolSettingRefused("hosted_tool_not_found", 404)
    if (
        not isinstance(payload, dict)
        or set(payload) - {"source_id", "model_id"}
        or ("source_id" in payload and not is_model_hub_source_id(payload["source_id"]))
        or ("model_id" in payload and ("source_id" not in payload or not is_model_hub_model_id(payload["model_id"])))
    ):
        raise HostedToolSettingRefused("invalid_parameter", 400)
    return clear_matcher(tool, payload.get("source_id"), payload.get("model_id"))


def clear_matcher(
    tool: str, source_id: Optional[str] = None, model_id: Optional[str] = None
) -> Callable[[EvidenceRecord], bool]:
    """Which refusals one Tools clear removes: the tool's, on one Source (one model) or on every Source."""

    def matches(record: EvidenceRecord) -> bool:
        return (
            record.feature.kind == "hosted_tool"
            and record.feature.name == tool
            and (source_id is None or record.source_id == source_id)
            and (model_id is None or record.model_id == model_id)
        )

    return matches
