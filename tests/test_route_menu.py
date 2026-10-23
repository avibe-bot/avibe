"""The backend-neutral route menu every IM routing form renders (``core/modals.py``).

These rules used to be re-implemented per backend and per platform, and drifted: a stored choice past a
platform's option cap, missing from a changed catalog, or refused by Model Hub now was dropped by saving a form
unchanged on some surfaces and not others.
"""

from config.v2_settings import RoutingSettings
from core.modals import BackendRouteMenu, RouteDraft, RouteOption, RoutingModalData


def _options(*values):
    return [RouteOption(value, value.upper()) for value in values]


def _values(options):
    return [option.value for option in options]


def test_a_select_keeps_the_current_choice_within_its_cap_listed_or_not():
    menu = BackendRouteMenu("any", models=_options(*(f"m{i}" for i in range(30))), option_limit=25)

    assert _values(menu.model_options(None)) == [f"m{i}" for i in range(24)]  # Default takes the 25th slot
    past_cap = menu.model_options("m29")
    assert len(past_cap) == 24 and past_cap[-1] == RouteOption("m29", "M29")  # keeps its catalog label
    unlisted = menu.model_options("gone-model")
    assert len(unlisted) == 24 and unlisted[-1] == RouteOption("gone-model", "gone-model")
    assert _values(menu.model_options("m3")) == [f"m{i}" for i in range(24)]  # already shown: order unchanged


def test_a_select_carries_only_values_its_platform_can_hold():
    menu = BackendRouteMenu("any", models=_options("ok", "x" * 101, "__default__"), value_limit=100)

    assert _values(menu.model_options(None)) == ["ok"]
    assert _values(menu.model_options("x" * 101)) == ["ok"]


def test_efforts_follow_the_model_and_keep_a_stored_effort_the_menu_omits():
    menu = BackendRouteMenu(
        "any",
        efforts={"": _options("low", "high"), "reasoner": _options("high"), "plain": []},
        option_limit=3,
    )

    assert _values(menu.effort_options("reasoner", None)) == ["high"]
    assert _values(menu.effort_options("unknown-model", None)) == ["low", "high"]  # inherits the Default model's
    assert _values(menu.effort_options("plain", None)) == []  # a model that does not reason does not inherit
    # A model change clears the effort, so an unlisted one is the stored route's (Model Hub may refuse it now).
    assert _values(menu.effort_options("reasoner", "max")) == ["high", "max"]
    assert _values(menu.effort_options(None, "max")) == ["low", "max"]  # within the cap
    assert RouteDraft(variant="v", model="a", reasoning_effort="max").with_model("b") == RouteDraft("v", "b", None)


def test_the_stored_route_shows_only_on_its_own_backend():
    routing = RoutingSettings(agent_name="codex", model="gpt-5.4", reasoning_effort="high", claude_agent="helper")
    data = RoutingModalData(registered_backends=["claude", "codex", "vibey"], current_backend="codex", current_routing=routing)

    assert data.stored_draft("codex") == RouteDraft(None, "gpt-5.4", "high")
    # Another backend keeps its own native agent but not the stored backend's model or effort.
    assert data.stored_draft("claude") == RouteDraft("helper", None, None)
    assert data.stored_draft("vibey") == RouteDraft()
    # A route saved by an older release with backend-specific fields reads the same.
    legacy = RoutingSettings(agent_name="codex", codex_model="gpt-5.4", codex_reasoning_effort="high")
    assert RoutingModalData(["codex"], "codex", legacy).stored_draft("codex") == RouteDraft(None, "gpt-5.4", "high")
