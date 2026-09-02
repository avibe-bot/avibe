"""Model Hub feature gate and managed-runtime E2E scenarios."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from tests.e2e.drivers.model_hub_app import ModelHubTestApp


pytestmark = pytest.mark.e2e_model_hub


def _local_engine_manifest() -> str:
    raw = os.environ.get("VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH", "").strip()
    if not raw:
        pytest.skip(
            "managed Model Hub engine unavailable: set "
            "VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH to an offline manifest"
        )
    path = Path(raw).expanduser()
    if not path.is_file():
        pytest.skip(
            f"managed Model Hub engine manifest does not exist: {path}"
        )
    return str(path.resolve())


def _engine_app(model_hub_app_factory):
    manifest = _local_engine_manifest()
    return model_hub_app_factory(
        extra_env={
            "VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH": manifest,
            "VIBE_MODEL_HUB_ENGINE_OFFLINE": "1",
        }
    )


def _install_engine(app) -> list[str]:
    observed: list[str] = []
    response = app.client.post("/api/models/runtime/install", {})
    body = response.json()
    assert response.status == 200, body
    observed.append(body["runtime"]["status"]["health"])
    deadline = time.monotonic() + 60
    latest = body["runtime"]
    while time.monotonic() < deadline:
        status_response = app.client.get("/api/models/runtime/status")
        assert status_response.status == 200, status_response.json()
        latest = status_response.json()["runtime"]
        health = latest["status"]["health"]
        observed.append(health)
        if health != "installing":
            break
        time.sleep(0.1)
    assert latest["status"]["health"] == "not_started", latest
    assert latest["status"]["verified"] is True
    return observed


def test_harness_scrubs_inherited_backend_credentials(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Harness contract: subprocesses inherit no user backend credentials."""

    inherited = {
        "ANTHROPIC_API_KEY": "real-user-anthropic-key",
        "ANTHROPIC_BASE_URL": "https://real-user-anthropic.example",
        "CLAUDE_CODE_OAUTH_TOKEN": "real-user-claude-token",
        "OPENAI_API_KEY": "real-user-openai-key",
        "OPENAI_BASE_URL": "https://real-user-openai.example",
        "OPENCODE_CONFIG_CONTENT": '{"real":"user"}',
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)

    app = ModelHubTestApp(Path.cwd(), tmp_path)

    assert set(inherited).isdisjoint(app.env)
    assert app.env["HOME"] == str(tmp_path / "home")
    assert app.env["CODEX_HOME"] == str(tmp_path / "home" / ".codex")


def test_harness_cleans_controller_when_ui_start_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Harness contract: partial startup never leaves a child process alive."""

    app = ModelHubTestApp(Path.cwd(), tmp_path)
    process: subprocess.Popen[bytes] | None = None

    def start_controller() -> None:
        nonlocal process
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        app._controller = process

    def fail_ui_start() -> None:
        raise OSError("synthetic UI startup failure")

    monkeypatch.setattr(app, "_initialize_config", lambda: None)
    monkeypatch.setattr(app, "_start_controller", start_controller)
    monkeypatch.setattr(app, "_start_ui", fail_ui_start)
    try:
        with pytest.raises(
            RuntimeError, match="hermetic Model Hub app failed to start"
        ):
            app.start()
        assert process is not None
        assert process.poll() is not None
        assert app._controller is None
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize(
    "interruption_type",
    [KeyboardInterrupt, SystemExit],
    ids=["keyboard-interrupt", "system-exit"],
)
def test_harness_cleans_controller_when_startup_is_interrupted(
    monkeypatch,
    tmp_path: Path,
    interruption_type: type[BaseException],
) -> None:
    """Harness contract: startup interruptions cannot leak controller children."""

    app = ModelHubTestApp(Path.cwd(), tmp_path)
    process: subprocess.Popen[bytes] | None = None

    def start_controller() -> None:
        nonlocal process
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        app._controller = process

    def interrupt_ui_start() -> None:
        raise interruption_type("synthetic startup interruption")

    monkeypatch.setattr(app, "_initialize_config", lambda: None)
    monkeypatch.setattr(app, "_start_controller", start_controller)
    monkeypatch.setattr(app, "_start_ui", interrupt_ui_start)
    try:
        with pytest.raises(
            interruption_type,
            match="synthetic startup interruption",
        ):
            app.start()
        assert process is not None
        assert process.poll() is not None
        assert app._controller is None
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_harness_retries_with_fresh_port_after_ui_bind_race(
    monkeypatch,
) -> None:
    """Harness contract: an occupied UI port retries without child leaks."""

    ui_processes: list[subprocess.Popen[bytes]] = []
    controller_process: subprocess.Popen[bytes] | None = None
    with tempfile.TemporaryDirectory(
        prefix="avibe-model-hub-port-race-", dir="/tmp"
    ) as runtime_root:
        app = ModelHubTestApp(Path.cwd(), Path(runtime_root))
        occupied_port = app.port
        start_ui = app._start_ui

        def start_and_capture_ui() -> None:
            start_ui()
            assert app._ui is not None
            ui_processes.append(app._ui)

        monkeypatch.setattr(app, "_start_ui", start_and_capture_ui)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
                blocker.bind(("127.0.0.1", occupied_port))
                blocker.listen(1)
                with app:
                    controller_process = app._controller
                    assert controller_process is not None
                    assert app.port != occupied_port
                    assert len(ui_processes) >= 2
                    assert all(
                        process.poll() is not None
                        for process in ui_processes[:-1]
                    )
                    assert ui_processes[-1].poll() is None
        finally:
            app.stop()

    assert controller_process is not None
    assert controller_process.poll() is not None
    assert all(process.poll() is not None for process in ui_processes)


def test_harness_cleans_detached_child_after_leader_exits(
    tmp_path: Path,
) -> None:
    """Harness contract: AVIBE_HOME ownership outlives recorded leaders."""

    app = ModelHubTestApp(Path.cwd(), tmp_path)
    leader_code = (
        "import subprocess, sys; "
        "child = subprocess.Popen("
        "[sys.executable, '-c', 'import time; time.sleep(60)', sys.argv[1]], "
        "start_new_session=True); "
        "print(child.pid, flush=True)"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code, str(app.avibe_home)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child_pid: int | None = None
    try:
        assert leader.stdout is not None
        child_pid = int(leader.stdout.readline())
        leader.wait(timeout=5)
        assert any(
            pid == child_pid
            for processes in app._owned_process_groups().values()
            for pid, _argv in processes
        )

        app.stop()

        assert app._owned_process_groups() == {}
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if leader.poll() is None:
            os.killpg(leader.pid, signal.SIGKILL)
            leader.wait(timeout=5)
        if child_pid is not None:
            for process_group, processes in (
                app._owned_process_groups().items()
            ):
                if any(pid == child_pid for pid, _argv in processes):
                    try:
                        os.killpg(process_group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


def test_a1_feature_flag_disables_the_complete_models_api(
    disabled_model_hub_app,
) -> None:
    """A1: every Model Hub API route is dormant when the flag is off."""

    requests = (
        ("GET", "/api/models/sources", None),
        ("GET", "/api/models/runtime/status", None),
        ("GET", "/api/models/events", None),
        ("GET", "/api/models/usage", None),
        ("POST", "/api/models/sources/observe", {}),
        ("PATCH", "/api/models/agents/claude/mode", {"mode": "hub"}),
    )
    for method, path, payload in requests:
        response = disabled_model_hub_app.client.request(
            method,
            path,
            **({"json_body": payload} if payload is not None else {}),
        )
        body = response.json()
        assert response.status == 404, (method, path, body)
        assert body == {
            "ok": False,
            "contract_version": 6,
            "error": "feature_disabled",
        }

    config = disabled_model_hub_app.client.get("/api/config")
    assert config.status == 200
    assert config.json()["capabilities"]["model_hub"]["enabled"] is False


def test_a2_offline_engine_install_start_stop_and_hardened_config(
    model_hub_app_factory,
) -> None:
    """A2: install/start/stop preserves the hardened engine configuration."""

    with _engine_app(model_hub_app_factory) as app:
        observed = _install_engine(app)
        assert "installing" in observed

        started = app.client.post("/api/models/runtime/start", {})
        started_body = started.json()
        assert started.status == 200, started_body
        runtime = started_body["runtime"]
        assert runtime["enabled"] is True
        assert runtime["status"]["health"] == "ok"
        assert runtime["status"]["listening"]["host"] == "127.0.0.1"

        configs = list(
            (
                app.avibe_home
                / "runtime"
                / "model-hub"
                / "state"
                / "instances"
            ).glob("*/config.yaml")
        )
        assert len(configs) == 1
        config = yaml.safe_load(configs[0].read_text(encoding="utf-8"))
        assert config["host"] == "127.0.0.1"
        assert config["request-retry"] == 0
        assert config["disable-cooling"] is True
        assert config["usage-statistics-enabled"] is False
        assert config["force-model-prefix"] is True
        assert config["passthrough-headers"] is False

        stopped = app.client.post("/api/models/runtime/stop", {})
        stopped_body = stopped.json()
        assert stopped.status == 200, stopped_body
        assert stopped_body["runtime"]["enabled"] is False
        assert stopped_body["runtime"]["status"]["health"] == "not_started"


def test_a3_runtime_stop_reports_every_blocking_backend(
    model_hub_app,
) -> None:
    """A3: the API guards runtime stop with every blocking backend."""

    for backend in ("claude", "codex"):
        changed = model_hub_app.client.patch(
            f"/api/models/agents/{backend}/mode", {"mode": "hub"}
        )
        assert changed.status == 200, changed.json()
    refused = model_hub_app.client.post("/api/models/runtime/stop", {})
    body = refused.json()
    assert refused.status == 409, body
    assert body["error"] == "runtime_in_use"
    assert body["backends"] == ["claude", "codex"]


def test_a5_controller_restart_during_oauth_poll_reports_engine_down(
    model_hub_app_factory,
) -> None:
    """A5: restart during a Hub OAuth poll does not fake materialization."""

    with _engine_app(model_hub_app_factory) as app:
        _install_engine(app)
        started = app.client.post("/api/models/runtime/start", {})
        assert started.status == 200, started.json()
        oauth = app.client.post(
            "/api/models/oauth/start",
            {
                "vendor": "anthropic",
                "channel": "hub",
                "client_nonce": "ofn_a500000000000001",
            },
        )
        oauth_body = oauth.json()
        assert oauth.status == 200, oauth_body
        flow_id = oauth_body["flow"]["flow_id"]

        app.restart_controller()
        poll = app.client.get(f"/api/models/oauth/status/{flow_id}")
        body = poll.json()
        assert poll.status == 503, body
        assert body["error"] == "engine_down"
        assert "material" not in str(body.get("detail", "")).lower()
