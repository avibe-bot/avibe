"""AUTH-SETUP-171/175/176/177: real U1 + native + B1/B2 + R1 chain.

The only substitutions are the account provider, external tunnel service,
credential storage, and sockets. Runtime identity/OIDC/authorization and every
producer handler execute their candidate code. No running Avibe is contacted.
"""

from pathlib import Path
import json
import os
import queue
import re
from datetime import datetime, timedelta, timezone
import uuid
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
    native_log = (tmp_path / "native.log").open("w")

    def collect():
        for line in native.stdout:
            native_log.write(line)
            native_log.flush()
            try:
                value = json.loads(line)
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
    f["js"]("document.querySelector('[data-version-trigger]').click(); return true;")
    f["ready"]("return Boolean(document.querySelector('[data-update-menu] button'));")
    # This click navigates to the native presentation scheme. Sending the
    # observation scheme in the same turn would replace that navigation.
    f["send"](action="eval", script="document.querySelector('[data-update-menu] button').click();")

def native_dialog(f, expected=3):
    deadline = time.monotonic() + 15
    while True:
        f["send"](action="dialog")
        value = f["wait"]("dialog")
        if len(value["buttons"]) == expected:
            assert set(value["buttons"]) == ({"Download and Install", "Skip This Version", "Later"} if expected == 3 else {"Download and Install", "Later"})
            return value["buttons"]
        assert time.monotonic() < deadline, {"dialog": value, "updates": f["updates"]}
        time.sleep(0.1)


@pytest.mark.skipif(os.environ.get("DESKTOP_FIXTURE_UPDATER") != "1", reason="requires signed updater transport fixture build")
def test_actual_remote_footer_opens_native_confirmation_and_fences_download_completion(native_chain):
    f = native_chain
    login(f)
    f["state"](lambda state: state["account"]["status"] == "signed_in")
    select_studio(f)

    # The real U1 CTA is a presentation request. It must reach the actual native
    # three-choice dialog, and Later must cause no artifact download.
    open_footer(f)
    buttons = native_dialog(f)
    f["send"](action="dialog", choice=buttons.index("Later"))
    f["wait"]("dialog")
    f["wait"]("update", lambda event: event["state"]["phase"] == "available")
    assert not f["updater"]["requested"].is_set()

    # A confirmation that survives a generation change cannot start download.
    open_footer(f)
    buttons = native_dialog(f)
    f["send"](action="local_unavailable")
    f["wait"]("local_unavailable")
    f["send"](action="dialog", choice=buttons.index("Download and Install"))
    f["wait"]("dialog")
    f["wait"]("update", lambda event: event["state"]["phase"] == "available")
    assert not f["updater"]["requested"].is_set()

    # The same real owner must fence an already accepted download whose bytes
    # arrive after local escape. The inert signed fixture is never installed.
    open_footer(f)
    buttons = native_dialog(f)
    f["send"](action="dialog", choice=buttons.index("Download and Install"))
    f["wait"]("dialog")
    assert f["updater"]["requested"].wait(timeout=10)
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"] is True
    f["updater"]["release"].set()
    f["wait"]("update", lambda event: event["state"]["phase"] == "available")
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
@pytest.mark.parametrize("boundary", ["accept", "stale", "expired"])
def test_awaiting_remote_update_keeps_exact_ticket_through_native_host_approval(native_chain, boundary):
    f = native_chain
    login(f)
    select_studio(f)
    now = datetime.now(timezone.utc)
    request = {"schema_version": 1, "request_id": str(uuid.uuid4()), "target": f["updater"]["version"], "state": "pending",
               "created_at": (now-timedelta(seconds=1)).isoformat(), "expires_at": (now+timedelta(minutes=5)).isoformat()}
    f["send"](action="remote_update", request=request)
    f["wait"]("remote_dispatched")
    awaiting = f["wait"]("update", lambda e: (e["state"].get("remoteRequest") or {}).get("phase") == "awaiting_host")
    assert awaiting["state"]["remoteInstallAllowed"] is False
    assert awaiting["state"]["remoteRequest"]["requestId"] == request["request_id"]
    open_footer(f)
    buttons = native_dialog(f, 2)
    f["send"](action="dialog", choice=buttons.index("Later"))
    f["wait"]("dialog")
    assert not f["updater"]["requested"].is_set()
    open_footer(f)
    buttons = native_dialog(f, 2)
    if boundary == "stale":
        f["send"](action="local_unavailable")
        f["wait"]("local_unavailable")
    elif boundary == "expired":
        request["state"] = "expired"
        f["send"](action="remote_update", request=request)
        f["wait"]("remote_dispatched")
        f["wait"]("update", lambda e: (e["state"].get("remoteRequest") or {}).get("phase") == "expired")
    f["send"](action="dialog", choice=buttons.index("Download and Install"))
    f["wait"]("dialog")
    if boundary == "accept":
        assert f["updater"]["requested"].wait(timeout=10)
        download = f["wait"]("update", lambda e: (e["state"].get("remoteRequest") or {}).get("phase") == "downloading")
        assert download["state"]["remoteRequest"]["requestId"] == request["request_id"]
        assert download["state"]["remoteRequest"]["target"] == request["target"]
        f["send"](action="escape")
        assert f["wait"]("escaped")["ok"] is True
        f["updater"]["release"].set()
        f["wait"]("update", lambda e: (e["state"].get("remoteRequest") or {}).get("phase") == "failed")
    else:
        # A later state command orders observation after the dialog callback.
        f["state"](lambda _: True)
        assert not f["updater"]["requested"].is_set()
    assert not any(update["phase"] in ("installing", "installed") for update in f["updates"])
