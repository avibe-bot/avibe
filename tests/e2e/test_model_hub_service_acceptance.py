"""Model Hub end to end through the installed service, from a fresh home.

This is the acceptance case for Model Hub on Windows: CI runs it on the x64
and Arm64 Windows runners, where nothing else starts the real service. It runs
the same way on Linux and macOS. It drives ``vibe`` exactly as a user's install
does -- the CLI starts the background service, the Web UI API configures it --
against two loopback upstreams, and proves:

- the engine installs from the selected manifest (``VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH``,
  else the packaged one), as that manifest's verified build, and becomes healthy;
- an API-key Source saves through the real credential check;
- a Vibey turn and a Claude Code hub-mode turn each get the upstream's reply;
- after the service is killed outright, the next start reaps the engine it
  left behind and runs a new one.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Iterator, Mapping

import psutil
import pytest

pytestmark = pytest.mark.e2e_service

_MOCK_UPSTREAM = Path(__file__).resolve().parent / "drivers" / "mock_llm_upstream.py"
_BACKEND_CATALOG = Path(__file__).resolve().parents[2] / "vibe" / "data" / "backend_models.json"
_CHAT_KEY = "sk-acceptance-chat-not-real"
_ANTHROPIC_KEY = "sk-acceptance-anthropic-not-real"
_CHAT_MODEL = "mock-model"
_CLAUDE_MODEL = "claude-sonnet-4-6"
_VIBEY_MODEL = "acceptance-chat"
_REPLY = "mock response"
_ENGINE_START_SECONDS = 600
_ENGINE_MANIFEST_ENV = "VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH"
_ENGINE_MANIFEST_URL_ENV = "VIBE_MODEL_HUB_ENGINE_MANIFEST_URL"
_TURN_SECONDS = 300


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _await(condition, *, what: str, seconds: float, interval: float = 1.0):
    deadline = time.monotonic() + seconds
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = condition()
        except (OSError, ValueError) as exc:
            last = exc
        else:
            if last:
                return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}; last observation: {last!r}")


class _Upstream:
    """One loopback LLM upstream; the shared mock serves one protocol per process."""

    def __init__(self, process: subprocess.Popen[str], url: str) -> None:
        self.process = process
        self.url = url
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def control(self, method: str, path: str, payload: object | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}{path}", data=data, method=method, headers={"Content-Type": "application/json"},
        )
        with self._opener.open(request, timeout=10) as response:
            return json.loads(response.read() or b"null")

    def configure(self, **update: Any) -> None:
        self.control("POST", "/__control/config", update)

    def requests(self) -> list[dict[str, Any]]:
        return self.control("GET", "/__control/requests")["requests"]


@contextlib.contextmanager
def _upstream(**config: Any) -> Iterator[_Upstream]:
    process = subprocess.Popen(
        [sys.executable, str(_MOCK_UPSTREAM), "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        line = process.stdout.readline() if process.stdout else ""
        prefix = "Mock LLM upstream listening at "
        assert line.startswith(prefix), line
        upstream = _Upstream(process, line[len(prefix):].strip())
        upstream.configure(**config)
        yield upstream
    finally:
        process.kill()
        process.wait(timeout=10)


class _Service:
    """The installed ``vibe`` against one isolated home, and its Web UI API."""

    def __init__(self, home: Path, port: int) -> None:
        # Every subprocess runs from here, so it must exist before the first one.
        home.mkdir(parents=True, exist_ok=True)
        self.home = home
        models_dev = home / "models-dev.json"
        models_dev.write_text("{}", encoding="utf-8")
        # Not home/.avibe: a source checkout refuses to migrate what looks like
        # the user's default state, and this home is only ever the test's.
        self.avibe_home = home / "avibe-state"
        self.port = port
        self.origin = f"http://127.0.0.1:{port}"
        self.env = {
            **os.environ,
            "AVIBE_HOME": str(self.avibe_home),
            # Windows resolves the home directory from USERPROFILE, not HOME.
            "USERPROFILE": str(home),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            # A test service reports nothing and fetches nothing it does not test;
            # the engine still installs from the packaged manifest. The catalogs
            # it reads are this checkout's backend catalog and a models.dev copy
            # that describes nothing; neither is needed by the turns below.
            "AVIBE_BACKEND_MODEL_CATALOG_URL": _BACKEND_CATALOG.as_uri(),
            "AVIBE_MODELS_DEV_URL": models_dev.as_uri(),
            "VIBE_INSTALL_SKIP_ASKILL": "1",
            "VIBE_ASKILL_AUTO_UPDATE": "0",
            "VIBE_SHOW_RUNTIME_OFFLINE": "1",
            "VIBE_TMUX_OFFLINE": "1",
            "VIBE_GIT_OFFLINE": "1",
        }
        # The manifest path is the acceptance's one engine control, as in the Model
        # Hub E2E harness: an inherited manifest URL would outrank the packaged
        # manifest the acceptance falls back to.
        self.env.pop(_ENGINE_MANIFEST_URL_ENV, None)
        if self.env.get(_ENGINE_MANIFEST_ENV):
            # The service resolves a relative path from its own working directory,
            # which is not this one; give it, and the acceptance, the same file.
            self.env[_ENGINE_MANIFEST_ENV] = str(Path(self.env[_ENGINE_MANIFEST_ENV]).expanduser().resolve())
        self._socket_dir: str | None = None
        if os.name != "nt":
            # A Unix socket path is capped near 104 bytes; a test home is deeper.
            self._socket_dir = tempfile.mkdtemp(prefix="avibe-acc-", dir="/tmp")
            self.env["VIBE_INTERNAL_DISPATCH_SOCKET"] = os.path.join(self._socket_dir, "dispatch.sock")
        self._cookies = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self._cookies),
        )
        self._csrf: str | None = None
        # Each service and UI this home ran, by pid and start time: a start can
        # drop the record of one still running, and cleanup must not rely on it.
        self._started: list[dict[str, Any]] = []

    def python(self, *args: str, timeout: float = 300, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run this interpreter outside the checkout, so it imports the installed package."""

        result = subprocess.run(
            [sys.executable, *args],
            cwd=self.home,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if check:
            assert result.returncode == 0, (args, result.returncode, result.stdout[-4000:], result.stderr[-4000:])
        return result

    def packaged_engine_manifest(self) -> Path:
        """The engine manifest the installed package ships, which the service selects without an override."""

        result = self.python("-c", (
            "from importlib import resources; "
            "print(resources.files('vibe').joinpath('model_hub_runtime/cliproxyapi_manifest.json'))"
        ))
        return Path(result.stdout.strip())

    def vibe(self, *args: str, timeout: float = 300, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.python("-m", "vibe", *args, timeout=timeout, check=check)

    def request(self, method: str, path: str, payload: object | None = None, *, timeout: float = 60) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if method != "GET":
            if self._csrf is None:
                _status, body = self.request("GET", "/api/csrf-token")
                self._csrf = body["csrf_token"]
            headers.update({
                "Content-Type": "application/json",
                "Origin": self.origin,
                "X-Vibe-CSRF-Token": self._csrf,
            })
            data = json.dumps({} if payload is None else payload).encode("utf-8")
        request = urllib.request.Request(f"{self.origin}{path}", data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"null")
            except ValueError:
                return exc.code, raw.decode("utf-8", "replace")

    def ok(self, method: str, path: str, payload: object | None = None, *, timeout: float = 60) -> Any:
        status, body = self.request(method, path, payload, timeout=timeout)
        assert 200 <= status < 300 and isinstance(body, dict) and body.get("ok") is not False, (
            method, path, status, body,
        )
        return body

    def seed_config(self) -> None:
        """Write the first-run config ``vibe start`` would, on a port of our own.

        The default port may belong to a real service on a developer machine.
        Update checks are off: they would ask PyPI and npm for new releases.
        """

        self.python("-c", (
            "from config import paths; from core.services import settings; "
            "config = settings.default_config(); "
            f"config.ui.setup_port = {self.port}; config.ui.open_browser = False; "
            "config.update.check_interval_minutes = 0; "
            "config.save(paths.get_config_path())"
        ))
        assert (self.avibe_home / "config" / "config.json").is_file()

    def wait_ready(self) -> None:
        _await(
            lambda: self.request("GET", "/health", timeout=5)[1] == {"status": "ok"},
            what="the Web UI health check", seconds=120,
        )
        _await(
            lambda: self.request("GET", "/api/models/sources", timeout=10)[0] == 200,
            what="the Model Hub API", seconds=120,
        )
        self._remember_recorded()

    def _recorded_pids(self) -> list[int]:
        """The service and UI pids this home's runtime records right now."""

        runtime = self.avibe_home / "runtime"
        recorded: list[int] = []
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
            recorded.append(int(json.loads((runtime / "service.lock").read_text(encoding="utf-8"))["pid"]))
        for name in ("vibe.pid", "vibe-ui.pid"):
            with contextlib.suppress(OSError, ValueError):
                recorded.append(int((runtime / name).read_text(encoding="utf-8").strip()))
        return recorded

    def _remember_recorded(self) -> None:
        for pid in self._recorded_pids():
            if not self._runs_here(pid):
                continue
            with contextlib.suppress(psutil.Error):
                entry = {"pid": pid, "create_time": psutil.Process(pid).create_time()}
                if entry not in self._started:
                    self._started.append(entry)

    def _runs_here(self, pid: int) -> bool:
        """Whether ``pid`` carries this home, which no other process on the machine does."""

        try:
            return psutil.Process(pid).environ().get("AVIBE_HOME") == str(self.avibe_home)
        except psutil.Error:
            return False

    @staticmethod
    def _kill(pid: int) -> None:
        # An external command kills it: the service is not a child of this test.
        command = ["taskkill", "/F", "/PID", str(pid)] if os.name == "nt" else ["kill", "-KILL", str(pid)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        _await(
            lambda: not psutil.pid_exists(pid),
            what=f"process {pid} to exit after {command[0]} ({result.returncode}: {result.stderr.strip()})",
            seconds=30, interval=0.2,
        )

    def kill_service(self) -> None:
        """Kill the service outright, the way a crash or ``taskkill /F`` would.

        The pid comes from this home's service lock and must carry this home in
        its environment, so nothing else on the machine can be the target.
        """

        lock = json.loads((self.avibe_home / "runtime" / "service.lock").read_text(encoding="utf-8"))
        assert lock.get("phase") == "running", lock
        pid = int(lock["pid"])
        assert self._runs_here(pid), pid
        self._kill(pid)

    def stop(self) -> None:
        """Stop the service, then kill each service or UI of this home that ``vibe stop`` left."""

        try:
            result = self.vibe("stop", timeout=120, check=False)
        except subprocess.TimeoutExpired:
            print("vibe stop timed out")
        else:
            if result.returncode != 0:
                print(f"vibe stop exited {result.returncode}: {result.stdout[-2000:]}{result.stderr[-2000:]}")
        self._remember_recorded()
        for entry in self._started:
            if _process_is(entry):
                print(f"vibe stop left pid {entry['pid']} running; killing it")
                self._kill(entry["pid"])

    def engines(self) -> list[dict[str, Any]]:
        record = self.avibe_home / "runtime" / "model-hub" / "state" / "engine-process.json"
        try:
            payload = json.loads(record.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        return [entry for entry in payload.get("engines", []) if isinstance(entry.get("pid"), int)]

    def healthy_engine(self) -> dict[str, Any]:
        def ready():
            status = self.ok("GET", "/api/models/runtime/status")["runtime"]["status"]
            engines = self.engines()
            return engines[0] if status.get("health") == "ok" and len(engines) == 1 else None

        return _await(ready, what="a healthy engine", seconds=_ENGINE_START_SECONDS, interval=2)

    def close(self) -> None:
        if self._socket_dir is not None:
            shutil.rmtree(self._socket_dir, ignore_errors=True)

    def diagnostics(self) -> str:
        parts = []
        for name in ("logs/vibe_remote.log", "runtime/service_stderr.log", "runtime/ui_stderr.log"):
            path = self.avibe_home / name
            if path.exists():
                parts.append(f"--- {name} (tail) ---\n" + path.read_text(encoding="utf-8", errors="replace")[-6000:])
        return "\n".join(parts)


def _process_is(entry: dict[str, Any]) -> bool:
    """Whether the recorded process is still the one the record names."""

    try:
        return abs(psutil.Process(entry["pid"]).create_time() - float(entry["create_time"])) < 0.01
    except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, TypeError, ValueError):
        return False


def _selected_engine_manifest(env: Mapping[str, str], packaged_manifest: Path) -> dict[str, Any]:
    """The manifest the service selects: the one ``VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH`` names, else the packaged one.

    ``_Service`` makes the override absolute before the service starts, so both
    read the same file, and leaves the service no other selector. One that is missing, unreadable or names no version
    fails the run: an acceptance must never pass without knowing what it accepted.
    """

    override = env.get(_ENGINE_MANIFEST_ENV)
    path = Path(override) if override else packaged_manifest
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        pytest.fail(f"the selected engine manifest {path} is unreadable: {exc}")
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version.strip():
        pytest.fail(f"the selected engine manifest {path} names no version")
    return payload


def _asset(manifest: dict[str, Any], platform: str) -> dict[str, Any] | None:
    assets = manifest.get("assets")
    return next(
        (asset for asset in assets if isinstance(asset, dict) and asset.get("platform") == platform),
        None,
    ) if isinstance(assets, list) else None


def _assert_installed_engine(runtime: dict[str, Any], selected: dict[str, Any]) -> None:
    """The running engine is the selected manifest's build, not merely its upstream version.

    The service reports the manifest it loaded and verifies the installed
    binary against that manifest's asset, so the loaded asset must be the
    selected one, byte identity included. An ignored override installs the
    packaged engine and fails here even when both name the same upstream version.
    """

    platform = runtime["host_platform"]
    wanted = _asset(selected, platform)
    if wanted is None or not isinstance(wanted.get("sha256"), str):
        pytest.fail(f"the selected engine manifest has no asset for {platform}")
    loaded = runtime["manifest"]
    identity = ("version", "source_sha")
    assert {key: loaded.get(key) for key in identity} == {key: selected.get(key) for key in identity}, runtime
    got = _asset(loaded, platform) or {}
    assert (got.get("sha256"), got.get("size_bytes")) == (wanted["sha256"], wanted.get("size_bytes")), runtime
    status = runtime["status"]
    assert status["verified"] is True and status["installed_version"] == selected["version"], (
        f"installed engine {status['installed_version']}, selected manifest {selected['version']}: {runtime}"
    )


def _add_model(service: _Service, backend: str, model_id: str) -> None:
    baseline = service.ok("GET", f"/api/models/agents/{backend}/models")["agent"].get("catalog_models") or []
    if any(model.get("id") == model_id for model in baseline):
        return
    service.ok("PUT", f"/api/models/agents/{backend}/models", {
        "baseline": baseline,
        "models": [*baseline, {"id": model_id, "origin": "manual"}],
    })


def _route(service: _Service, backend: str, model_id: str, source_id: str, upstream_model: str) -> None:
    _add_model(service, backend, model_id)
    service.ok(
        "PUT", f"/api/models/agents/{backend}/chain?model={model_id}",
        {"hops": [{"source_id": source_id, "model_id": upstream_model}]},
    )
    service.vibe("agent", "update", backend, "--model", model_id)


def _turn(service: _Service, agent: str) -> dict[str, Any]:
    result = service.vibe(
        "agent", "run", "--agent", agent, "--sync", "--wait-timeout", str(_TURN_SECONDS),
        "--message", "Reply with the upstream's answer.", timeout=_TURN_SECONDS + 60,
    )
    return json.loads(result.stdout)["run"]


def test_fresh_install_serves_vibey_and_claude_through_the_hub_and_reaps_an_orphaned_engine(tmp_path) -> None:
    """Scenario: MH-SERVICE-001 — the installed service, end to end from a fresh home."""
    home = Path(os.environ["HOME"])
    service = _Service(home, _free_port())
    service.seed_config()
    started: list[dict[str, Any]] = []
    # The Anthropic relay takes the key only as Bearer, which is how the engine
    # sends a custom relay's key, so the Source must also verify with it (#2667).
    with _upstream(protocol="openai_chat", models=[_CHAT_MODEL], required_api_key=_CHAT_KEY) as chat, _upstream(
        protocol="anthropic", models=[_CLAUDE_MODEL], required_api_key=_ANTHROPIC_KEY,
        required_auth_scheme="bearer",
    ) as anthropic:
        try:
            service.vibe("start", "--no-open-browser", timeout=300)
            service.wait_ready()

            # The engine installs from the selected manifest (a candidate's, else the
            # packaged one) and becomes healthy.
            runtime = service.ok("POST", "/api/models/runtime/start", timeout=_ENGINE_START_SECONDS)["runtime"]
            first = service.healthy_engine()
            started.append(first)
            # Read once healthy: while it installs, no version is reported yet.
            _assert_installed_engine(
                service.ok("GET", "/api/models/runtime/status")["runtime"],
                _selected_engine_manifest(service.env, service.packaged_engine_manifest()),
            )
            # CI names the asset each runner must get, which is not always
            # Python's architecture: Windows on Arm runs x64 Python.
            expected_platform = os.environ.get("AVIBE_EXPECT_ENGINE_PLATFORM")
            if expected_platform:
                assert runtime["host_platform"] == expected_platform, runtime

            # API-key Sources save through the real credential check, which
            # requires the listing to refuse a request without the key.
            chat_source = service.ok("POST", "/api/models/sources", {
                "kind": "api_key", "vendor": "custom", "display_name": "Acceptance Chat",
                "base_url": f"{chat.url}/v1", "key": _CHAT_KEY, "protocol": "openai_chat",
            }, timeout=120)["source"]
            anthropic_source = service.ok("POST", "/api/models/sources", {
                "kind": "api_key", "vendor": "custom", "display_name": "Acceptance Anthropic",
                "base_url": anthropic.url, "key": _ANTHROPIC_KEY, "protocol": "anthropic",
            }, timeout=120)["source"]
            # A Vibey turn gets the upstream's reply through the hub.
            _route(service, "vibey", _VIBEY_MODEL, chat_source["id"], _CHAT_MODEL)
            vibey = _turn(service, "vibey")
            assert vibey["status"] == "succeeded", vibey
            assert _REPLY in (vibey.get("result_text") or ""), vibey
            assert any(item["path"] == "/v1/chat/completions" for item in chat.requests())

            # So does a Claude Code turn in hub mode.
            service.ok("PATCH", "/api/models/agents/claude/mode", {"mode": "hub"})
            _route(service, "claude", _CLAUDE_MODEL, anthropic_source["id"], _CLAUDE_MODEL)
            claude = _turn(service, "claude")
            assert claude["status"] == "succeeded", claude
            assert _REPLY in (claude.get("result_text") or ""), claude
            assert any(item["path"] == "/v1/messages" for item in anthropic.requests())

            # Kill the service outright: its engine is left running.
            first = service.engines()[0]
            service.kill_service()
            assert _process_is(first), "the engine should outlive a killed service"

            # The next start reaps that engine before it runs a new one.
            service.vibe("start", "--no-open-browser", timeout=300)
            service.wait_ready()
            service.ok("POST", "/api/models/runtime/start", timeout=_ENGINE_START_SECONDS)
            second = service.healthy_engine()
            started.append(second)
            assert second["pid"] != first["pid"] or second["create_time"] != first["create_time"]
            assert not _process_is(first), "the orphaned engine is still running"
            log = (service.avibe_home / "logs" / "vibe_remote.log").read_text(encoding="utf-8", errors="replace")
            assert "Reaped a Model Hub engine left running by an earlier service" in log

            service.vibe("stop", timeout=120)
            assert service.engines() == []
            assert not _process_is(second)
        except BaseException:
            print(service.diagnostics())
            raise
        finally:
            service.stop()
            service.close()
            for entry in started:
                if _process_is(entry):
                    psutil.Process(entry["pid"]).kill()
