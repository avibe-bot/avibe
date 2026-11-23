"""Which stored Session values move off a retired model or effort (``core/session_route_succession.py``).

The move must mirror what Model Hub refuses (``turn.no_candidate.unlisted``): moving a model Model Hub still
serves would silently change a working Session, and missing one it refuses leaves the Session stopped.
"""

from __future__ import annotations

import pytest

from config.v2_config import ModelHubBackendModelConfig, ModelHubConfig, ModelHubRouteConfig
from core.session_route_succession import RouteSuccession, retired_route_succession

EFFORTS = ["low", "medium", "high"]


def _hub(**agent_fields):
    hub = ModelHubConfig()
    agent = hub.agents["vibey"]
    agent.models = [ModelHubBackendModelConfig(id="new", reasoning_efforts=EFFORTS)]
    for name, value in agent_fields.items():
        setattr(agent, name, value)
    return hub


@pytest.mark.parametrize(
    ("hub", "stored", "agent", "expected"),
    [
        pytest.param(_hub(), ("old", None), ("new", None), RouteSuccession(model=("old", "new")), id="unlisted model"),
        pytest.param(_hub(), ("old", None), ("other", None), None, id="agent default unlisted too"),
        pytest.param(_hub(), ("old", None), (None, None), None, id="no agent default"),
        pytest.param(_hub(mode="direct"), ("old", None), ("new", None), None, id="direct backend"),
        pytest.param(_hub(menu_kind="open"), ("old", None), ("new", None), None, id="open menu"),
        pytest.param(
            _hub(routes={"old": ModelHubRouteConfig.from_payload({"hops": [{"source_id": "src_abcdef12", "model_id": "x"}]})}),
            ("old", None), ("new", None), None, id="manual route serves it",
        ),
        pytest.param(
            _hub(), ("new", "max"), ("new", "medium"), RouteSuccession(reasoning_effort=("max", "medium")),
            id="effort to agent default",
        ),
        pytest.param(
            _hub(), ("new", "max"), ("new", None), RouteSuccession(reasoning_effort=("max", "high")),
            id="effort to nearest below",
        ),
        pytest.param(
            _hub(), ("new", "minimal"), ("new", None), RouteSuccession(reasoning_effort=("minimal", "low")),
            id="effort to nearest above",
        ),
        pytest.param(
            _hub(), ("old", "max"), ("new", None),
            RouteSuccession(model=("old", "new"), reasoning_effort=("max", "high")), id="both move",
        ),
        pytest.param(_hub(), ("new", "low"), ("new", None), None, id="offered effort stays"),
    ],
)
def test_retired_route_succession(hub, stored, agent, expected):
    assert retired_route_succession(
        hub,
        "vibey",
        stored_model=stored[0],
        stored_reasoning_effort=stored[1],
        agent_model=agent[0],
        agent_reasoning_effort=agent[1],
    ) == expected
