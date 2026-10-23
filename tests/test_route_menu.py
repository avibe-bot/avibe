"""The backend-neutral route menu every IM routing form renders (``core/modals.py``).

These rules used to be re-implemented per backend and per platform, and drifted: a stored choice past a
platform's option cap, missing from a changed catalog, or refused by Model Hub now was dropped by saving a form
unchanged on some surfaces and not others.
"""

from config.v2_settings import RoutingSettings
from core.modals import (
    STORED_OPTION_VALUE,
    AgentChoice,
    BackendRouteMenu,
    RouteDraft,
    RouteOption,
    RoutingModalData,
    route_option_label,
)


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


def test_a_select_carries_only_values_its_platform_can_hold_and_stands_in_for_a_stored_one():
    long_id = "x" * 101
    menu = BackendRouteMenu(
        "any", models=_options("ok", long_id, "__default__"), value_limit=100, stored=RouteDraft(model=long_id)
    )

    assert _values(menu.model_options(None)) == ["ok"]
    # The stored choice stays shown through a stand-in option, named after it; saving that keeps it.
    for current in (long_id, STORED_OPTION_VALUE):
        assert menu.model_options(current) == [RouteOption("ok", "OK"), RouteOption(STORED_OPTION_VALUE, long_id.upper())]
        assert menu.shown_value(current) == STORED_OPTION_VALUE
    assert (menu.shown_value(None), menu.shown_value("ok")) == ("__default__", "ok")


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
    draft = RouteDraft(variant="v", model="a", reasoning_effort="max")
    assert draft.with_model("b") == RouteDraft("v", "b", None)
    assert draft.with_model("a") == draft  # picking the shown model again keeps its effort


def _agents():
    return [
        AgentChoice("a-codex", "codex", "codex", model="gpt-5.4", reasoning_effort="medium"),
        AgentChoice("a-reviewer", "reviewer", "codex", model="gpt-5.5"),
        AgentChoice("a-claude", "claude", "claude"),
        AgentChoice("a-vibey", "vibey", "vibey"),
    ]


def test_the_stored_route_shows_only_on_its_own_agent():
    routing = RoutingSettings(
        agent_name="codex", model="gpt-5.6", reasoning_effort="high", codex_agent="builder", claude_agent="helper"
    )
    data = RoutingModalData(agents=_agents(), current_agent="a-codex", current_routing=routing)

    assert data.stored_draft("a-codex") == RouteDraft("builder", "gpt-5.6", "high")
    # Another Agent starts at Default, its own; a backend's native agent is stored per backend.
    assert data.stored_draft("a-reviewer") == RouteDraft("builder", None, None)
    assert data.stored_draft("a-claude") == RouteDraft("helper", None, None)
    assert data.stored_draft("a-vibey") == RouteDraft()
    # A route saved by an older release with backend-specific fields reads the same.
    legacy = RoutingSettings(agent_name="codex", codex_model="gpt-5.6", codex_reasoning_effort="high")
    assert RoutingModalData(_agents(), "a-codex", legacy).stored_draft("a-codex") == RouteDraft(None, "gpt-5.6", "high")


def test_default_options_inherit_the_chosen_agents_own_model_and_effort():
    menu = BackendRouteMenu(
        "codex",
        models=[RouteOption("gpt-5.4", "GPT-5.4"), RouteOption("gpt-5.5", "GPT-5.5")],
        efforts={"gpt-5.4": _options("low", "medium"), "gpt-5.5": _options("high")},
    )
    data = RoutingModalData(agents=_agents(), current_agent="a-codex", current_routing=None, menus={"codex": menu})
    translate = {"common.default": "Default", "reasoning.medium": "Medium"}.get

    codex, reviewer = data.menu("a-codex"), data.menu("a-reviewer")
    # Each Agent on one backend shares its lists, but Default means that Agent's own choice.
    assert route_option_label(translate, codex.default_option("model")) == "Default · GPT-5.4"
    assert route_option_label(translate, codex.default_option("reasoning_effort"), effort=True) == "Default · Medium"
    assert route_option_label(translate, reviewer.default_option("model")) == "Default · GPT-5.5"
    assert reviewer.default_option("reasoning_effort").label == ""
    assert _values(codex.effort_options(None, None)) == ["low", "medium"]
    assert _values(reviewer.effort_options(None, None)) == ["high"]


def test_the_agent_select_keeps_the_scopes_agent_within_its_cap():
    agents = [AgentChoice(f"a{i}", f"agent-{i:02}", "vibey") for i in range(30)]
    data = RoutingModalData(agents=agents, current_agent="a29", current_routing=None, agent_option_limit=25)

    shown = data.agent_options()
    assert len(shown) == 25 and shown[-1].id == "a29"
    assert data.shown_agent("missing") == "a29" and data.shown_agent("a3") == "a3"
