"""AUTH-SETUP-171/175/176/177: real U1 + native + B1/B2 + R1 chain.

The only substitutions are the account provider, external tunnel service,
credential storage, and sockets. Runtime identity/OIDC/authorization and every
producer handler execute their candidate code. No running Avibe is contacted.
"""

from pathlib import Path
import http.server
import json
import os
import queue
import re
from datetime import datetime, timedelta, timezone
import uuid
import subprocess
import threading
import time
from urllib.parse import urlsplit

import pytest
import requests

from tests.test_desktop_local_instance import desktop_host as desktop_host
from tests.test_pairing_recovery import pairing_host as pairing_host

import backend
import proxy
import runtime as runtime_fixture
import peer
import updater_fixture


@pytest.fixture
def native_chain(desktop_host, monkeypatch, tmp_path):
    checkout = Path(os.environ["DESKTOP_COMBINED_CHECKOUT"]).resolve()
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
        == os.environ["DESKTOP_COMBINED_HEAD"]
    )
    assert not subprocess.check_output(["git", "diff", "--name-only"], cwd=checkout)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == os.environ["DESKTOP_R1_HEAD"]
    producer, info, log = backend.start(checkout, tmp_path)
    mapping_file = tmp_path / "mappings.json"
    mapping_file.write_text("{}")
    runtime, local = runtime_fixture.start(monkeypatch, info, mapping_file)
    studio, studio_log, studio_instance = peer.start(tmp_path / "studio", info, mapping_file)
    updater = updater_fixture.start(tmp_path)
    runtime_fixture.publish_mapping(mapping_file, "dl.avibe.bot", updater["base"])
    tunnel, tunnel_url, fingerprint, document = proxy.start(tmp_path, mapping_file)
    fixture = tmp_path / "native.json"
    fixture.write_text(
        json.dumps(
            {
                "root": str(tmp_path),
                "backend": info["base"],
                "provider": info["provider"],
                "local": local,
                "proxy": tunnel_url,
                "mappings": str(mapping_file),
            }
        )
    )
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "__CF_USER_TEXT_ENCODING") if key in os.environ}
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR"):
        path = tmp_path / key.lower()
        path.mkdir(exist_ok=True)
        environment[key] = str(path)
    environment["AVIBE_FIXTURE_TLS_SHA256"] = fingerprint
    update_config = tmp_path / "update-transport.json"
    update_config.write_text(json.dumps({"proxy": tunnel_url, "certificate": str(tmp_path / "certificate.pem")}))
    environment["AVIBE_FIXTURE_UPDATE_CONFIG"] = str(update_config)
    if os.environ.get("DESKTOP_CHAIN_SHELL") == "1":
        environment["AVIBE_FIXTURE_SHELL"] = "1"
        environment["AVIBE_FIXTURE_LEGACY_INSTALL"] = os.environ.get("DESKTOP_CHAIN_LEGACY_INSTALL", "0")
    executable = Path(os.environ["DESKTOP_NATIVE_PROBE"]).resolve()
    native = subprocess.Popen(
        [str(executable), str(fixture)],
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    events = queue.Queue()
    updates = []
    history = []
    native_log = (tmp_path / "native.log").open("w")

    def collect():
        for line in native.stdout:
            native_log.write(line)
            native_log.flush()
            try:
                value = json.loads(line)
                history.append(value)
                if value.get("event") == "update":
                    updates.append(value["state"])
                events.put(value)
            except ValueError:
                pass

    threading.Thread(target=collect, daemon=True).start()

    def send(**command):
        native.stdin.write(json.dumps(command) + "\n")
        native.stdin.flush()

    def wait(event, predicate=lambda _: True, timeout=35):
        deadline = time.monotonic() + timeout
        while True:
            try:
                value = events.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                pytest.fail(f"native {event} timed out; logs in {tmp_path}")
            if value.get("event") == event and predicate(value):
                return value

    def js(script):
        send(
            action="eval",
            script="Promise.resolve().then(async()=>{"
            + script
            + "}).then(value=>window.location='fixture-result://probe?value='+encodeURIComponent(JSON.stringify(value))).catch(error=>window.location='fixture-result://probe?value='+encodeURIComponent(JSON.stringify({error:String(error)})))",
        )
        return wait("javascript")["value"]

    def state_until(predicate):
        deadline = time.monotonic() + 35
        while True:
            send(action="state")
            value = wait("state")
            if predicate(value["state"]):
                return value
            assert time.monotonic() < deadline, {
                "state": value,
                "page": js("return document.body.innerText;"),
            }
            time.sleep(0.1)

    def ready(script):
        deadline = time.monotonic() + 15
        while not js(script):
            assert time.monotonic() < deadline, js("return document.body.innerText;")
            time.sleep(0.1)

    try:
        wait("native_ready")
        yield {
            "send": send,
            "wait": wait,
            "js": js,
            "ready": ready,
            "state": state_until,
            "info": info,
            "root": tmp_path,
            "studio": studio_instance,
            "local": local,
            "updater": updater,
            "updates": updates,
            "document": document,
            "runtime": runtime,
            "history": history,
        }
    finally:
        document.release.set()
        for process in (native, studio, producer):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        runtime.shutdown()
        tunnel.shutdown()
        updater["release"].set()
        updater["server"].shutdown()
        native_log.close()
        studio_log.close()
        log.close()
        print(f"native producer chain evidence: {tmp_path}")


@pytest.fixture
def local_held_chain(desktop_host, monkeypatch, tmp_path):
    """Hold a successful actual local response; loopback bypasses the proxy."""
    original_start = runtime_fixture.start
    armed, entered, release = threading.Event(), threading.Event(), threading.Event()

    def start(*args, **kwargs):
        server, local = original_start(*args, **kwargs)
        handler = server.RequestHandlerClass
        original_end_headers = handler.end_headers

        def end_headers(request):
            if request.path == "/" and request.headers.get("Host") == urlsplit(local).netloc and armed.is_set():
                armed.clear()
                assert b"200" in request._headers_buffer[0]
                entered.set()
                assert release.wait(timeout=25)
            return original_end_headers(request)

        monkeypatch.setattr(handler, "end_headers", end_headers)
        return server, local

    monkeypatch.setattr(runtime_fixture, "start", start)
    fixture = native_chain.__wrapped__(desktop_host, monkeypatch, tmp_path)
    try:
        value = next(fixture)
        value["local_hold"] = (armed, entered, release)
        yield value
    finally:
        release.set()
        fixture.close()


@pytest.fixture
def production_shell_chain(desktop_host, monkeypatch, tmp_path, request):
    """Run the shared production shell callbacks with inert RuntimeHost dependencies."""
    options = getattr(request, "param", {})
    monkeypatch.setenv("DESKTOP_CHAIN_SHELL", "1")
    monkeypatch.setenv("DESKTOP_CHAIN_SSE", "1" if options.get("sse", True) else "0")
    monkeypatch.setenv("DESKTOP_CHAIN_LEGACY_INSTALL", "1" if options.get("legacy_install_grant", False) else "0")
    monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    monkeypatch.setenv("AVIBE_COMPUTER_USE_STATE_DIR", str(tmp_path / "home/Library/Application Support/bot.avibe.desktop"))
    from vibe import internal_client

    async def no_running_agents():
        return {"body": {"agents": []}}

    monkeypatch.setattr(internal_client, "list_running_agents", no_running_agents)
    fixture = local_held_chain.__wrapped__(desktop_host, monkeypatch, tmp_path)
    try:
        value = next(fixture)
        value["options"] = options
        yield value
    finally:
        fixture.close()


@pytest.mark.parametrize("presentation,interruption", [
    ("remote", None), ("account", None),
    ("remote", "lifecycle"), ("account", "new_link"), ("remote", "explicit"),
])
def test_production_deep_link_finished_defers_to_one_local_owner(production_shell_chain, presentation, interruption):
    f = production_shell_chain
    login(f)
    select_studio(f)
    if presentation == "account":
        f["send"](action="present", view="instances")
        f["wait"]("loaded", lambda event: "account/index.html" in event["url"])
    before = f["state"](lambda _: True)["state"]["generation"]
    armed, entered, release = f["local_hold"]
    armed.set()
    f["send"](action="native_link", url="avibe://settings/shortcuts")
    f["wait"]("link_received")
    assert entered.wait(timeout=10), "the production receive path did not start local replacement"
    # The production Finished callback runs for the staging document while
    # the local response is held. It must not admit another ReturnLocal.
    for _ in range(3):
        f["send"](action="shell_control", operation="finished")
        f["wait"]("shell_control")
    held = f["state"](lambda _: True)
    assert held["state"]["generation"] == before + 1, held
    target = f["local"] + "/settings/shortcuts"
    expected = before + 1
    if interruption == "lifecycle":
        f["send"](action="local_unavailable")
        f["wait"]("local_unavailable")
        f["send"](action="shell_control", operation="rebind", origin=f["local"])
        f["wait"]("shell_control")
        expected += 2
    elif interruption == "new_link":
        target = f["local"] + "/settings/computer-use"
        f["send"](action="native_link", url="avibe://settings/computer-use")
        f["wait"]("link_received")
    elif interruption == "explicit":
        f["send"](action="escape")
        expected += 1
    release.set()
    if interruption == "explicit":
        f["wait"]("escaped")
        target = f["local"] + "/"
    else:
        f["wait"]("loaded", lambda event: event["url"] == target, timeout=10)
    f["send"](action="shell_status")
    assert f["wait"]("shell_status")["pending"] is (interruption == "explicit")
    current = f["state"](lambda _: True)
    assert current["visible"] is True
    assert current["state"]["generation"] == expected
    assert f["js"]("return location.href;") == target
    time.sleep(0.2)
    assert f["js"]("return location.href;") == target


@pytest.mark.parametrize("presentation,interruption", [
    ("remote", "lifecycle_twice"), ("account", "new_link"), ("remote", "explicit"),
])
def test_production_deep_link_lifecycle_before_admission_keeps_received_authority(
    production_shell_chain, presentation, interruption,
):
    """Pause the production task after claim, where earlier load holds cannot reach."""
    f = production_shell_chain
    login(f)
    select_studio(f)
    if presentation == "account":
        f["send"](action="present", view="instances")
        f["wait"]("loaded", lambda event: "account/index.html" in event["url"])
    before = f["state"](lambda _: True)["state"]["generation"]
    f["send"](action="shell_control", operation="hold_link_return")
    f["wait"]("shell_control")
    f["send"](action="native_link", url="avibe://settings/shortcuts")
    f["wait"]("link_received")
    f["wait"]("link_return_held")
    rounds = 2 if interruption == "lifecycle_twice" else 1
    for _ in range(rounds):
        f["send"](action="local_unavailable")
        f["wait"]("local_unavailable")
        f["send"](action="shell_control", operation="rebind", origin=f["local"])
        f["wait"]("shell_control")
        f["send"](action="shell_control", operation="finished")
        f["wait"]("shell_control")
    expected = before + rounds * 2
    assert f["state"](lambda _: True)["state"]["generation"] == expected
    target = f["local"] + "/settings/shortcuts"
    if interruption == "new_link":
        target = f["local"] + "/settings/computer-use"
        f["send"](action="native_link", url="avibe://settings/computer-use")
        f["wait"]("link_received")
    elif interruption == "explicit":
        f["send"](action="escape")
        assert f["wait"]("escaped")["ok"]
        target = f["local"] + "/"
    f["send"](action="shell_control", operation="release_link_return")
    f["wait"]("shell_control")
    f["wait"]("link_return_finished")
    if interruption != "explicit":
        f["wait"]("loaded", lambda event: event["url"] == target, timeout=10)
    for _ in range(3):
        f["send"](action="shell_control", operation="finished")
        f["wait"]("shell_control")
    f["send"](action="shell_status")
    assert f["wait"]("shell_status")["pending"] is (interruption == "explicit")
    current = f["state"](lambda _: True)
    assert current["visible"] is True
    assert current["state"]["generation"] == expected + 1
    assert f["js"]("return location.href;") == target
    time.sleep(0.2)
    assert f["js"]("return location.href;") == target
    assert sum(event.get("event") == "link_return_held" for event in f["history"]) == 1
    if interruption != "new_link":
        assert sum(event.get("event") == "link_return_finished" for event in f["history"]) == 1


@pytest.mark.skipif(os.environ.get("DESKTOP_FIXTURE_UPDATER") != "1", reason="requires signed updater transport fixture build")
@pytest.mark.parametrize("production_shell_chain", [
    {"sse": True, "legacy_install_grant": False},
    {"sse": False, "legacy_install_grant": True},
], indirect=True)
def test_production_local_update_post_reaches_connection_while_remote(production_shell_chain):
    f = production_shell_chain
    login(f)
    select_studio(f)
    open_footer(f)
    assert trusted_action(f, {"type": "update_check"})["ok"]
    checked = f["state"](lambda state: state["desktop"]["update"]["phase"] == "available")
    # A released remote-install grant is never migrated into automatic policy.
    assert checked["state"]["desktop"]["update"]["autoUpdate"] is False
    f["send"](action="shell_status")
    status = f["wait"]("shell_status")
    assert Path(status["data"]).is_relative_to(f["root"])
    assert status["notifications_enabled"] is True
    assert status["mini_window"] is False
    if f["options"]["legacy_install_grant"]:
        f["send"](action="shell_control", operation="notifications", enabled=False)
        f["wait"]("shell_control")
    session = requests.Session()
    session.trust_env = False
    token = session.get(f["local"] + "/api/csrf-token", timeout=5).json()["csrf_token"]
    response = session.post(
        f["local"] + "/api/desktop/update/request",
        json={"target_version": f["updater"]["version"], "confirm_interrupt": True},
        headers={"Origin": f["local"], "X-Vibe-CSRF-Token": token},
        timeout=15,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"] is True
    assert result["delivery"] == "install"
    remote = f["state"](lambda state: (state["desktop"]["update"].get("remoteRequest") or {}).get("phase") == "downloading")
    receipt = remote["state"]["desktop"]["update"]["remoteRequest"]
    assert receipt["requestId"] == result["request_id"]
    assert receipt["target"] == f["updater"]["version"]
    assert f["updater"]["requested"].wait(timeout=5)
    # The explicit click authorizes this ticket regardless of the legacy grant
    # or automatic policy. Expire it before releasing inert signed bytes.
    from vibe.desktop_update_request import expire_desktop_update_request
    assert expire_desktop_update_request(result["request_id"])
    expired = json.loads((Path(status["data"]) / "desktop-update-request.json").read_text())
    assert (expired["request_id"], expired["target"], expired["state"]) == (
        result["request_id"], f["updater"]["version"], "expired",
    )
    f["updater"]["release"].set()
    f["state"](lambda state: (state["desktop"]["update"].get("remoteRequest") or {}).get("phase") == "expired")
    if f["options"]["sse"]:
        assert any(event.get("event") == "sse_update_nudge" for event in f["history"])
    assert not any(update["phase"] in ("installing", "installed") for update in f["updates"])
    if f["options"]["legacy_install_grant"]:
        assert json.loads((f["root"] / "notifications.json").read_text()) == {"enabled": False}


def test_production_notification_output_is_fenced_without_retiring_local_control(production_shell_chain, monkeypatch):
    """Actual SSE, actual detail response, native output gates and stop/rebind.

    The old transport abort hid delayed output risks. Keep that transport alive
    and prove a lookup cannot be revived by a later local presentation.
    """
    f = production_shell_chain
    login(f)
    f["state"](lambda state: state["account"]["status"] == "signed_in")
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    f["send"](action="shell_control", operation="hide")
    f["wait"]("shell_control")
    broker = f["runtime"].broker

    def approval(identifier):
        broker.publish("vaults.updated", {"request_id": identifier, "request_status": "pending"})

    approval("positive-before")
    f["wait"]("notification_output")
    start = len(f["history"])
    entered, release = threading.Event(), threading.Event()
    handler = f["runtime"].RequestHandlerClass
    original = handler.end_headers

    def end_headers(request):
        if request.path == "/api/harness/runs/fixture-delayed":
            assert b"404" in request._headers_buffer[0]
            entered.set()
            assert release.wait(timeout=10)
        return original(request)

    monkeypatch.setattr(handler, "end_headers", end_headers)
    broker.publish("runs.updated", {"run_id": "fixture-delayed", "status": "succeeded", "run_type": "watch"})
    assert entered.wait(timeout=5)
    began = time.monotonic()
    try:
        select_studio(f)
        f["send"](action="shell_control", operation="hide")
        f["wait"]("shell_control")
        approval("suppressed-remote")
        f["send"](action="shell_status")
        status = f["wait"]("shell_status")
        assert status["notifications_enabled"] is True
        assert status["mini_window"] is False
        f["send"](action="escape")
        assert f["wait"]("escaped")["ok"]
        f["send"](action="shell_control", operation="hide")
        f["wait"]("shell_control")
        # Stay inside the production five-second detail timeout so this
        # specifically exercises late completion after returning local.
        assert time.monotonic() - began < 4.5
    finally:
        release.set()
    time.sleep(0.2)
    approval("suppressed-remote")  # suppressed events are consumed, not queued
    time.sleep(0.2)
    assert not [event for event in f["history"][start:] if event.get("event") == "notification_output"]
    approval("positive-after")
    f["wait"]("notification_output")

    select_studio(f)
    f["send"](action="shell_control", operation="stop")
    f["wait"]("shell_control")
    stopped = sum(event.get("event") == "mailbox_poll" for event in f["history"])
    time.sleep(5.2)
    assert sum(event.get("event") == "mailbox_poll" for event in f["history"]) == stopped
    connections = f["runtime"].streams["native_connections"]
    replacement = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=replacement.serve_forever, daemon=True).start()
    try:
        origin = f"http://127.0.0.1:{replacement.server_port}"
        for _ in range(3):
            f["send"](action="shell_control", operation="rebind", origin=origin)
            f["wait"]("shell_control")
        deadline = time.monotonic() + 5
        while f["runtime"].streams["native_connections"] == connections:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert f["runtime"].streams["native_connections"] == connections + 1
        f["send"](action="shell_control", operation="stop")
        f["wait"]("shell_control")
    finally:
        replacement.shutdown()
        replacement.server_close()
    f["send"](action="shell_status")
    assert f["wait"]("shell_status")["notifications_enabled"] is True


def login(f):
    f["wait"]("loaded", lambda event: "account/index.html" in event["url"])
    deadline = time.monotonic() + 10
    while True:
        fields = f["js"](
            "return Array.from(document.querySelectorAll('input')).map(input=>({type:input.type,name:input.name,placeholder:input.placeholder}));"
        )
        if fields:
            break
        assert time.monotonic() < deadline, fields
        time.sleep(0.1)
    assert (
        f["js"](
            "const input=document.querySelector('input[type=email]'); if(!input) return {error:document.body.innerText}; Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'desktop@example.test'); input.dispatchEvent(new Event('input',{bubbles:true})); return true;"
        )
        is True
    )
    assert f["js"]("document.querySelector('form').requestSubmit(); return true;") is True
    f["state"](lambda state: state["account"]["status"] == "verifying")
    assert (
        f["js"](
            "const input=document.querySelector('input'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'123456'); input.dispatchEvent(new Event('input',{bubbles:true})); return true;"
        )
        is True
    )
    f["js"]("document.querySelector('form').requestSubmit(); return true;")


def test_actual_bundled_account_login_registers_through_native_and_r1(native_chain):
    f = native_chain
    login(f)
    state = f["state"](lambda state: state["account"]["status"] == "signed_in")
    assert state["state"]["enrollment"]["status"] in ("paired_this_account", "paired_tunnel_unavailable"), state
    assert state["state"]["local"]["name"] == "小明的 Mac"
    assert state["credential_count"] == 1
    assert len(state["state"]["catalog"]["instances"]) == 2, state
    studio = next(
        instance for instance in state["state"]["catalog"]["instances"] if instance["id"] == f["studio"]["id"]
    )
    f["send"](action="present", view="instances")
    f["wait"]("loaded", lambda event: "view=instances" in event["url"])
    deadline = time.monotonic() + 10
    while True:
        ready = f["js"](
            "return Array.from(document.querySelectorAll('main button')).some(button=>button.innerText.includes("
            + json.dumps(studio["name"])
            + "));"
        )
        if ready:
            break
        assert time.monotonic() < deadline
        time.sleep(0.1)
    f["js"](
        "Array.from(document.querySelectorAll('main button')).find(button=>button.innerText.includes("
        + json.dumps(studio["name"])
        + ")).click(); return true;"
    )
    f["state"](lambda state: state["consent"] is not None)
    f["js"]("document.querySelector('main button').click(); return true;")
    selected = f["state"](lambda state: state["connection"]["committed"]["kind"] == "remote")
    assert selected["state"]["connection"]["committed"]["instance_id"] == studio["id"]
    assert selected["state"]["remote_auxiliary"] == "disabled_incomplete"
    # Native projection must not carry account cookies or the full catalog.
    projection = f["js"]("return window.__AVIBE_DESKTOP_CLIENT_STATE__;")
    assert "catalog" not in projection
    assert projection["connection"]["committed"]["instance_id"] == studio["id"]
    denied = f["js"](
        "try { await window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:{version:1,request_id:'remote-denied',expected_generation:0,action:{type:'sign_out'}}}); return false; } catch { return true; }"
    )
    assert denied is True
    f["send"](action="present", view="instances")
    f["wait"]("loaded", lambda event: "view=instances" in event["url"])
    f["ready"]("return Array.from(document.querySelectorAll('main button')).some(button=>button.innerText.includes('小明的 Mac'));")
    f["js"]("Array.from(document.querySelectorAll('main button')).find(button=>button.innerText.includes('小明的 Mac')).click(); return true;")
    f["wait"]("loaded", lambda event: event["url"].startswith("http://127.0.0.1:"))
    returned = f["state"](lambda state: state["connection"]["committed"]["kind"] == "local")
    assert returned["state"]["account"]["status"] == "signed_in"
    assert returned["state"]["remote_auxiliary"] == "local"

    # Re-enter the real remote store and sign out directly from it. This must
    # erase the instance store as well as native credentials before local return.
    select_studio(f)
    assert f["state"](lambda state: state["connection"]["committed"]["kind"] == "remote")["instance_stores"] == 1
    sign_out(f)
    ended = f["state"](lambda state: state["account"]["status"] == "signed_out")
    assert ended["credential_count"] == 0
    assert ended["instance_stores"] == 0
    assert ended["state"]["connection"]["committed"]["kind"] == "local"
    descriptor = requests.get(f["local"] + "/api/desktop/local-instance", timeout=5).json()
    assert descriptor["pairing"] is not None
    assert descriptor["remote_access"]["requested"] is True


def select_studio(f):
    f["send"](action="present", view="instances")
    f["wait"]("loaded", lambda event: "view=instances" in event["url"])
    selector = studio_selector(f)
    f["ready"]("return Boolean(" + selector + ");")
    f["js"](selector + ".click(); return true;")
    state = f["state"](lambda state: state["consent"] is not None or state["connection"]["committed"]["kind"] == "remote")
    if state["state"]["consent"] is not None:
        f["ready"]("return document.body.innerText.includes('openid');")
        f["js"]("document.querySelector('main button').click(); return true;")
    return f["state"](lambda state: state["connection"]["committed"]["kind"] == "remote")


def sign_out(f):
    f["send"](action="present", view="sign-out")
    f["wait"]("loaded", lambda event: "view=sign-out" in event["url"])
    f["ready"]("return document.querySelectorAll('main button').length === 2;")
    f["js"]("document.querySelector('main button').click(); return true;")
    f["wait"]("loaded", lambda event: event["url"].startswith("http://127.0.0.1:"))


def test_totp_wrong_csrf_and_late_account_result_cannot_recreate_signed_out_state(native_chain):
    f = native_chain
    control = requests.post(f["info"]["provider"] + "/fixture/control", json={"factor":"totp"}, timeout=5).json()
    before = requests.get(f["local"] + "/api/desktop/local-instance", timeout=5).json()
    f["send"](action="wrong_csrf")
    f["wait"]("control_ready")
    login(f)
    gated = f["state"](lambda state: state["account"]["status"] == "second_factor_required")
    assert gated["credential_count"] == 0
    assert requests.get(f["local"] + "/api/desktop/local-instance", timeout=5).json() == before
    f["ready"]("return Boolean(document.querySelector('#factor-code'));")
    f["js"]("const input=document.querySelector('#factor-code'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input," + json.dumps(control["factor_code"]) + "); input.dispatchEvent(new Event('input',{bubbles:true})); return true;")
    f["js"]("document.querySelector('form').requestSubmit(); return true;")
    failed = f["state"](lambda state: state["account"]["status"] == "signed_in")
    assert failed["state"]["enrollment"]["status"] == "failed"
    assert failed["credential_count"] == 1
    # Actual R1 rejected the mismatched native header before prepare could
    # allocate identity, claim an operation, or call B2 ensure/redeem.
    assert requests.get(f["local"] + "/api/desktop/local-instance", timeout=5).json() == before
    counts = requests.post(f["info"]["provider"] + "/fixture/control", json={}, timeout=5).json()
    assert counts["devices"] == control["devices"] == 1
    assert counts["instances"] == control["instances"] == 1
    f["send"](action="present", view="instances")
    f["wait"]("loaded", lambda event: "view=instances" in event["url"])
    selector = studio_selector(f)
    f["ready"]("return Boolean(" + selector + ");")
    f["send"](action="hold", path="/api/mobile/session")
    f["wait"]("control_ready")
    f["js"](selector + ".click(); return true;")
    held = f["wait"]("http_held")
    assert held["status"] == 200
    sign_out(f)
    f["send"](action="release")
    late = f["wait"]("reply", lambda event: event["error"] == "stale_generation")
    assert late["error"] == "stale_generation"
    final = f["state"](lambda state: state["account"]["status"] == "signed_out")
    assert final["credential_count"] == 0
    assert final["instance_stores"] == 0
    assert final["state"]["catalog"]["instances"] == []
    assert final["state"]["connection"]["committed"]["kind"] == "local"


def studio_selector(f):
    state = f["state"](lambda state: state["account"]["status"] == "signed_in")["state"]
    name = next(item["name"] for item in state["catalog"]["instances"] if item["id"] == f["studio"]["id"])
    return "Array.from(document.querySelectorAll('main button')).find(button=>button.innerText.includes(" + json.dumps(name) + "))"


@pytest.mark.parametrize("different_instance", [False, True])
def test_recovered_account_window_and_delayed_event_keep_runtime_state_private(native_chain, different_instance):
    # A normal remote build does not expose the bug: its initialization script
    # was created with account=false. Exercise the actual rollback-created view
    # and defer one real full-state WKWebView eval across its next navigation.
    f = native_chain
    extra = extra_log = None
    try:
        login(f)
        selected = select_studio(f)
        committed = selected["state"]["connection"]["committed"]
        target = {"id": committed["instance_id"], "public_url": committed["canonical_origin"]}
        if different_instance:
            extra, extra_log, target = peer.start(f["root"] / "other-studio", f["info"], f["root"] / "mappings.json")
        f["send"](action="present", view="instances")
        f["wait"]("loaded", lambda event: "view=instances" in event["url"])
        f["ready"]("return Boolean(window.__TAURI_INTERNALS__);")
        generation = f["state"](lambda state: state["account"]["status"] == "signed_in")["state"]["generation"]
        refreshed = f["js"]("return await window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
                            + json.dumps({"version": 1, "request_id": "projection-refresh", "expected_generation": generation,
                                          "action": {"type": "refresh_instances"}}) + "});")
        assert refreshed["ok"]
        catalog_target = next(row for row in refreshed["state"]["catalog"]["instances"] if row["id"] == target["id"])
        f["document"].arm(catalog_target["host"], "/")
        f["send"](action="eval", script="window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
                  + json.dumps({"version": 1, "request_id": "projection-select", "expected_generation": generation,
                                "action": {"type": "select_instance", "instance_id": target["id"]}}) + "});")
        if different_instance:
            f["state"](lambda state: state["consent"] is not None)
            f["ready"]("return document.body.innerText.includes('openid');")
            f["js"]("document.querySelector('main button').click(); return true;")
        assert f["document"].entered.wait(timeout=15)
        f["send"](action="local_unavailable")
        f["wait"]("local_unavailable")
        f["wait"]("loaded", lambda event: "account/index.html" in event["url"])
        restored = f["state"](lambda state: state["connection"]["pending"] is None)
        assert restored["state"]["connection"]["committed"] == committed
        f["document"].release.set()
        control = f["js"]("return await window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
                          + json.dumps({"version": 1, "request_id": "projection-account-control",
                                        "expected_generation": restored["state"]["generation"], "action": {"type": "get_state"}}) + "});")
        assert control["ok"] and len(control["state"]["catalog"]["instances"]) == (3 if different_instance else 2)
        f["send"](action="hold_account_event")
        f["wait"]("account_event_held")
        f["send"](action="eval", script="window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
                  + json.dumps({"version": 1, "request_id": "projection-cancel",
                                "expected_generation": restored["state"]["generation"], "action": {"type": "cancel"}}) + "});")
        f["wait"]("loaded", lambda event: event["url"].startswith(committed["canonical_origin"]))
        initial = f["js"]("window.fixtureStates=[]; window.addEventListener('avibe:desktop-client-state',e=>window.fixtureStates.push(e.detail)); return window.__AVIBE_DESKTOP_CLIENT_STATE__;")
        assert not {"catalog", "enrollment", "consent"}.intersection(initial)
        assert initial["connection"]["committed"] == committed
        f["send"](action="release_account_event")
        f["wait"]("account_event_released")
        received = f["js"]("return window.fixtureStates;")
        assert all(not {"catalog", "enrollment", "consent"}.intersection(state) for state in received), received
        assert f["js"]("return location.origin;") == committed["canonical_origin"]
    finally:
        if extra is not None:
            if extra.poll() is None:
                extra.terminate()
                extra.wait(timeout=10)
            extra_log.close()


@pytest.mark.parametrize("escape", ["sign_out", "return_local"])
@pytest.mark.parametrize("interruption", ["disappear", "rebind", "explicit_escape"])
def test_local_replacement_completes_under_current_presentation_owner(local_held_chain, escape, interruption):
    # Offline-start tests never invalidate the already-created hidden local
    # replacement. Hold its actual HTTP response before that terminal boundary.
    f = local_held_chain
    login(f)
    select_studio(f)
    f["send"](action="present", view="sign-out")
    f["wait"]("loaded", lambda event: "view=sign-out" in event["url"])
    f["ready"]("return document.querySelectorAll('main button').length === 2;")
    armed, entered, release = f["local_hold"]
    armed.set()
    if escape == "sign_out":
        f["send"](action="eval", script="document.querySelector('main button').click();")
    else:
        f["send"](action="escape")
    assert entered.wait(timeout=15)
    f["send"](action="runtime_offline")
    f["wait"]("runtime_offline")
    if interruption != "disappear":
        f["send"](action="runtime_rebind")
        f["wait"]("runtime_rebind")
    if interruption == "explicit_escape":
        f["send"](action="escape")
    release.set()
    destination = "tauri://localhost/" if interruption == "disappear" else f["local"] + "/"
    f["wait"]("loaded", lambda event: event["url"] == destination)
    f["ready"]("return location.href === " + json.dumps(destination) + ";")
    final = f["state"](lambda state: state["connection"]["committed"]["kind"] == "local")
    deadline = time.monotonic() + 5
    while not final["visible"] and time.monotonic() < deadline:
        time.sleep(0.02)
        final = f["state"](lambda state: state["connection"]["committed"]["kind"] == "local")
    assert final["visible"]
    assert final["state"]["account"]["status"] == ("signed_out" if escape == "sign_out" else "signed_in")
    assert final["credential_count"] == (0 if escape == "sign_out" else 1)
    assert final["instance_stores"] == 0


def test_rollback_retains_previous_material_across_two_window_interruptions(native_chain):
    f = native_chain
    login(f)
    selected = select_studio(f)
    committed = selected["state"]["connection"]["committed"]
    f["send"](action="present", view="instances")
    f["wait"]("loaded", lambda event: "view=instances" in event["url"])
    selector = studio_selector(f)
    f["ready"]("return Boolean(" + selector + ");")
    f["send"](action="interrupt_windows", count=2)
    f["wait"]("control_ready")
    f["js"](selector + ".click(); return true;")
    f["wait"]("window_lifecycle_interrupted", lambda event: event["remaining"] == 0)
    f["wait"]("loaded", lambda event: "account/index.html" in event["url"])
    final = f["state"](lambda state: state["connection"]["pending"] is None)
    assert final["state"]["connection"]["committed"] == committed
    assert final["credential_count"] == final["instance_stores"] == 1
    f["ready"]("return Boolean(window.__TAURI_INTERNALS__);")
    control = f["js"]("return await window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:{version:1,request_id:'twice-restored',expected_generation:0,action:{type:'get_state'}}});")
    assert control["ok"] and control["state"]["connection"]["committed"] == committed


@pytest.mark.parametrize("interruption", ["escape", "local_unavailable"])
def test_staged_document_cannot_commit_after_local_lifecycle_or_escape(native_chain, interruption):
    # Unit transport tests cannot see the physical gap between cookie projection
    # and main-document readiness. Hold a real Runtime response in that gap.
    f = native_chain
    login(f)
    selected = select_studio(f)
    committed = selected["state"]["connection"]["committed"]
    f["send"](action="present", view="instances")
    f["wait"]("loaded", lambda event: "view=instances" in event["url"])
    selector = studio_selector(f)
    f["ready"]("return Boolean(" + selector + ");")
    f["document"].arm(committed["canonical_origin"].removeprefix("https://"), "/")
    f["js"](selector + ".click(); return true;")
    assert f["document"].entered.wait(timeout=15)
    pending = f["state"](lambda state: state["generation"] > selected["state"]["generation"])
    assert pending["state"]["connection"]["committed"] == committed
    f["send"](action=interruption)
    if interruption == "escape":
        assert f["wait"]("escaped")["ok"] is True
        f["ready"]("return window.location.origin === " + json.dumps(f["local"]) + ";")
        ended = f["state"](lambda state: state["connection"]["committed"]["kind"] == "local")
        assert ended["instance_stores"] == 0
    else:
        f["wait"]("local_unavailable")
        f["wait"]("loaded", lambda event: "account/index.html" in event["url"])
        ended = f["state"](lambda state: state["connection"]["pending"] is None)
        assert ended["state"]["connection"]["committed"] == committed
        assert ended["instance_stores"] == 1
        f["ready"]("return Boolean(window.__TAURI_INTERNALS__);")
        # The restored bundled page must still have a usable trusted bridge.
        reply = f["js"]("return await window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:{version:1,request_id:'restored-state',expected_generation:0,action:{type:'get_state'}}});")
        assert reply["ok"] is True
        assert reply["state"]["connection"]["committed"] == committed
    f["document"].release.set()
    assert ended["credential_count"] == 1
    assert ended["state"]["generation"] > pending["state"]["generation"]
    assert ended["state"]["account"]["status"] == "signed_in"


def open_footer(f):
    f["ready"]("return Boolean(document.querySelector('[data-version-trigger]'));")
    # This one click only presents the trusted panel; navigation never installs.
    f["send"](action="eval", script="document.querySelector('[data-version-trigger]').click();")
    f["wait"]("loaded", lambda event: "account/index.html?view=updates" in event["url"])
    f["ready"]("return Boolean(window.__TAURI_INTERNALS__);")


def trusted_action(f, action, generation=None):
    if generation is None:
        generation = f["state"](lambda _: True)["state"]["generation"]
    request = {"version": 1, "request_id": str(uuid.uuid4()), "expected_generation": generation, "action": action}
    return f["js"]("return await window.__TAURI_INTERNALS__.invoke('desktop_client_request', {request:"
                   + json.dumps(request) + "});")


@pytest.mark.skipif(os.environ.get("DESKTOP_FIXTURE_UPDATER") != "1", reason="requires signed updater transport fixture build")
def test_actual_remote_footer_opens_trusted_panel_and_fences_download_completion(native_chain):
    # AUTH-SETUP-177/178: the old dialog fixture did not exercise panel actions.
    f = native_chain
    login(f)
    f["state"](lambda state: state["account"]["status"] == "signed_in")
    select_studio(f)
    denied = f["js"]("try { await window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
                     "{version:1,request_id:'remote-update-denied',expected_generation:0,action:{type:'update_check'}}});"
                     " return false; } catch { return true; }")
    assert denied is True
    open_footer(f)
    assert not f["updater"]["requested"].is_set()
    assert trusted_action(f, {"type": "update_check"})["ok"]
    available = f["state"](lambda state: state["desktop"]["update"]["phase"] == "available")["state"]
    before = available["desktop"]["update"]
    for enabled in (True, False):
        reply = trusted_action(f, {"type": "update_set_auto_update", "enabled": enabled})
        assert reply["ok"]
        update = reply["state"]["desktop"]["update"]
        assert update["autoUpdate"] is enabled
        assert (update["phase"], update["latest"], update["checkedAt"]) == (
            before["phase"], before["latest"], before["checkedAt"],
        )
    assert trusted_action(f, {"type": "update_set_prereleases", "enabled": True})["ok"]
    available = f["state"](lambda state: state["desktop"]["update"]["phase"] == "available")["state"]
    assert available["desktop"]["update"]["prereleases"] is True
    assert not trusted_action(f, {"type": "update_install", "target_version": "0.0.0"})["ok"]
    assert not f["updater"]["requested"].is_set()

    # Stale actions cannot download. Escape also fences an accepted download.
    old_generation = available["generation"]
    f["send"](action="local_unavailable")
    f["wait"]("local_unavailable")
    install = {"type": "update_install", "target_version": f["updater"]["version"]}
    stale = trusted_action(f, install, old_generation)
    assert stale["error"]["code"] == "stale_generation"
    assert not f["updater"]["requested"].is_set()
    assert trusted_action(f, install)["ok"]
    assert f["updater"]["requested"].wait(timeout=10)
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"] is True
    f["updater"]["release"].set()
    f["state"](lambda state: state["desktop"]["update"]["phase"] == "available")
    assert not any(update["phase"] in ("installing", "installed") for update in f["updates"])
    assert f["state"](lambda state: state["connection"]["committed"]["kind"] == "local")["credential_count"] == 1


@pytest.mark.parametrize("escape", ["return_local", "sign_out"])
def test_remote_escape_without_local_runtime_finishes_visible_owned_bootstrap(native_chain, escape):
    f = native_chain
    login(f)
    select_studio(f)
    f["send"](action="runtime_offline")
    f["wait"]("runtime_offline")
    if escape == "return_local":
        f["send"](action="escape")
        assert f["wait"]("escaped")["ok"] is True
        # The load event precedes the completed escape reply and was consumed
        # while awaiting it. Observe the actual destination after completion.
        assert f["js"]("return location.href;") == "tauri://localhost/"
    else:
        f["send"](action="present", view="sign-out")
        f["wait"]("loaded", lambda event: "view=sign-out" in event["url"])
        f["ready"]("return document.querySelectorAll('main button').length === 2;")
        f["send"](action="eval", script="document.querySelector('main button').click();")
        f["wait"]("loaded", lambda event: event["url"] == "tauri://localhost/")
    final = f["state"](lambda state: state["connection"]["committed"]["kind"] == "local")
    deadline = time.monotonic() + 5
    while not final["visible"]:
        assert time.monotonic() < deadline
        final = f["state"](lambda _: True)
    assert final["instance_stores"] == 0
    assert final["credential_count"] == (0 if escape == "sign_out" else 1)
    assert final["state"]["enrollment"]["error_code"] is None


def test_pending_remote_document_cannot_present_until_public_commit(native_chain):
    f = native_chain
    login(f)
    selected = select_studio(f)
    target = selected["state"]["connection"]["committed"]
    html = requests.get(f["local"] + "/", timeout=5).text
    script = re.search(r'<script[^>]*src="([^"]+)"', html).group(1)
    f["send"](action="present", view="instances")
    f["wait"]("loaded", lambda event: "view=instances" in event["url"])
    selector = studio_selector(f)
    f["ready"]("return Boolean(" + selector + ");")
    f["document"].arm(target["canonical_origin"].removeprefix("https://"), script)
    f["js"](selector + ".click(); return true;")
    assert f["document"].entered.wait(timeout=15)
    f["send"](action="presentation_status")
    pending = f["wait"]("presentation_status")
    assert pending["url"].startswith(target["canonical_origin"])
    assert pending["allowed"] is False
    f["send"](action="present", view="account")
    f["send"](action="presentation_status")
    retained = f["wait"]("presentation_status")
    assert retained["url"] == pending["url"]
    assert retained["allowed"] is False
    f["document"].release.set()
    f["wait"]("loaded", lambda event: event["url"].startswith(target["canonical_origin"]))
    deadline = time.monotonic() + 10
    while True:
        f["send"](action="presentation_status")
        if f["wait"]("presentation_status")["allowed"]:
            break
        assert time.monotonic() < deadline
        time.sleep(0.1)


@pytest.mark.skipif(os.environ.get("DESKTOP_FIXTURE_UPDATER") != "1", reason="requires signed updater transport fixture build")
@pytest.mark.parametrize("boundary", ["replaced", "expired"])
def test_explicit_remote_update_needs_no_host_prompt_and_rechecks_its_ticket(native_chain, boundary):
    f = native_chain
    login(f)
    select_studio(f)
    now = datetime.now(timezone.utc)
    request = {"schema_version": 1, "request_id": str(uuid.uuid4()), "target": f["updater"]["version"], "state": "pending",
               "created_at": (now-timedelta(seconds=1)).isoformat(), "expires_at": (now+timedelta(minutes=5)).isoformat()}
    f["send"](action="remote_update", request=request)
    f["wait"]("remote_dispatched")
    assert f["updater"]["requested"].wait(timeout=10)
    download = f["state"](lambda state: (state["desktop"]["update"].get("remoteRequest") or {}).get("phase") == "downloading")
    receipt = download["state"]["desktop"]["update"]["remoteRequest"]
    assert receipt["requestId"] == request["request_id"]
    assert receipt["target"] == request["target"]
    assert not any((update.get("remoteRequest") or {}).get("phase") == "awaiting_host" for update in f["updates"])
    if boundary == "expired":
        changed = {**request, "state": "expired"}
    else:
        changed = {**request, "request_id": str(uuid.uuid4())}
    f["send"](action="remote_update", request=changed)
    f["wait"]("remote_dispatched")
    f["updater"]["release"].set()
    ended = f["state"](lambda state: (state["desktop"]["update"].get("remoteRequest") or {}).get("phase") in {"expired", "failed"})
    assert ended["state"]["desktop"]["update"]["remoteRequest"]["requestId"] == request["request_id"]
    assert not any(update["phase"] in ("installing", "installed") for update in f["updates"])
