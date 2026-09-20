from config.v2_config import V2Config
from core.agent_auth_service import AgentAuthService
from tests.scenario_harness.core import BaseScenarioHarness, FakeProcess, ScenarioControllerBase


def save_direct_auth_config(config: V2Config | None = None) -> V2Config:
    """Persist the native-auth scenario precondition in its isolated test home."""
    if config is None:
        try:
            config = V2Config.load()
        except FileNotFoundError:
            config = V2Config.default()
    for agent in config.model_hub.agents.values():
        agent.mode = "direct"
    config.save()
    return config


class AuthSetupScenarioHarness(BaseScenarioHarness):
    """Native Direct-mode setup; Hub OAuth uses its separate scenario harness."""

    def __init__(self):
        super().__init__(ScenarioControllerBase(default_backend="codex"))
        config = save_direct_auth_config()
        self.controller.config.model_hub = config.model_hub
        self.service = AgentAuthService(self.controller)

    def flow(self, backend: str):
        return self.service._flows[f"C1:{backend}"]
