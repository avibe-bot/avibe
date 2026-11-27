"""The UI's disk key and a controller child's live answer must name the same native scope."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

from core.handlers.direct_inventory_identity import inventory_identity
from modules.agents.opencode import picker_inventory as picker
from modules.agents.opencode.agent import OpenCodeAgent
from modules.agents.opencode.client_manager import OpenCodeRuntime
from modules.agents.opencode.server import OpenCodeLaunchSpec


def write(path, value):
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def native(tmp_path, monkeypatch):
    # Nothing from a developer's production profile or credentials reaches
    # either the reader or the fixture daemon. No configured backend is run.
    home = tmp_path / "home"
    cwd = tmp_path / "workspace"
    (cwd / ".git").mkdir(parents=True)
    home.mkdir()
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "AVIBE_HOME": str(home / ".avibe"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "conf"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "PATH": str(Path(sys.executable).parent),
    }
    if os.name == "nt":
        env["SystemRoot"] = os.environ["SystemRoot"]
    monkeypatch.setattr(os, "environ", env)
    config = NS(agents=NS(opencode=NS(enabled=True)), runtime=NS(default_cwd=str(cwd)))
    return NS(
        config=config,
        env=env,
        cwd=cwd,
        home=home,
        tmp=tmp_path,
        auth=tmp_path / "data/opencode/auth.json",
        global_config=tmp_path / "conf/opencode/opencode.json",
        identity=lambda: inventory_identity("opencode", config, cwd=str(cwd)),
    )


@pytest.mark.parametrize(
    "credential",
    [
        {},
        [],
        {"openai": {}},
        {"openai": {"type": "api", "key": " "}},
        {"openai": {"type": "oauth", "access": "A", "refresh": "R"}},
        {"openai": {"type": "wellknown", "key": "X", "token": "Y"}},
    ],
)
def test_files_are_not_evidence_of_an_account(native, credential):
    write(native.auth, credential)
    write(native.global_config, {"theme": "dark"})
    write(native.cwd / "opencode.json", {"model": "openai/old", "provider": {"openai": {"name": "OpenAI"}}})
    write(native.home / ".local/share/opencode/auth.json", {"openai": {"type": "api", "key": "other-profile"}})
    assert native.identity() is None


@pytest.mark.parametrize("reference", ["{file:token}", "{env:MY_FIXTURE_TOKEN}"])
def test_dont_hash_substitutions_as_if_they_were_credentials(native, reference):
    native.env["MY_FIXTURE_TOKEN"] = "key-A"
    write(native.cwd / "opencode.jsonc", {"provider": {"openai": {"options": {"apiKey": reference}}}})
    assert native.identity() is None
    write(native.auth, {"openai": {"type": "api", "key": "key-B"}})
    assert native.identity() is None  # Can't prove which provider credential actually wins.


def test_only_the_effective_non_ambiguous_project_and_global_layers_are_evidence(native):
    provider = {"provider": {"openai": {"options": {"apiKey": "fixture"}}}}
    write(native.home / ".config/opencode/opencode.json", provider)  # shadowed by XDG
    write(native.tmp / "elsewhere/opencode.json", provider)
    assert native.identity() is None
    write(native.global_config, provider)
    a = native.identity()
    assert a
    write(native.cwd / "opencode.json", {"disabled_providers": ["openai"]})
    assert native.identity() is None
    write(native.cwd / "opencode.json", provider)
    assert native.identity() is None  # overlapping provider layers
    native.global_config.unlink()
    assert native.identity()
    native.env["OPENCODE_CONFIG_CONTENT"] = "{}"
    assert native.identity() is None


def test_stable_account_id_not_rotating_tokens_or_credential_presence(native):
    def auth(account, n):
        write(
            native.auth,
            {"openai": {"type": "oauth", "accountId": account, "access": f"a{n}", "refresh": f"r{n}", "expires": n}},
        )

    auth("account-A", 1)
    a = native.identity()
    before = picker.binding(native.config, str(native.cwd))
    auth("account-A", 2)
    assert a and native.identity() == a
    assert picker.binding(native.config, str(native.cwd))[1] != before[1]
    auth("account-B", 2)
    assert native.identity() not in (None, a)
    native.auth.unlink()
    assert native.identity() is None


@pytest.mark.reap_processes
def test_reader_process_does_not_get_a_controllers_other_env_profile(native):
    native.env["OPENAI_API_KEY"] = "controller-fixture"
    a = native.identity()
    assert a
    # Same files in a different process, no shared registries; write only
    # the opaque identity to stdout (never the credential or environment).
    program = (
        "from core.handlers.direct_inventory_identity import inventory_identity; "
        "from types import SimpleNamespace as S; import sys; "
        "print(inventory_identity('opencode', S(agents=S(opencode=S(enabled=True))),cwd=sys.argv[1]))"
    )

    def other(**override):
        return subprocess.check_output(
            [sys.executable, "-c", program, str(native.cwd)],
            cwd=Path(__file__).resolve().parents[1],
            env={**native.env, **override},
            text=True,
            timeout=15,
        ).strip()

    assert other(UNRELATED_APPLICATION_TOKEN="other") == a
    assert other(OPENAI_API_KEY="reader-fixture") not in (a, "None")
    assert other(OPENAI_API_KEY="") == "None"
    native.env["OPENAI_BASE_URL"] = "https://changed.invalid"
    assert native.identity() not in (a, None)
    second = native.tmp / "second"
    (second / ".git").mkdir(parents=True)
    assert inventory_identity("opencode", native.config, cwd=str(second)) != native.identity()


# This is the only network boundary used. The controller really spawns and
# owns this short-lived fixture process; it listens only on its chosen loopback
# port. Mutable files control its canned native documents and record GETs.
_DAEMON = """
from http.server import BaseHTTPRequestHandler, HTTPServer
import json, os, sys
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with Path("requests").open("a") as f:
            print(json.dumps([self.path,self.headers.get("x-opencode-directory")]),file=f)
        data=json.loads(Path("documents").read_text()).get(self.path)
        self.send_response(404 if data is None else 200)
        self.send_header("Content-Type","application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    def log_message(self,*args): pass
port=int(next(s for s in sys.argv if s.startswith("--port=")).split("=")[1])
HTTPServer(("127.0.0.1",port),Handler).serve_forever()
"""


@pytest.mark.reap_processes
@pytest.mark.asyncio
async def test_only_warm_matching_generation_can_publish_full_connected_catalog(native, monkeypatch):
    from modules.agents.opencode import server as owner

    monkeypatch.chdir(native.tmp)
    binary = os.path.realpath(sys.executable)
    script = native.tmp / "fixture.py"
    script.write_text(_DAEMON, encoding="utf-8")
    actual_spawn = asyncio.create_subprocess_exec
    children = []

    async def spawn(path, *args, **kw):
        assert not children and path == binary and args[0] == "serve", "picker tried to launch another daemon"
        child = await actual_spawn(path, "-I", str(script), *args, cwd=str(native.tmp), **kw)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(owner, "SERVER_START_TIMEOUT", 5)
    monkeypatch.setattr(owner, "generation_records_dir", lambda: native.tmp / "records")
    monkeypatch.setattr(owner, "_OWNED_HERE", {})
    monkeypatch.setattr(owner, "_CHILDREN_HERE", {})
    monkeypatch.setattr(owner, "opencode_disabled_mcp", lambda: {})
    native.env["OPENAI_API_KEY"] = "launch-fixture"
    cfg = {"provider": {"openrouter": {"options": {"apiKey": "project-fixture"}}}}
    write(native.cwd / "opencode.json", cfg)
    full = {
        "providers": [
            {"id": "openai", "models": {"one": {"name": "One", "variants": {"high": {}}}}},
            {"id": "openrouter", "models": {"org/two": {"name": "Two"}}},
            {"id": "disconnected", "models": {"never": {}}},
        ]
    }
    connections = {
        "all": [{"id": "openai", "models": {"not-the-picker": {}}}, {"id": "openrouter"}, {"id": "disconnected"}],
        "connected": ["openai", "openrouter"],
    }
    docs = {
        "/global/health": {"healthy": True},
        "/path": {"directory": str(native.cwd)},
        "/config": {**cfg, "tools": {"skill": False}},
        "/provider": connections,
        "/config/providers": full,
    }
    documents = native.tmp / "documents"
    write(documents, docs)
    agent = OpenCodeAgent.__new__(OpenCodeAgent)
    agent._runtime = runtime = OpenCodeRuntime(NS(binary=binary, request_timeout_seconds=4))
    service = NS(agents={"opencode": agent}, is_backend_ready=lambda _: True)
    agent.controller = controller = NS(agent_service=service)
    cwd = str(native.cwd)
    current = None
    try:
        with pytest.raises(ValueError, match="no serving"):
            await picker.discover(controller, binary, cwd)
        assert children == [] and not runtime.adopted
        # Only this simulated normal turn acquires a generation and warms the
        # directory. Discovery must not use this API even for a dead current.
        runtime._adopted = True
        turn = await runtime.acquire(OpenCodeLaunchSpec(digest="fixture", binary=binary))
        current = turn.generation.runtime
        await turn.release()
        with pytest.raises(ValueError, match="cold directory"):
            await picker.discover(controller, binary, cwd)
        await current.ensure_directory_ready(cwd)
        rows = await picker.discover(controller, binary, cwd)
        assert [(r.id, r.reasoning_efforts) for r in rows] == [("openai/one", ("high",)), ("openrouter/org/two", None)]
        reads = [json.loads(s) for s in (native.tmp / "requests").read_text().splitlines()]
        assert reads[-4:] == [[path, cwd] for path in ("/path", "/config", "/provider", "/config/providers")]
        # An idle, still-serving daemon was borrowed then released; no lease,
        # new start, or dependence on an admitted turn for future menu reads.
        assert not runtime.has_bound_work()
        for wrong_binary in (str(script), str(native.tmp / "missing")):
            with pytest.raises(ValueError, match="launch profile"):
                await picker.discover(controller, wrong_binary, cwd)
        native.env["OPENAI_API_KEY"] = "different-process"
        with pytest.raises(ValueError, match="launch profile"):
            await picker.discover(controller, binary, cwd)
        native.env["OPENAI_API_KEY"] = "launch-fixture"
        write(native.auth, {"anthropic": {"type": "api", "key": "new-key"}})
        with pytest.raises(ValueError, match="launch profile"):
            await picker.discover(controller, binary, cwd)
        native.auth.unlink()
        write(native.cwd / "opencode.json", {})  # server still has stale per-directory provider config
        with pytest.raises(ValueError, match="effective provider config"):
            await picker.discover(controller, binary, cwd)
        write(native.cwd / "opencode.json", cfg)
        current.model_hub_overlay_hash = "fixture-overlay"
        with pytest.raises(ValueError, match="no serving"):
            await picker.discover(controller, binary, cwd)
        current.model_hub_overlay_hash = None
        with monkeypatch.context() as patch:
            patch.setattr(current, "picker_launch_proof", None)  # adopted or pre-upgrade child
            with pytest.raises(ValueError, match="no serving"):
                await picker.discover(controller, binary, cwd)
        with monkeypatch.context() as patch:
            # A native profile switch during a successful GET must discard
            # the answer, not return the old account's models under a new key.
            get = current.direct_picker_providers

            async def switch_profile(directory):
                result = await get(directory)
                patch.setitem(native.env, "OPENAI_API_KEY", "changed-in-flight")
                return result

            patch.setattr(current, "direct_picker_providers", switch_profile)
            with pytest.raises(ValueError, match="changed during discovery"):
                await picker.discover(controller, binary, cwd)
        for endpoint, bad in [
            ("/path", {"directory": str(native.home)}),
            ("/config", {"error": "not initialized"}),
            ("/provider", {}),
            ("/provider", {**connections, "connected": ["openai", "openai"]}),
            ("/provider", {**connections, "connected": ["disconnected"]}),
            ("/config/providers", {"providers": []}),
            ("/config/providers", {"providers": [{"id": "openai", "models": []}]}),
            ("/config/providers", None),
            (
                "/config/providers",
                {
                    "providers": [
                        {"id": "openrouter", "models": {}},
                        {"id": "openai", "key": "unexpected-live-credential", "models": {}},
                    ]
                },
            ),
            (
                "/config/providers",
                {
                    "providers": [
                        {"id": "openrouter", "models": {}},
                        {"id": "openai", "models": {"bad": {"variants": ["wrong"]}}},
                    ]
                },
            ),
        ]:
            write(documents, {**docs, endpoint: bad})
            with pytest.raises(ValueError):
                await picker.discover(controller, binary, cwd)
            assert not runtime.has_bound_work()
        write(documents, {**docs, "/provider": {**connections, "connected": []}})
        assert await picker.discover(controller, binary, cwd) == []
        children[0].terminate()
        await children[0].wait()
        with pytest.raises(ValueError, match="not serving"):
            await picker.discover(controller, binary, cwd)
        assert len(children) == 1
    finally:
        if current is not None:
            await current.close_http_session()
        for child in children:
            if child.returncode is None:
                child.terminate()
                await child.wait()
