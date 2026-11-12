"""AUTH-SETUP-171/175/176/177: real U1 + native + B1/B2 + R1 chain.

The only substitutions are the account provider, external tunnel service,
credential storage, and sockets. Runtime identity/OIDC/authorization and every
producer handler execute their candidate code. No running Avibe is contacted.
"""

from pathlib import Path
import json
import os
import queue
import subprocess
import threading
import time

import pytest
import requests

from tests.test_desktop_local_instance import desktop_host as desktop_host
from tests.test_pairing_recovery import pairing_host as pairing_host

import backend
import proxy
import runtime as runtime_fixture
import peer


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
    tunnel, tunnel_url, fingerprint = proxy.start(tmp_path, mapping_file)
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
    native_log = (tmp_path / "native.log").open("w")

    def collect():
        for line in native.stdout:
            native_log.write(line)
            native_log.flush()
            try:
                events.put(json.loads(line))
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
        }
    finally:
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
        native_log.close()
        studio_log.close()
        log.close()
        print(f"native producer chain evidence: {tmp_path}")


def test_actual_bundled_account_login_registers_through_native_and_r1(native_chain):
    f = native_chain
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
