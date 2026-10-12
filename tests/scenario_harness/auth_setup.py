import json
from pathlib import Path

from core.command_runner import SupervisedCommandResult
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
    for backend in ("claude", "codex", "opencode"):
        config.model_hub.agents[backend].mode = "direct"
    config.save()
    return config


class CliCommandObservations:
    """Auth scenarios own command responses, not a second health/readiness verdict.

    The real detection parser and HTTP projections consume these responses.
    Real native processes remain forbidden here; isolated CLI tests own that layer.
    """

    def __init__(self):
        self.responses = {}
        self.calls = []

    def replace(self, path: Path, *, stdout: str, exit_code: int = 0, stderr: str = ""):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"stdout": stdout, "exit_code": exit_code, "stderr": stderr}
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o755)
        self.responses[str(path)] = payload

    async def run(self, *, command, label, **_kwargs):
        assert command[1:] == ["--version"], "Only CLI metadata probes are scenario-controlled"
        backend = label.split()[0]
        defaults = {"claude": "2.1.34 (Claude Code)", "codex": "codex-cli 0.162.1", "opencode": "1.2.15"}
        assert backend in defaults
        path = command[0]
        self.calls.append((backend, path))
        payload = self.responses.get(path, {"stdout": defaults[backend], "exit_code": 0, "stderr": ""})
        return SupervisedCommandResult(**payload, timed_out=False)

    def bind(self, monkeypatch):
        from vibe import cli_health
        monkeypatch.setattr(cli_health, "run_supervised_command", self.run)
        return self


class AuthSetupScenarioHarness(BaseScenarioHarness):
    """Native Direct-mode setup; Hub OAuth uses its separate scenario harness."""

    def __init__(self):
        super().__init__(ScenarioControllerBase(default_backend="codex"))
        config = save_direct_auth_config()
        self.controller.config.model_hub = config.model_hub
        self.service = AgentAuthService(self.controller)

    def cli_setup_snapshot(self, client, backend: str, path: str):
        """The actual HTTP consumers used by native setup, with no UI simulation."""
        runtime = client.get(f"/api/backend/{backend}/runtime", params={"cli_path": path})
        connection = client.get(f"/api/backend/{backend}/connection")
        assert runtime.status_code == 200 and connection.status_code == 200
        return runtime.get_json(), connection.get_json()

    def flow(self, backend: str):
        return self.service._flows[f"C1:{backend}"]
