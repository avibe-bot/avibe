"""Platform-neutral modal data models.

A channel route names an Agent, as the Web route picker does, and may override its model and reasoning effort;
on a native CLI backend it may also pick that backend's own agent (``variant``). Backends differ only in where
the model and effort lists come from, which the settings handler resolves into one :class:`BackendRouteMenu`
per backend. Each IM platform renders an Agent select and one generic section from the chosen Agent's menu and a
:class:`RouteDraft`, so a new backend or Agent needs no platform code.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from config.v2_settings import normalize_routing_settings
from modules.agents.catalog import NATIVE_CLI_BACKENDS, display_name_for_backend
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
class AgentChoice:
    """An Agent a channel route can name, as the routing form lists it."""

    id: str
    name: str
    backend: str
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.name} · {display_name_for_backend(self.backend)}"


@dataclass(frozen=True)
class RouteDraft:
    """What a routing form shows for one Agent; ``None`` is the Default option."""

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
    # The Agent's own model and effort: what a Default option inherits.
    inherited: RouteDraft = field(default_factory=RouteDraft)

    def variant_options(self, current: Optional[str]) -> list[RouteOption]:
        return self._bounded(self.variants, current, self.stored.variant)

    def model_options(self, current: Optional[str]) -> list[RouteOption]:
        return self._bounded(self.models, current, self.stored.model)

    def effort_options(self, model: Optional[str], current: Optional[str]) -> list[RouteOption]:
        model = self.stored.model if model == STORED_OPTION_VALUE else model
        # A route without its own model runs the Agent's.
        listed = self.efforts.get(model or self.inherited.model or "")
        return self._bounded(
            self.efforts.get("", []) if listed is None else listed, current, self.stored.reasoning_effort
        )

    def default_option(self, field: str) -> RouteOption:
        """A select's Default option, named after what it inherits from the Agent where that is known."""
        inherited = getattr(self.inherited, field, None) if field in ("model", "reasoning_effort") else None
        if not inherited:
            return RouteOption(DEFAULT_OPTION_VALUE, "")
        label = next((option.label for option in self.models if option.value == inherited), inherited)
        return RouteOption(DEFAULT_OPTION_VALUE, label if field == "model" else inherited)

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
    """Shared routing modal payload consumed by platform renderers.

    ``agents`` are the Agents a route can name, ``current_agent`` the id of the one the scope runs now, and
    ``menus`` each listed backend's menu, sized for the platform. ``agent_option_limit`` caps the Agent select.
    """

    agents: list[AgentChoice]
    current_agent: str
    current_routing: ChannelRouting | None
    menus: dict[str, BackendRouteMenu] = field(default_factory=dict)
    agent_option_limit: Optional[int] = None

    def agent(self, agent_id: Optional[str]) -> Optional[AgentChoice]:
        return next((agent for agent in self.agents if agent.id == agent_id), None)

    def shown_agent(self, agent_id: Optional[str]) -> Optional[str]:
        """The Agent a form shows for ``agent_id``: that one if listed, else the scope's, else the first."""
        for candidate in (agent_id, self.current_agent):
            if self.agent(candidate) is not None:
                return candidate
        return self.agents[0].id if self.agents else None

    def agent_options(self) -> list[AgentChoice]:
        """The Agent select's options within its cap, keeping the scope's Agent shown."""
        limit = self.agent_option_limit
        if limit is None or len(self.agents) <= limit:
            return list(self.agents)
        kept = self.agents[:limit]
        current = self.agent(self.current_agent)
        if current is not None and current not in kept:
            kept = [*self.agents[: limit - 1], current]
        return kept

    def menu(self, agent_id: str) -> BackendRouteMenu:
        """The chosen Agent's menu: its backend's lists, its stored choices, and what Default inherits from it."""
        agent = self.agent(agent_id)
        backend = agent.backend if agent else ""
        base = self.menus.get(backend) or BackendRouteMenu(backend)
        return replace(
            base,
            stored=self.stored_draft(agent_id),
            inherited=RouteDraft(model=agent.model, reasoning_effort=agent.reasoning_effort) if agent else RouteDraft(),
        )

    def stored_draft(self, agent_id: str) -> RouteDraft:
        """The stored route as one Agent's form shows it before any change.

        The stored model and effort belong to the scope's Agent only; another Agent starts at Default, its own. A
        backend's native agent is stored per backend, so every Agent on that backend shows it.
        """
        routing = self.current_routing
        agent = self.agent(agent_id)
        backend = agent.backend if agent else ""
        variant = getattr(routing, f"{backend}_agent", None) if routing and backend in NATIVE_CLI_BACKENDS else None
        if routing is None or agent_id != self.current_agent:
            return RouteDraft(variant=variant)
        normalized = normalize_routing_settings(routing)
        return RouteDraft(variant=variant, model=normalized.model, reasoning_effort=normalized.reasoning_effort)


@dataclass
class RoutingModalSelection:
    """A routing form's state as an update reports it.

    ``draft`` is the shown Agent's choices; ``None`` when the form had no section for ``selected_agent`` (an Agent
    switch), which then starts from the stored route instead of blanks that saving would persist.
    """

    selected_agent: str
    draft: Optional[RouteDraft] = None


def _effort_text(translate: Callable[[str], str], effort: str, fallback: str) -> str:
    key = f"reasoning.{effort}"
    translated = translate(key)
    return translated if translated != key else fallback


def route_option_label(translate: Callable[[str], str], option: RouteOption, *, effort: bool = False) -> str:
    """An option's text: Default and effort tiers are translated, a model or agent keeps its catalog label.

    A Default option that names what it inherits reads ``Default · <that>``.
    """
    if option.value == DEFAULT_OPTION_VALUE:
        default = translate("common.default")
        if not option.label:
            return default
        inherited = _effort_text(translate, option.label, option.label) if effort else option.label
        return f"{default} · {inherited}"
    if effort:
        return _effort_text(translate, option.value, option.label)
    return option.label
