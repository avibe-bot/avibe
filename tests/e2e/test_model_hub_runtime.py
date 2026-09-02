"""Model Hub feature gate and managed-runtime E2E scenarios."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
import yaml

from core.managed_runtime import runtime_platform_tag
from tests.e2e.drivers.model_hub_app import (
    HTTPResult,
    ModelHubTestApp,
    _OwnedProcess,
)


pytestmark = pytest.mark.e2e_model_hub

_ENGINE_MANIFEST_PLATFORMS = {
    "linux-x64": "linux-amd64",
}


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

    def seed_archive(app: ModelHubTestApp) -> None:
        payload = json.loads(Path(manifest).read_text(encoding="utf-8"))
        host_platform = runtime_platform_tag()
        platform = _ENGINE_MANIFEST_PLATFORMS.get(
            host_platform,
            host_platform,
        )
        archive = next(
            (
                item
                for item in payload["assets"]
                if item["platform"] == platform
            ),
            None,
        )
        if archive is None:
            return
        parsed = urllib.parse.urlparse(archive["url"])
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            pytest.skip(
                "managed Model Hub E2E requires a local file archive "
                "in its offline manifest"
            )
        source = Path(urllib.parse.unquote(parsed.path))
        if not source.is_file():
            pytest.skip(
                f"managed Model Hub engine archive does not exist: {source}"
            )
        downloads = (
            app.avibe_home
            / "runtime"
            / "model-hub"
            / "engine"
            / "downloads"
        )
        downloads.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, downloads / source.name)

    return model_hub_app_factory(
        extra_env={
            "VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH": manifest,
            "VIBE_MODEL_HUB_ENGINE_OFFLINE": "1",
        },
        before_start=seed_archive,
    )


def _install_engine(app) -> list[str]:
    observed: list[str] = []
    response = app.client.post("/api/models/runtime/install", {})
    body = response.json()
    if (
        response.status == 422
        and body.get("error") == "runtime_platform_unsupported"
    ):
        host_reason = body.get("detail") or body["error"]
        pytest.skip(
            "managed Model Hub engine unavailable on this host: "
            f"{host_reason}"
        )
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


def _app_with_install_response(
    status: int, body: bytes
) -> SimpleNamespace:
    response = HTTPResult(status=status, headers={}, body=body)
    client = SimpleNamespace(post=lambda *_args, **_kwargs: response)
    return SimpleNamespace(client=client)


def test_install_engine_skips_exact_unsupported_host_response() -> None:
    """Harness contract: an unsupported manifest target is a precondition."""

    app = _app_with_install_response(
        422,
        b'{"error":"runtime_platform_unsupported","detail":"linux/s390x"}',
    )

    with pytest.raises(pytest.skip.Exception, match="linux/s390x"):
        _install_engine(app)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (422, b'{"error":"engine_down"}'),
        (500, b'{"error":"runtime_platform_unsupported"}'),
    ],
)
def test_install_engine_keeps_other_installer_failures_red(
    status: int,
    body: bytes,
) -> None:
    """Harness contract: post-precondition product failures never skip."""

    app = _app_with_install_response(status, body)

    with pytest.raises(AssertionError):
        _install_engine(app)


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
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Harness contract: no running marked child outlives its leader."""

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
            process.pid == child_pid
            for processes in app._owned_process_groups().values()
            for process in processes
        )

        with caplog.at_level(
            "WARNING", logger="tests.e2e.drivers.model_hub_app"
        ):
            app.stop()

        assert app._owned_process_groups() == {}
        try:
            child_status = psutil.Process(child_pid).status()
        except psutil.NoSuchProcess:
            child_status = None
        assert child_status in {None, psutil.STATUS_ZOMBIE}
        if child_status == psutil.STATUS_ZOMBIE:
            assert f"pid={child_pid}" in caplog.text
    finally:
        if leader.poll() is None:
            os.killpg(leader.pid, signal.SIGKILL)
            leader.wait(timeout=5)
        if child_pid is not None:
            for process_group, processes in (
                app._owned_process_groups().items()
            ):
                if any(
                    process.pid == child_pid for process in processes
                ):
                    try:
                        os.killpg(process_group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


def test_harness_tracks_pid_status_after_marker_argv_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Harness contract: tracked PID status, not stale argv, proves absence."""

    app = ModelHubTestApp(Path.cwd(), tmp_path)
    identity = _OwnedProcess(
        pid=4242,
        process_group=4242,
        create_time=123.0,
        argv=f"synthetic-child {app.avibe_home}",
    )
    status = {"value": psutil.STATUS_RUNNING}

    class FakeProcess:
        def create_time(self) -> float:
            return identity.create_time

        def status(self) -> str:
            return status["value"]

    monkeypatch.setattr(app, "_owned_process_groups", lambda: {})
    monkeypatch.setattr(
        "tests.e2e.drivers.model_hub_app.psutil.Process",
        lambda _pid: FakeProcess(),
    )
    tracked = {identity.pid: identity}

    running, zombies = app._wait_for_owned_processes(tracked, timeout=0)
    assert running == tracked
    assert zombies == {}

    status["value"] = psutil.STATUS_ZOMBIE
    running, zombies = app._wait_for_owned_processes(tracked, timeout=0)
    assert running == {}
    assert zombies == tracked
    with caplog.at_level(
        "WARNING", logger="tests.e2e.drivers.model_hub_app"
    ):
        app._log_persistent_zombies(zombies)
    assert "harmless zombie" in caplog.text


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
