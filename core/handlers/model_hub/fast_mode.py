"""Fast mode: a Session's opt-in to a vendor's faster, pricier processing tier.

docs/plans/fast-mode.md. A Session that turns fast mode on asks every Model
Hub attempt of its turns to run at the vendor's fast tier where that attempt's
exact (Source, model) is known to offer one, and at the standard tier anywhere
else. The request field that selects the tier is read from the models.dev entry
of the model's first-party vendor (``experimental.modes.fast``), which also
covers new models without a release:

- OpenAI (GPT, o-series): ``service_tier: "priority"`` on Responses and Chat
  Completions requests.
- Anthropic (Claude Opus): ``speed: "fast"`` on Messages requests. The engine
  adds the beta header a model needs.

The field is sent on every Hub Source that speaks the vendor's own protocol: an
API key, on the vendor's endpoint or a relay, or a subscription the Model Hub
holds. Whether it took effect is the response's to say, not ours to assume: the
vendor reports the tier it served (``usage.speed``, ``service_tier``), and a
relay may drop the field or serve a standard tier. ``FastTierObservations``
keeps what the last fast request on each Source and model reported, so the
picker can say where fast mode was not confirmed. A request is never translated
across protocols to reach a fast tier. Reading the catalog never fetches: an
absent or unreadable copy offers fast mode nowhere.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Optional

from vibe.backend_model_catalog import catalog_model_id

logger = logging.getLogger(__name__)

FAST_SPEED: Final = "fast"

# The vendors whose fast tier Avibe drives, the request protocols each one's
# field belongs to, and the one body field each may set. A catalog entry that
# asks for anything else is not applied.
_VENDOR_PROTOCOLS: Final[Mapping[str, frozenset[str]]] = {
    "openai": frozenset({"openai_responses", "openai_chat"}),
    "anthropic": frozenset({"anthropic"}),
}
_VENDOR_FIELDS: Final[Mapping[str, str]] = {"openai": "service_tier", "anthropic": "speed"}


def normalized_speed(value: Any) -> Optional[str]:
    """The stored speed a value names: ``"fast"``, or None for the standard tier."""

    return FAST_SPEED if value == FAST_SPEED else None


@dataclass(frozen=True)
class FastModeCatalog:
    """Each vendor's fast-tier request field by model, and which vendor makes each model family."""

    bodies: Mapping[str, Mapping[str, Mapping[str, str]]] = field(default_factory=dict)
    families: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_models_dev(cls, catalog: Mapping[str, Any], vendor_map: Mapping[str, Any]) -> "FastModeCatalog":
        families = tuple(
            (item["prefix"], item["vendor_id"])
            for item in (vendor_map.get("families") if isinstance(vendor_map.get("families"), list) else ())
            if isinstance(item, Mapping)
            and isinstance(item.get("prefix"), str)
            and item.get("vendor_id") in _VENDOR_FIELDS
        )
        bodies: dict[str, dict[str, Mapping[str, str]]] = {}
        for vendor, field_name in _VENDOR_FIELDS.items():
            provider = catalog.get(vendor)
            models = provider.get("models") if isinstance(provider, Mapping) else None
            if not isinstance(models, Mapping):
                continue
            for key, entry in models.items():
                body = _fast_body(entry, field_name)
                if body is None:
                    continue
                names = {str(key)}
                if isinstance(entry, Mapping) and isinstance(entry.get("id"), str):
                    names.add(entry["id"])
                for name in names:
                    bodies.setdefault(vendor, {})[name] = body
        return cls(bodies=bodies, families=families)

    def first_party(self, model_id: str) -> Optional[str]:
        matches = [(prefix, vendor) for prefix, vendor in self.families if model_id.startswith(prefix)]
        return max(matches, key=lambda item: len(item[0]))[1] if matches else None

    def body_for(self, source: Any, model_id: str, request_protocol: Optional[str]) -> Optional[Mapping[str, str]]:
        """The body fields that ask for this exact attempt's fast tier, or None where none applies.

        The model's first-party vendor decides the field, and the Source must speak
        that vendor's protocol, unchanged from the request's. Where the Source
        points (official endpoint or relay) does not matter here: the response
        says whether the tier was served.
        """

        if not model_id or getattr(source, "supply_channel", None) != "hub":
            return None
        if getattr(source, "kind", None) not in ("api_key", "subscription"):
            return None
        protocol = getattr(source, "protocol", None)
        if request_protocol != protocol:
            return None
        for candidate in dict.fromkeys((model_id, catalog_model_id(model_id))):
            vendor = self.first_party(candidate)
            if vendor is None or protocol not in _VENDOR_PROTOCOLS[vendor]:
                continue
            body = (self.bodies.get(vendor) or {}).get(candidate)
            if body is not None:
                return body
        return None


def _fast_body(entry: Any, field_name: str) -> Optional[Mapping[str, str]]:
    modes = entry.get("experimental") if isinstance(entry, Mapping) else None
    modes = modes.get("modes") if isinstance(modes, Mapping) else None
    fast = modes.get(FAST_SPEED) if isinstance(modes, Mapping) else None
    provider = fast.get("provider") if isinstance(fast, Mapping) else None
    body = provider.get("body") if isinstance(provider, Mapping) else None
    if not isinstance(body, Mapping) or set(body) != {field_name}:
        return None
    value = body[field_name]
    return {field_name: value} if isinstance(value, str) and value.strip() else None


class FastTierObservations:
    """Whether the last fast request on each Source and model was served fast, as the response reported.

    True when the response named the vendor's fast tier; False when it named
    another tier or none, which is how a relay that drops the field answers.
    Absent until a fast request on that hop is served. Only the latest answer
    counts, so a Source that starts honouring the field is confirmed by its next
    fast request. Process memory, bounded; a restart forgets it.
    """

    _LIMIT: Final = 512

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._confirmed: OrderedDict[tuple[str, str], bool] = OrderedDict()

    def observe(self, source_id: str, model_id: str, confirmed: bool) -> None:
        key = (source_id, model_id)
        with self._lock:
            self._confirmed.pop(key, None)
            self._confirmed[key] = confirmed
            while len(self._confirmed) > self._LIMIT:
                self._confirmed.popitem(last=False)

    def confirmed(self, source_id: str, model_id: str) -> Optional[bool]:
        with self._lock:
            return self._confirmed.get((source_id, model_id))


_LOCK = threading.Lock()
_CACHED: tuple[Optional[tuple[str, int, int]], FastModeCatalog] = (None, FastModeCatalog())


def load_fast_mode_catalog() -> FastModeCatalog:
    """The fast-mode index of the cached models.dev copy, rebuilt only when that copy changes.

    Reads files: call it off the controller loop. Never fetches.
    """

    global _CACHED
    from vibe import models_dev_catalog

    stamp = models_dev_catalog.cached_models_dev_stamp()
    with _LOCK:
        if stamp is not None and _CACHED[0] == stamp:
            return _CACHED[1]
        try:
            catalog = models_dev_catalog.cached_models_dev_catalog() if stamp is not None else {}
            vendor_map = models_dev_catalog.load_model_vendor_map()
            built = FastModeCatalog.from_models_dev(catalog, vendor_map)
        except Exception as exc:  # noqa: BLE001 - optional metadata never fails a request
            logger.info("Model Hub has no fast-mode catalog: %s", type(exc).__name__)
            built = FastModeCatalog()
        _CACHED = (stamp, built)
        return built
