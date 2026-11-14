"""A4 owning native boundaries over isolated HTTP and actual WKWebView documents.

Uses the existing native probe (in-memory credential substitute, loopback-only
transport mapping) and fixture TLS proxy. Provider/control-plane replies are
synthetic; real NativeSession cookies, AccountFlow, Coordinator and Tauri
admission/cleanup execute. This does not claim a live provider or installed app.
"""

import http.server
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from urllib.parse import urlsplit

import pytest

import proxy


@pytest.fixture
def account_native(tmp_path):
    calls = []
    policy = {"grant": "ok", "factor": False}
    identity = {"account": {"id": "synthetic-account", "email": "user@example.test"}, "instances": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self):
            size = int(self.headers.get("content-length", "0"))
            raw = self.rfile.read(size) if size else b""
            body = json.loads(raw) if raw else None
            headers = {key.lower(): value for key, value in self.headers.items()}
            calls.append((self.command, self.path, headers, body))
            path = urlsplit(self.path).path
            status, value, cookie = 200, {}, None
            content_type = "application/json"
            if path == "/api/config/auth":
                value = {"supabase_url": "https://provider.fixture.test", "supabase_anon_key": "public-fixture"}
                if policy.get("failure") == "config":
                    status, value = 400, {"error": "synthetic_secret_lowercase"}
            elif path == "/auth/v1/token":
                grant = policy["grant"]
                if grant == "ok":
                    value = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}
                    cookie = "provider_only=synthetic; Path=/; Secure; HttpOnly"
                elif grant == "malformed":
                    value = "invalid-json"
                elif grant == "missing_token":
                    value = {"access_token": "synthetic-access"}
                else:
                    status = 429 if grant == "rate" else 400
                    value = {"error_code": "invalid_credentials" if grant == "invalid" else "unexpected",
                             "error_description": "secret-should-never-reach-reply"}
            elif path == "/auth/v1/otp":
                value = {}
            elif path == "/api/desktop/session":
                if policy.get("failure") == "session":
                    status, value = 400, {"error": "synthetic_secret_lowercase"}
                elif policy["factor"]:
                    status, value = 403, {"error": "two_factor_required", "email": "user@example.test",
                                          "gate": "challenge", "methods": ["totp", "passkey"]}
                    cookie = "account_gate=synthetic; Path=/; Secure; HttpOnly"
                else:
                    value, cookie = identity, "account=synthetic; Path=/; Secure; HttpOnly"
            elif path == "/api/two-factor/verify":
                value, cookie = {}, "account=synthetic; Path=/; Secure; HttpOnly"
            elif path == "/api/mobile/session":
                if self.command == "DELETE":
                    value = {}
                elif "account=synthetic" in headers.get("cookie", ""):
                    value = identity
                else:
                    status, value = 401, {"error": "signed_out"}
            elif path == "/api/desktop/local-instance":
                status, value = 404, {"error": "not_supported"}
            elif path == "/":
                content_type = "text/html"
                value = "<!doctype html><html><body>Isolated local Workbench</body></html>"
            else:
                status, value = 404, {"error": "fixture_unknown_path"}
            data = value.encode() if isinstance(value, str) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_DELETE = respond

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    mappings = tmp_path / "mappings.json"
    mappings.write_text(json.dumps({"avibe.bot": base, "provider.fixture.test": base}))
    tunnel, tunnel_url, fingerprint, hold = proxy.start(tmp_path, mappings)
    config = tmp_path / "native.json"
    config.write_text(json.dumps({"root": str(tmp_path), "backend": base, "provider": base,
                                 "local": base, "proxy": tunnel_url, "mappings": str(mappings)}))
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "AVIBE_FIXTURE_TLS_SHA256": fingerprint}
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                "AVIBE_HOME", "CODEX_HOME", "TMPDIR"):
        destination = tmp_path / key.lower()
        destination.mkdir()
        environment[key] = str(destination)
    executable = Path(os.environ["DESKTOP_NATIVE_PROBE"]).resolve()
    assert not any(part.endswith(".app") for part in executable.parts)
    process = subprocess.Popen([str(executable), str(config)], cwd=tmp_path, env=environment,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    events = queue.Queue()
    history = []
    log = (tmp_path / "native.log").open("w")

    def collect():
        for line in process.stdout:
            log.write(line)
            log.flush()
            try:
                event = json.loads(line)
                history.append(event)
                events.put(event)
            except ValueError:
                pass

    threading.Thread(target=collect, daemon=True).start()

    def send(**command):
        process.stdin.write(json.dumps(command) + "\n")
        process.stdin.flush()

    def wait(kind, predicate=lambda _: True, timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            try:
                value = events.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                pytest.fail(f"native {kind} timeout; evidence {tmp_path}")
            if value.get("event") == kind and predicate(value):
                return value

    def js(script):
        send(action="eval", script="Promise.resolve().then(async()=>{" + script
             + "}).then(value=>location='fixture-result://probe?value='+encodeURIComponent(JSON.stringify(value)))"
             + ".catch(error=>location='fixture-result://probe?value='+encodeURIComponent(JSON.stringify({error:String(error)})))")
        return wait("javascript")["value"]

    def state():
        send(action="state")
        return wait("state")

    def request(action, generation=None):
        generation = state()["state"]["generation"] if generation is None else generation
        envelope = {"version": 1, "request_id": "feedback-fixture", "expected_generation": generation, "action": action}
        return js("return await window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
                  + json.dumps(envelope) + "});")

    def present(view="account", locale="en"):
        suffix = f"?locale={locale}" if locale else ""
        send(action="eval", script=f"location='avibe://desktop/{view}{suffix}';")
        wait("loaded", lambda event: f"view={view}" in event["url"] and
             (locale is None or f"locale={locale}" in event["url"]))

    try:
        wait("native_ready")
        wait("loaded", lambda event: "/account/index.html" in event["url"])
        yield {"send": send, "wait": wait, "js": js, "state": state, "request": request, "present": present,
               "calls": calls, "policy": policy, "root": tmp_path, "history": history}
    finally:
        hold.release.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        tunnel.shutdown()
        log.close()
        # Retain exact wire shape without duplicating the synthetic password/tokens in logs.
        (tmp_path / "http-shapes.json").write_text(json.dumps([
            {"method": method, "path": path, "headers": sorted(headers), "fields": sorted(body or {})}
            for method, path, headers, body in calls
        ], indent=2))


def password(f):
    return f["request"]({"type": "sign_in_password", "email": "user@example.test", "password": "synthetic-password"})


def test_plain_admitted_signout_returns_native_receipt(account_native):
    """AUTH-SETUP-176: old producer accepted the route but returned no intent."""
    f = account_native
    f["present"]("sign-out", None)
    assert f["request"]({"type": "get_state"}).get("result", {}).get("type") == "sign_out_intent"


@pytest.mark.parametrize("factor", [None, "totp", "recovery"])
def test_password_native_http_session_and_cookie_isolation(account_native, factor):
    """AUTH-SETUP-171/177: native first factor, factor continuation and cookie authority."""
    f = account_native
    f["policy"]["factor"] = factor is not None
    reply = password(f)
    assert reply["ok"], reply
    expected = "second_factor_required" if factor else "signed_in"
    assert reply["state"]["account"]["status"] == expected
    grant = next(call for call in f["calls"] if call[1].startswith("/auth/v1/token"))
    method, path, headers, body = grant
    assert (method, path) == ("POST", "/auth/v1/token?grant_type=password")
    assert body == {"email": "user@example.test", "password": "synthetic-password"}
    assert headers["apikey"] == "public-fixture"
    assert "authorization" not in headers
    assert headers["origin"] == "https://provider.fixture.test"
    assert "cookie" not in headers
    session = next(call for call in f["calls"] if call[1] == "/api/desktop/session")
    assert session[0] == "POST"
    assert session[2]["origin"] == "https://avibe.bot"
    assert "provider_only" not in session[2].get("cookie", "")
    assert session[3] == {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}
    if factor:
        reply = f["request"]({"type": "verify_second_factor", "method": factor, "code": "synthetic-code"})
        assert reply["ok"] and reply["state"]["account"]["status"] == "signed_in", reply
        verification = next(call for call in f["calls"] if call[1] == "/api/two-factor/verify")
        assert "account_gate=synthetic" in verification[2]["cookie"]
        assert verification[3] == {"method": factor, "code": "synthetic-code"}
    assert f["state"]()["credential_count"] == 1
    assert f["js"]("return document.cookie;") == ""
    for forbidden in ("synthetic-password", "synthetic-access", "synthetic-refresh", "provider_only", "account_gate"):
        assert forbidden not in json.dumps(reply)


@pytest.mark.parametrize("grant,code", [
    ("invalid", "invalid_credentials"), ("rate", "rate_limited"), ("unknown", "request_failed"),
    ("malformed", "invalid_response"), ("missing_token", "invalid_response"),
])
def test_password_errors_leave_coherent_native_state_without_provider_details(account_native, grant, code):
    f = account_native
    assert f["request"]({"type": "request_otp", "email": "old@example.test"})["ok"]
    f["policy"]["grant"] = grant
    reply = password(f)
    assert not reply["ok"] and reply["error"]["code"] == code, reply
    assert reply["state"]["account"] == {"status": "signed_out", "identity": None,
                                         "email": None, "gate": None, "methods": []}
    assert f["state"]()["credential_count"] == 0
    assert not any(call[1] == "/api/desktop/session" for call in f["calls"])
    assert "secret-should-never" not in json.dumps(reply)


@pytest.mark.parametrize("stage", ["config", "session"])
def test_password_config_and_session_errors_never_echo_arbitrary_upstream_codes(account_native, stage):
    f = account_native
    f["policy"]["failure"] = stage
    result = password(f)
    assert result["error"]["code"] == "request_failed"
    assert "synthetic_secret" not in json.dumps(result)
    assert f["state"]()["credential_count"] == 0


@pytest.mark.parametrize("email,password_value,error", [
    ("invalid", "synthetic-password", "invalid_email"),
    ("user@example.test", "", "invalid_credentials"),
])
def test_invalid_first_factor_does_not_reach_http(account_native, email, password_value, error):
    f = account_native
    before = len(f["calls"])
    result = f["request"]({"type": "sign_in_password", "email": email, "password": password_value})
    assert result["error"]["code"] == error
    assert len(f["calls"]) == before


def test_cancel_fences_password_response_from_actual_http(account_native):
    f = account_native
    f["send"](action="hold", path="/auth/v1/token")
    f["wait"]("control_ready")
    envelope = {"version": 1, "request_id": "held-password", "expected_generation": f["state"]()["state"]["generation"],
                "action": {"type": "sign_in_password", "email": "user@example.test", "password": "synthetic-password"}}
    f["send"](action="eval", script="window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
              + json.dumps(envelope) + "});")
    f["wait"]("http_held")
    envelope["action"] = {"type": "cancel"}
    f["send"](action="eval", script="window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
              + json.dumps(envelope) + "});")
    f["wait"]("loaded", lambda event: event["url"].startswith("http://127.0.0.1"))
    f["send"](action="release")
    f["wait"]("reply", lambda event: event["error"] == "stale_generation")
    assert f["state"]()["credential_count"] == 0
    assert f["state"]()["state"]["account"]["status"] == "signed_out"
    assert not any(call[1] == "/api/desktop/session" for call in f["calls"])


def test_signout_receipt_repeats_only_in_admitted_document_and_consumes_once(account_native):
    f = account_native
    assert password(f)["ok"]
    # A route alone has no authority, even when it is otherwise trusted.
    f["send"](action="eval", script="location='tauri://localhost/account/index.html?view=sign-out&locale=en';")
    f["wait"]("loaded", lambda event: "view=sign-out" in event["url"])
    assert "result" not in f["request"]({"type": "get_state"})
    f["present"]("sign-out", "zh")
    one = f["request"]({"type": "get_state"})
    two = f["request"]({"type": "get_state"})
    assert one.get("result", {}).get("type") == "sign_out_intent", one
    assert one["result"] == two["result"]
    receipt = one["result"]
    envelope = {"version": 1, "request_id": "consume", "expected_generation": receipt["generation"],
                "action": {"type": "sign_out", "intent_id": receipt["intent_id"]}}
    f["send"](action="eval", script="window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
              + json.dumps(envelope) + "});")
    f["wait"]("loaded", lambda event: event["url"].startswith("http://127.0.0.1"))
    after = f["state"]()
    assert after["credential_count"] == 0
    assert after["state"]["account"]["status"] == "signed_out"
    assert after["visible"]
    f["present"]()
    replay = f["request"]({"type": "sign_out", "intent_id": receipt["intent_id"]})
    assert not replay["ok"] and replay["error"]["code"] == "invalid_sign_out_intent"
    assert f["state"]()["state"]["generation"] == after["state"]["generation"]


@pytest.mark.parametrize("replacement", ["reload", "route", "owner", "lifecycle"])
def test_logout_receipt_cannot_reappear_after_document_or_owner_replacement(account_native, replacement):
    f = account_native
    f["present"]("sign-out")
    receipt = f["request"]({"type": "get_state"})["result"]
    if replacement == "reload":
        f["send"](action="eval", script="location.reload();")
        f["wait"]("loaded", lambda event: "view=sign-out" in event["url"])
    elif replacement == "route":
        f["present"]()
    elif replacement == "lifecycle":
        f["send"](action="local_unavailable")
        f["wait"]("local_unavailable")
    else:
        assert f["request"]({"type": "request_otp", "email": "new@example.test"})["ok"]
    for _ in range(2):
        assert "result" not in f["request"]({"type": "get_state"})
    rejected = f["request"]({"type": "sign_out", "intent_id": receipt["intent_id"]})
    assert not rejected["ok"] and rejected["error"]["code"] == "invalid_sign_out_intent"


@pytest.mark.parametrize("locale,store_failure", [("en", False), ("zh", False), ("zh", True)])
def test_receipt_cleanup_failure_is_visible_and_only_explicit_current_action_retries(account_native, locale, store_failure):
    """AUTH-SETUP-176: cleanup error survives new receiver without a new intent."""
    f = account_native
    assert password(f)["ok"]
    f["present"]("sign-out", locale)
    receipt = f["request"]({"type": "get_state"})["result"]
    error = "window_unavailable" if store_failure else "credentials_unavailable"
    f["send"](action="retire_failure" if store_failure else "credential_failure", enabled=True)
    f["wait"]("control_ready")
    envelope = {"version": 1, "request_id": "cleanup-failure", "expected_generation": receipt["generation"],
                "action": {"type": "sign_out", "intent_id": receipt["intent_id"]}}
    f["send"](action="eval", script="window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
              + json.dumps(envelope) + "});")
    f["wait"]("reply", lambda event: event["error"] == error)
    state = f["state"]()
    assert state["credential_count"] == (0 if store_failure else 1) and state["erase_calls"] == 1
    assert state["state"]["account"]["status"] == "signed_out"
    assert "view=sign-out" in f["js"]("return location.href;"), "cleanup error lost its owned UI receiver"
    assert f"locale={locale}" in f["js"]("return location.href;")
    # Before the test issues any get_state, the product hook's first request
    # must have rendered the failure. A later manual refresh cannot mask a race.
    deadline = time.monotonic() + 5
    while not f["js"]("return Boolean(document.querySelector('[role=alert]')?.textContent.trim());"):
        assert time.monotonic() < deadline, "initial error receiver missed cleanup failure"
    failure = f["request"]({"type": "get_state"})
    assert failure["error"]["code"] == error
    assert "result" not in failure
    assert f["request"]({"type": "sign_out", "intent_id": receipt["intent_id"]})["error"]["code"] == "invalid_sign_out_intent"
    assert f["state"]()["erase_calls"] == 1
    assert password(f)["error"]["code"] == error
    f["send"](action="credential_failure", enabled=False)
    f["wait"]("control_ready")
    envelope["expected_generation"] = f["state"]()["state"]["generation"]
    envelope["action"] = {"type": "sign_out"}
    f["send"](action="eval", script="window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
              + json.dumps(envelope) + "});")
    f["wait"]("reply", lambda event: event["error"] is None)
    assert f["state"]()["credential_count"] == 0
    assert f["state"]()["erase_calls"] == (1 if store_failure else 2)


@pytest.mark.parametrize("decision", ["approve", "cancel", "new_owner", "reload"])
def test_native_menu_guard_receipt_keeps_actual_source_and_owner(account_native, decision):
    """AUTH-SETUP-176/177: actual menu callback cannot cross a source/owner replacement."""
    f = account_native
    assert password(f)["ok"]
    f["send"](action="escape")
    f["wait"]("loaded", lambda event: event["url"].startswith("http://127.0.0.1"))
    f["js"]("window.addEventListener('avibe:desktop-present',e=>{e.preventDefault();window.fixtureGuard=e.detail;});return true;")
    original = f["state"]()
    f["send"](action="menu")
    deadline = time.monotonic() + 5
    receipt = None
    while not receipt:
        receipt = f["js"]("return window.fixtureGuard || null;")
        assert time.monotonic() < deadline
    assert receipt["view"] == "sign-out"
    assert f["state"]()["state"]["generation"] == original["state"]["generation"]
    if decision == "cancel":
        # Consuming the real event without returning an approval is the guard's
        # cancellation contract; it must never create an automatic fallback.
        assert f["js"]("return location.protocol;") == "http:"
        assert f["state"]()["credential_count"] == 1
        return
    if decision == "new_owner":
        f["send"](action="escape")
        f["wait"]("escaped")
    elif decision == "reload":
        f["send"](action="eval", script="location.reload();")
        f["wait"]("loaded", lambda event: event["url"].startswith("http://127.0.0.1"))
    destination = f"avibe://desktop/sign-out?native_request={receipt['presentationToken']}&locale=zh"
    f["send"](action="eval", script="location=" + json.dumps(destination))
    if decision == "approve":
        f["wait"]("loaded", lambda event: "view=sign-out" in event["url"] and "locale=zh" in event["url"])
        assert f["request"]({"type": "get_state"})["result"]["type"] == "sign_out_intent"
    else:
        # Process another native eval after the attempted navigation callback.
        assert f["js"]("return location.protocol;") == "http:"
        assert f["state"]()["credential_count"] == 1


def test_new_explicit_escape_supersedes_delayed_cleanup_error_presentation(account_native):
    f = account_native
    assert password(f)["ok"]
    f["present"]("sign-out")
    receipt = f["request"]({"type": "get_state"})["result"]
    for action in ["credential_failure", "hold_error_view"]:
        f["send"](action=action, enabled=True)
        f["wait"]("control_ready")
    envelope = {"version": 1, "request_id": "late-error", "expected_generation": receipt["generation"],
                "action": {"type": "sign_out", "intent_id": receipt["intent_id"]}}
    f["send"](action="eval", script="window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
              + json.dumps(envelope) + "});")
    f["wait"]("error_view_held")
    f["send"](action="escape")
    f["wait"]("loaded", lambda event: event["url"].startswith("http://127.0.0.1"))
    generation = f["state"]()["state"]["generation"]
    f["send"](action="release_error_view")
    f["wait"]("reply", lambda event: event["error"] == "credentials_unavailable")
    assert f["state"]()["state"]["generation"] == generation
    assert f["js"]("return location.protocol;") == "http:"
    assert f["state"]()["erase_calls"] == 1
