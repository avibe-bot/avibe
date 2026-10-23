"""Platform-neutral modal data models.

A channel route is the same four choices for every backend: the backend, its native agent (``variant``; only
native CLI backends have one), the model, and the reasoning effort. Backends differ only in where those lists
come from, which the settings handler resolves into one :class:`BackendRouteMenu` per backend. Each IM platform
then renders a single generic section from the menu and a :class:`RouteDraft`, so a new backend needs no
platform code.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from config.v2_settings import normalize_routing_settings
from modules.agents.catalog import NATIVE_CLI_BACKENDS
from modules.settings_manager import ChannelRouting

# The value of every select's Default option: no choice of its own, so the route inherits its Agent's.
DEFAULT_OPTION_VALUE = "__default__"
# The value of the option showing a stored choice the platform cannot carry as an option value of its own (too
# long, or a reserved value). Saving it keeps the stored choice (``SettingsHandler.handle_routing_update``).
STORED_OPTION_VALUE = "__stored__"


@dataclass(frozen=True)
class RouteOption:
    value: str
    label: str


@dataclass(frozen=True)
class RouteDraft:
    """What a routing form shows for one backend; ``None`` is the Default option."""

    variant: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None

    def with_model(self, model: Optional[str]) -> RouteDraft:
        # A model change offers that model's efforts afresh. It is also why an effort the menu does not list can
        # only be the stored route's, which ``BackendRouteMenu.effort_options`` keeps shown. Picking the shown
        # model again changes nothing.
        if model == self.model:
            return self
        return replace(self, model=model, reasoning_effort=None)


@dataclass
class BackendRouteMenu:
    """What one backend offers a channel route, sized for the platform that renders it.

    ``efforts`` maps a model to its efforts (the Default option excluded); ``""`` holds those of a route without
    its own model, and a model with no entry falls back to them. ``option_limit`` caps one select's options, the
    Default option included; ``value_limit`` is the longest option value the platform carries. ``stored`` is the
    stored route as this backend shows it, which names the choice a ``STORED_OPTION_VALUE`` option stands for.
    """

    backend: str
    variants: list[RouteOption] = field(default_factory=list)
    models: list[RouteOption] = field(default_factory=list)
    efforts: dict[str, list[RouteOption]] = field(default_factory=dict)
    option_limit: Optional[int] = None
    value_limit: Optional[int] = None
    stored: RouteDraft = field(default_factory=RouteDraft)

    def variant_options(self, current: Optional[str]) -> list[RouteOption]:
        return self._bounded(self.variants, current, self.stored.variant)

    def model_options(self, current: Optional[str]) -> list[RouteOption]:
        return self._bounded(self.models, current, self.stored.model)

    def effort_options(self, model: Optional[str], current: Optional[str]) -> list[RouteOption]:
        model = self.stored.model if model == STORED_OPTION_VALUE else model
        listed = self.efforts.get(model or "")
        return self._bounded(
            self.efforts.get("", []) if listed is None else listed, current, self.stored.reasoning_effort
        )

    def fits(self, value: object) -> bool:
        """Whether a select on this platform can carry ``value`` as an option of its own."""
        return (
            isinstance(value, str)
            and bool(value)
            and value not in (DEFAULT_OPTION_VALUE, STORED_OPTION_VALUE)
            and (self.value_limit is None or len(value) <= self.value_limit)
        )

    def chosen(self, field: str, value: Optional[str]) -> Optional[str]:
        """The choice a selected option stands for: the stored ``field`` for the stand-in option."""
        return getattr(self.stored, field) if value == STORED_OPTION_VALUE else value

    def shown_value(self, current: Optional[str]) -> str:
        """The option value a select shows for ``current``."""
        if not current:
            return DEFAULT_OPTION_VALUE
        return current if self.fits(current) or current == STORED_OPTION_VALUE else STORED_OPTION_VALUE

    def _bounded(self, options: list[RouteOption], current: Optional[str], stored: Optional[str]) -> list[RouteOption]:
        """``options`` within one select's cap, keeping ``current`` shown, listed or not.

        Saving a form unchanged then never drops a stored choice: one the list omits (a catalog that changed, an
        effort Model Hub now refuses), one past the cap, or one the platform cannot carry stays selectable.
        """
        room = None if self.option_limit is None else max(self.option_limit - 1, 0)
        kept = [option for option in options if self.fits(option.value)]
        shown = self.shown_value(current)
        if shown != DEFAULT_OPTION_VALUE and all(option.value != shown for option in kept[:room]):
            named = stored if current == STORED_OPTION_VALUE else current
            label = next((option.label for option in options if option.value == named), str(named or current))
            kept = [option for option in kept if option.value != shown]
            if room is not None:
                kept = kept[: max(room - 1, 0)]
            kept.append(RouteOption(shown, label))
        return kept if room is None else kept[:room]


@dataclass
class RoutingModalData:
    """Shared routing modal payload consumed by platform renderers."""

    registered_backends: list[str]
    current_backend: str
    current_routing: ChannelRouting | None
    menus: dict[str, BackendRouteMenu] = field(default_factory=dict)

    def menu(self, backend: str) -> BackendRouteMenu:
        return self.menus.get(backend) or BackendRouteMenu(backend, stored=self.stored_draft(backend))

    def stored_draft(self, backend: str) -> RouteDraft:
        """The stored route as one backend's form shows it before any change.

        The stored model and effort belong to the stored backend's Agent only; another backend starts at Default.
        """
        routing = self.current_routing
        variant = getattr(routing, f"{backend}_agent", None) if routing and backend in NATIVE_CLI_BACKENDS else None
        if routing is None or backend != self.current_backend:
            return RouteDraft(variant=variant)
        normalized = normalize_routing_settings(routing)
        return RouteDraft(variant=variant, model=normalized.model, reasoning_effort=normalized.reasoning_effort)


@dataclass
class RoutingModalSelection:
    """A routing form's state as an update reports it.

    ``draft`` is the shown backend's choices; ``None`` when the form had no section for ``selected_backend`` (a
    backend switch), which then starts from the stored route instead of blanks that saving would persist.
    """

    selected_backend: str
    draft: Optional[RouteDraft] = None


def route_option_label(translate: Callable[[str], str], option: RouteOption, *, effort: bool = False) -> str:
    """An option's text: Default and effort tiers are translated, a model or agent keeps its catalog label."""
    if option.value == DEFAULT_OPTION_VALUE:
        return translate("common.default")
    if effort:
        key = f"reasoning.{option.value}"
        translated = translate(key)
        if translated != key:
            return translated
    return option.label
