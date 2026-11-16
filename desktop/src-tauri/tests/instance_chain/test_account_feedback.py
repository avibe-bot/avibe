"""A4/A5 owning native boundaries over isolated HTTP and actual WKWebView documents.

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
from urllib.parse import parse_qs, quote, urlsplit

import pytest

import proxy


@pytest.fixture
def account_native(tmp_path, request):
    options = getattr(request, "param", {})
    calls = []
    revocation_received = threading.Event()
    revocation_release = threading.Event()
    local_document_entered = threading.Event()
    local_document_release = threading.Event()
    version_document_entered = threading.Event()
    version_document_release = threading.Event()
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
            location = None
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
            elif path == "/auth/v1/verify":
                value = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}
            elif path == "/auth/v1/otp":
                value = {}
            elif path == "/api/desktop/session":
                if policy.get("failure") == "session":
                    status, value = 400, {"error": "synthetic_secret_lowercase"}
                elif policy["factor"]:
                    status, value = 403, {"error": "two_factor_required", "email": "user@example.test",
                                          "gate": policy.get("gate", "challenge"), "methods": ["totp", "passkey"]}
                    cookie = "account_gate=synthetic; Path=/; Secure; HttpOnly"
                else:
                    value, cookie = identity, "account=synthetic; Path=/; Secure; HttpOnly"
            elif path == "/api/two-factor/verify":
                value, cookie = {}, "account=synthetic; Path=/; Secure; HttpOnly"
            elif path == "/api/two-factor/totp/setup":
                value = {"uri": "otpauth://totp/fixture", "qrSvg": "<svg></svg>", "secret": "fixture-secret", "token": "fixture-enroll"}
            elif path == "/api/two-factor/totp":
                policy["mobile_factor"] = False
                value = {"method": {"id": "totp-fixture", "kind": "totp", "name": "Authenticator",
                                    "createdAt": "2026-11-15", "lastUsedAt": None},
                         "recoveryCodes": ["fixture-recovery"]}
                cookie = "account=synthetic; Path=/; Secure; HttpOnly"
            elif path == "/api/mobile/session":
                if self.command == "DELETE":
                    revocation_received.set()
                    if policy.get("hold_revocation"):
                        assert revocation_release.wait(timeout=10), "fixture revocation response not released"
                    value = {}
                elif policy.get("mobile_factor"):
                    status, value = 403, {"error": "two_factor_required", "email": "user@example.test",
                                          "gate": "enroll", "methods": ["totp", "passkey"]}
                elif "account=synthetic" in headers.get("cookie", ""):
                    value = identity
                else:
                    status, value = 401, {"error": "signed_out"}
            elif path == "/auth/login":
                host = headers["host"]
                status = 302
                location = "https://avibe.bot/oauth/authorize?state=owned&redirect_uri=" + quote(f"https://{host}/auth/callback", safe="")
            elif path == "/api/desktop/instance-authorization":
                callback = parse_qs(body["authorize_query"])["redirect_uri"][0]
                value = {"status": "authorized", "redirect_url": callback + "?code=fixture&state=owned"}
            elif path == "/auth/callback":
                status = 302
                cookie = "__Host-vibe_remote_session=fixture; Secure; HttpOnly; Path=/"
            elif path == "/api/session":
                value = {"remote": True, "authenticated": True, "authorization_state": "current",
                         "sub": "synthetic-account", "instance_id": headers["host"].split(".")[0],
                         "capabilities": {"can_read_instance": True}}
            elif path == "/api/desktop/local-instance":
                if policy.get("paired"):
                    value = local_descriptor()
                else:
                    status, value = 404, {"error": "not_supported"}
            elif path == "/api/csrf-token":
                value, cookie = {"csrf_token": "fixture-csrf"}, "vibe_csrf_token=fixture-csrf; Path=/"
            elif path == "/api/desktop/local-instance/prepare":
                assert headers["x-vibe-csrf-token"] == "fixture-csrf"
                value = {"operation_id": "fixture-operation", "local": local_descriptor()}
            elif path == "/api/desktop/instances/ensure-local":
                value = {"outcome": "already_paired", "owner_match": True, "instance_id": "local"}
            elif path == "/fixture-settings.js" and options.get("settings"):
                content_type = "application/javascript"
                value = Path(os.environ["DESKTOP_SETTINGS_FIXTURE"]).joinpath("settings.js").read_text()
            elif path == "/fixture-version.js" and options.get("version"):
                content_type = "application/javascript"
                value = Path(os.environ["DESKTOP_VERSION_FIXTURE"]).joinpath("version.js").read_text()
            elif path == "/fixture-load-blocker.gif" and options.get("version"):
                # React and its native IPC execute while this subresource keeps
                # the genuine candidate's Finished callback pending.
                if policy.get("hold_version_document"):
                    version_document_entered.set()
                    assert version_document_release.wait(timeout=25), "fixture version document not released"
                content_type = "image/gif"
                value = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")
            elif path == "/api/version" and options.get("version"):
                value = {"current": "0.1.0", "latest": None, "update_available": False, "managed_by": "desktop",
                         "phase": "idle", "host_actions": ["check", "prereleases", "auto_update"],
                         "auto_update": True, "prereleases": False}
            elif path in ("/", "/settings/general", "/settings/shortcuts", "/settings/notifications"):
                if policy.get("hold_local_document") and path == "/" and headers["host"] == urlsplit(alternate_origin).netloc:
                    local_document_entered.set()
                    assert local_document_release.wait(timeout=15), "fixture local document not released"
                content_type = "text/html"
                value = (Path(os.environ["DESKTOP_SETTINGS_FIXTURE"]).joinpath("index.html").read_text()
                         if options.get("settings") and path == "/settings/notifications"
                         else Path(os.environ["DESKTOP_VERSION_FIXTURE"]).joinpath("index.html").read_text()
                         if options.get("version")
                         else "<!doctype html><html><body>Isolated local Workbench</body></html>")
                if policy.get("document_redirect") and headers["host"] == "studio.avibe.bot":
                    status, location = 302, "https://other.avibe.bot/"
            else:
                status, value = 404, {"error": "fixture_unknown_path"}
            data = value if isinstance(value, bytes) else value.encode() if isinstance(value, str) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_DELETE = respond

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    alternate = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) if options.get("lifecycle") else None
    if alternate:
        threading.Thread(target=alternate.serve_forever, daemon=True).start()
    alternate_origin = f"http://127.0.0.1:{alternate.server_port}" if alternate else None
    mappings = tmp_path / "mappings.json"
    mapped = {"avibe.bot": base, "provider.fixture.test": base, "studio.avibe.bot": base, "other.avibe.bot": base,
              f"127.0.0.1:{server.server_port}": base}
    if alternate:
        mapped[f"127.0.0.1:{alternate.server_port}"] = alternate_origin
    mappings.write_text(json.dumps(mapped))
    tunnel, tunnel_url, fingerprint, hold = proxy.start(tmp_path, mappings)
    config = tmp_path / "native.json"
    config.write_text(json.dumps({"root": str(tmp_path), "backend": base, "provider": base,
                                 "local": base, "proxy": tunnel_url, "mappings": str(mappings)}))
    foreground = os.environ.get("AVIBE_FIXTURE_FOREGROUND") == "1"
    locale = getattr(request.node, "callspec", None)
    locale = locale.params.get("locale", options.get("locale", "en")) if locale else options.get("locale", "en")
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "AVIBE_FIXTURE_TLS_SHA256": fingerprint,
                   "AVIBE_FIXTURE_FOREGROUND": "1" if foreground else "0", "AVIBE_FIXTURE_LOCALE": locale}
    if options.get("receipt_observer"):
        environment["AVIBE_FIXTURE_RECEIPT_OBSERVER"] = "1"
    if options.get("shell"):
        environment["AVIBE_FIXTURE_SHELL"] = "1"
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

    def js(script, label=None):
        send(action="eval", label=label, script="Promise.resolve().then(async()=>{" + script
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

    def present(view="account", locale=None):
        suffix = f"?locale={locale}" if locale else ""
        send(action="eval", script=f"location='avibe://desktop/{view}{suffix}';")
        wait("loaded", lambda event: f"view={view}" in event["url"] and
             (locale is None or f"locale={locale}" in event["url"]))
        presentation_ready(view)

    def presentation_ready(view):
        deadline = time.monotonic() + 8
        while True:
            send(action="presentation_status")
            status = wait("presentation_status")
            if status["allowed"] and f"view={view}" in (status["url"] or ""):
                return
            assert time.monotonic() < deadline, status
            time.sleep(.02)

    try:
        wait("native_ready")
        wait("loaded", lambda event: "/account/index.html" in event["url"])
        presentation_ready("account")
        deadline = time.monotonic() + 8
        while not js("return Boolean(document.querySelector('h1'));"):
            assert time.monotonic() < deadline, "actual AccountApp has not rendered"
        yield {"send": send, "wait": wait, "js": js, "state": state, "request": request, "present": present,
               "calls": calls, "policy": policy, "root": tmp_path, "history": history,
               "identity": identity, "hold": hold, "foreground": foreground,
               "local_origin": base, "alternate_origin": alternate_origin,
               "local_document_entered": local_document_entered, "local_document_release": local_document_release,
               "version_document_entered": version_document_entered, "version_document_release": version_document_release,
               "revocation_received": revocation_received, "revocation_release": revocation_release}
    finally:
        hold.release.set()
        revocation_release.set()
        local_document_release.set()
        version_document_release.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        if alternate:
            alternate.shutdown()
        tunnel.shutdown()
        log.close()
        (tmp_path / "process-cleanup.json").write_text(json.dumps({
            "pid": process.pid, "exit_code": process.returncode,
            "executable": str(executable), "environment": environment,
            "server_port": server.server_port, "proxy": tunnel_url,
            "owned_http_and_proxy_shutdown": True,
        }, indent=2))
        # Retain exact wire shape without duplicating the synthetic password/tokens in logs.
        (tmp_path / "http-shapes.json").write_text(json.dumps([
            {"method": method, "path": path, "headers": sorted(headers), "fields": sorted(body or {})}
            for method, path, headers, body in calls
        ], indent=2))


def local_descriptor():
    return {"schema_version": 1, "installation_id": "fixture-installation", "device_name": "Fixture computer",
            "binding_revision": "fixture-binding", "pairing": {"instance_id": "local", "backend_url": "https://avibe.bot",
            "public_url": "https://local.avibe.bot"}, "remote_access": {"configured_enabled": False, "requested": False, "running": False},
            "enrollment": None}


def password(f):
    return f["request"]({"type": "sign_in_password", "email": "user@example.test", "password": "synthetic-password"})


@pytest.mark.parametrize("account_native", [{"receipt_observer": True}], indirect=True)
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
    eventually(f, lambda _: (active(f)["url"] or "").startswith("http://127.0.0.1"))
    f["send"](action="release")
    f["wait"]("reply", lambda event: event["error"] == "untrusted_desktop_caller")
    assert f["state"]()["credential_count"] == 0
    assert f["state"]()["state"]["account"]["status"] == "signed_out"
    assert not any(call[1] == "/api/desktop/session" for call in f["calls"])


@pytest.mark.parametrize("account_native", [{"receipt_observer": True, "locale": "zh"}], indirect=True)
def test_signout_receipt_repeats_only_in_admitted_document_and_consumes_once(account_native):
    f = account_native
    assert password(f)["ok"]
    # A route alone has no authority, even when it is otherwise trusted.
    f["send"](action="eval", script="location='tauri://localhost/account/index.html?view=sign-out&locale=en';")
    # A5 also rejects direct replacement of the admitted account route.
    assert "view=account" in f["js"]("return location.href;")
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
@pytest.mark.parametrize("account_native", [{"receipt_observer": True}], indirect=True)
def test_logout_receipt_cannot_reappear_after_document_or_owner_replacement(account_native, replacement):
    f = account_native
    f["present"]("sign-out")
    receipt = f["request"]({"type": "get_state"})["result"]
    if replacement == "reload":
        f["send"](action="eval", script="location.reload();")
        f["wait"]("loaded", lambda event: "view=sign-out" in event["url"])
        assert f["request"]({"type": "get_state"})["error"] == "untrusted_desktop_caller"
        return
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
@pytest.mark.parametrize("account_native", [{"receipt_observer": True}], indirect=True)
def test_receipt_cleanup_failure_is_visible_and_only_explicit_current_action_retries(account_native, locale, store_failure):
    """AUTH-SETUP-176: cleanup error survives new receiver without a new intent."""
    f = account_native
    assert password(f)["ok"]
    f["present"]("sign-out", locale)
    receipt = f["request"]({"type": "get_state"})["result"]
    original_receiver = active(f)["label"]
    error = "window_unavailable" if store_failure else "credentials_unavailable"
    f["send"](action="retire_failure" if store_failure else "credential_failure", enabled=True)
    f["wait"]("control_ready")
    envelope = {"version": 1, "request_id": "cleanup-failure", "expected_generation": receipt["generation"],
                "action": {"type": "sign_out", "intent_id": receipt["intent_id"]}}
    f["send"](action="eval", script="window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
              + json.dumps(envelope) + "});")
    f["wait"]("reply", lambda event: event["error"] == error)
    eventually(f, lambda _: owned_error_ready(f, original_receiver))
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
    f["wait"]("reply", lambda event: event["error"] == "untrusted_desktop_caller")
    assert f["js"]("return location.protocol;") == "http:"
    assert f["state"]()["credential_count"] == 0
    assert f["state"]()["erase_calls"] == (1 if store_failure else 2)


@pytest.mark.parametrize("decision", ["approve", "cancel", "new_owner", "reload"])
@pytest.mark.parametrize("account_native", [{"receipt_observer": True}], indirect=True)
def test_native_menu_guard_receipt_keeps_actual_source_and_owner(account_native, decision):
    """AUTH-SETUP-176/177: actual menu callback cannot cross a source/owner replacement."""
    f = account_native
    assert password(f)["ok"]
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
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


@pytest.mark.parametrize("account_native", [{"receipt_observer": True}], indirect=True)
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
    f["wait"]("reply", lambda event: event["error"] == "untrusted_desktop_caller")
    assert f["state"]()["state"]["generation"] == generation
    assert f["js"]("return location.protocol;") == "http:"
    assert f["state"]()["erase_calls"] == 1


@pytest.mark.parametrize("source", ["local", "remote"])
def test_back_preserves_actual_workbench_document(account_native, source):
    """A5: Back retains DOM, route, scroll and draft; URL restoration is insufficient."""
    f = account_native
    if source == "local":
        f["send"](action="escape")
        assert f["wait"]("escaped")["ok"]
    else:
        f["identity"]["instances"] = [{"id": "studio", "name": "shared-studio", "host": "studio.avibe.bot",
            "url": "https://studio.avibe.bot", "relationship": "shared", "owner_email": "owner@example.test", "available": True}]
        assert password(f)["ok"]
        f["send"](action="eval", script="window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
                  + json.dumps({"version": 1, "request_id": "select", "expected_generation": f["state"]()["state"]["generation"],
                                "action": {"type": "select_instance", "instance_id": "studio"}}) + "});")
        deadline = time.monotonic() + 15
        while f["state"]()["state"]["connection"]["committed"]["kind"] != "remote":
            assert time.monotonic() < deadline
            time.sleep(.05)
    before = f["js"]("""
        window.feedbackDocument = crypto.randomUUID();
        document.body.innerHTML = '<textarea id="draft"></textarea><div style="height:3000px"></div>';
        document.querySelector('#draft').value = 'unsubmitted fixture';
        history.pushState({}, '', '/?session=retained#draft');
        window.scrollTo(0, 600);
        return {id:window.feedbackDocument,url:location.href,scroll:scrollY,draft:document.querySelector('#draft').value};
    """)
    f["present"]("instances")
    f["send"](action="eval", script="window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
              + json.dumps({"version": 1, "request_id": "back", "expected_generation": f["state"]()["state"]["generation"],
                            "action": {"type": "cancel"}}) + "});")
    deadline = time.monotonic() + 8
    while True:
        f["send"](action="presentation_status")
        status = f["wait"]("presentation_status")
        if status["url"] == before["url"]:
            break
        assert time.monotonic() < deadline, status
        time.sleep(.05)
    after = f["js"]("return {id:window.feedbackDocument ?? null,url:location.href,scroll:scrollY,draft:document.querySelector('#draft')?.value ?? null};")
    assert after == before


def catalog_entry(instance_id="studio"):
    return {"id": instance_id, "name": f"shared-{instance_id}", "host": f"{instance_id}.avibe.bot",
            "url": f"https://{instance_id}.avibe.bot", "relationship": "shared",
            "owner_email": "owner@example.test", "available": True}


def popup_snapshot(f):
    f["send"](action="update_control", operation="snapshot")
    return f["wait"]("popup_snapshot")["popup"]


def popup_request(f, action):
    envelope = {"version": 1, "request_id": "popup-fixture",
                "expected_generation": f["state"]()["state"]["generation"], "action": action}
    return f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
                   + json.dumps(envelope) + "}).catch(error=>({rejected:String(error)}));", "update-popover")


def committed_remote(f):
    f["identity"]["instances"] = [catalog_entry()]
    assert password(f)["ok"]
    dispatch(f, {"type": "select_instance", "instance_id": "studio"})
    eventually(f, lambda state: state["state"]["connection"]["committed"]["kind"] == "remote")
    label = active(f)["label"]
    marker = f["js"]("""
        window.retainedMarker=crypto.randomUUID();history.pushState({},'', '/?remote=retained');
        const input=document.createElement('textarea');input.id='retained-draft';input.value='unsent fixture';
        document.body.append(input);return [window.retainedMarker,location.href,history.length,input.value];
    """)
    return label, marker


@pytest.mark.parametrize("account_native", [{"shell": True}], indirect=True)
def test_actual_remote_update_popup_retains_document_and_owns_only_update_actions(account_native):
    """AUTH-SETUP-177/178: popup GetState must not borrow Account authority.

    The mechanically integrated native baseline cannot find A5's physical main;
    owner mocks cannot establish real child construction, IPC or DOM consumption.
    """
    f = account_native
    remote, marker = committed_remote(f)
    before = presentation_snapshot(f)
    start = len(f["history"])
    f["send"](action="menu", view="updates")
    eventually(f, lambda _: popup_snapshot(f) is not None or any(
        event.get("event") == "popup_open_rejected" for event in f["history"][start:]))
    assert not [event for event in f["history"][start:] if event.get("event") == "popup_open_rejected"], (
        "actual popup producer rejected A5's physical main", f["history"][start:])
    eventually(f, lambda _: popup_snapshot(f)["visible"])
    popup = popup_snapshot(f)
    assert popup["position"]["x"] < -10000 and not popup["focused"], popup
    eventually(f, lambda _: f["js"]("return Boolean(document.querySelector('[data-update-popover] [role=dialog]'));", "update-popover"))
    reply = popup_request(f, {"type": "get_state"})
    assert reply["ok"] and "result" not in reply, reply
    assert any(event.get("event") == "page_load" and event.get("label") == "update-popover"
               and event.get("finished") for event in f["history"])
    assert f["js"]("return [window.retainedMarker,location.href,history.length,document.querySelector('#retained-draft').value];", remote) == marker
    for action in [{"type": "cancel"}, {"type": "sign_out"}, {"type": "return_local"},
                   {"type": "select_instance", "instance_id": "studio"}]:
        assert popup_request(f, action) == {"rejected": "untrusted_desktop_caller"}, action
    for action in [{"type": "get_state"}, {"type": "update_set_auto_update", "enabled": False}]:
        envelope = {"version": 1, "request_id": "remote-cannot-borrow-popup",
                    "expected_generation": f["state"]()["state"]["generation"], "action": action}
        assert f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
                       + json.dumps(envelope) + "}).then(()=>false,()=>true);", remote)
    # The actual AccountApp toggle invokes the actual private updater preference.
    eventually(f, lambda _: f["js"]("return document.querySelectorAll('[role=switch]').length===2;", "update-popover"))
    initial = f["js"]("return [...document.querySelectorAll('[role=switch]')].map(e=>({name:e.getAttribute('aria-label'),checked:e.getAttribute('aria-checked')}));", "update-popover")
    (f["root"] / "popup-switches.json").write_text(json.dumps(initial, indent=2))
    current = reply["state"]["desktop"]["update"]["autoUpdate"]
    f["js"]("document.querySelectorAll('[role=switch]')[1].click();return true;", "update-popover")
    eventually(f, lambda _: popup_request(f, {"type": "get_state"})["state"]["desktop"]["update"]["autoUpdate"] != current)
    now = presentation_snapshot(f)
    for key in ("owner", "window", "document", "label", "runtime", "returning", "logout"):
        assert now[key] == before[key], (key, before, now)
    assert not [call for call in f["calls"] if call[0] == "DELETE"]
    assert f["state"]()["erase_calls"] == 0
    (f["root"] / "popup-result.json").write_text(json.dumps({"popup": popup, "before": before, "after": now}, indent=2))
    f["send"](action="eval", label="update-popover",
              script="window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));")
    eventually(f, lambda _: popup_snapshot(f) is None)
    assert f["js"]("return [window.retainedMarker,location.href,history.length,document.querySelector('#retained-draft').value];", remote) == marker


@pytest.mark.parametrize("account_native", [{"shell": True}], indirect=True)
@pytest.mark.parametrize("entry", ["menu:updates", "updater"])
def test_remote_update_popup_requires_recovery_acknowledgement_and_fresh_request(account_native, entry):
    """AUTH-SETUP-176: retained remote selection cannot bypass the Account guard."""
    f = account_native
    remote, marker = committed_remote(f)
    f["present"]("account")
    recovery_page(f)
    original = presentation_snapshot(f)
    before = f["state"]()
    start = len(f["history"])
    ordinary_presentation(f, entry)
    current, callbacks = presentation_outcome(f, start, original["label"])
    assert current["label"] == original["label"] and callbacks[-1]["result"] == '"dispatched"'
    assert popup_snapshot(f) is None and not current["logout"]
    assert f["js"]("return document.body.innerText.includes('fixture-recovery');")
    assert f["state"]()["erase_calls"] == before["erase_calls"]
    assert f["state"]()["state"]["generation"] == before["state"]["generation"]
    assert not [call for call in f["calls"] if call[0] == "DELETE"]
    f["js"]("[...document.querySelectorAll('button')].find(b=>b.textContent==='I have saved these codes').click();return true;")
    eventually(f, lambda _: f["js"]("return !document.body.innerText.includes('fixture-recovery');"))
    f["send"](action="shell_control", operation="finished")
    f["wait"]("shell_control", lambda event: event["operation"] == "finished")
    assert popup_snapshot(f) is None
    ordinary_presentation(f, entry)
    eventually(f, lambda _: popup_snapshot(f) is not None)
    eventually(f, lambda _: popup_snapshot(f)["visible"])
    assert popup_request(f, {"type": "get_state"})["ok"]
    assert presentation_snapshot(f)["label"] == original["label"]
    assert f["js"]("return [window.retainedMarker,location.href,history.length,document.querySelector('#retained-draft').value];", remote) == marker


@pytest.mark.parametrize("account_native", [{"shell": True}], indirect=True)
@pytest.mark.parametrize("phase", ["build", "finished"])
def test_popup_delayed_native_work_cannot_show_after_source_retirement(account_native, phase):
    """AUTH-SETUP-176: real build/Finished retains its original presentation."""
    f = account_native
    committed_remote(f)
    f["send"](action="update_control", operation="hold", phase=phase)
    f["wait"]("update_control", lambda event: event["operation"] == "hold")
    f["send"](action="menu", view="updates")
    f["wait"]("update_held", lambda event: event["phase"] == phase)
    held = popup_snapshot(f)
    if held:
        assert not held["focused"] and held["position"]["x"] < -10000
        assert not held["visible"], "parented popup became visible before source-fenced Finished"
    # The remote Runtime fixture has no Workbench discard listener. Its actual
    # explicit presentation URL is the independent document-retirement trigger.
    f["present"]("account")
    eventually(f, lambda _: f["js"]("return Boolean(document.querySelector('h1'));"))
    account = active(f)["label"]
    f["send"](action="update_control", operation="release", phase=phase)
    f["wait"]("update_released", lambda event: event["phase"] == phase)
    if phase == "finished":
        f["wait"]("popup_stale_finished")
        eventually(f, lambda _: popup_snapshot(f) is None)
    else:
        f["wait"]("popup_build_settled")
        assert popup_snapshot(f) is None
    # A fresh explicit request supplies the new Account source and remains live.
    f["send"](action="menu", view="updates")
    eventually(f, lambda _: popup_snapshot(f) is not None)
    eventually(f, lambda _: popup_request(f, {"type": "get_state"}).get("ok"))
    assert active(f)["label"] == account
    assert not any(event.get("event") == "popup_shown" for event in f["history"][:next(
        index for index, event in enumerate(f["history"])
        if event.get("event") == "update_released" and event.get("phase") == phase)])


@pytest.mark.parametrize("account_native", [{"shell": True}], indirect=True)
@pytest.mark.parametrize("retirement", ["rebuild", "reload"])
def test_popup_get_state_reply_cannot_cross_same_label_rebuild(account_native, retirement):
    """AUTH-SETUP-177: hold the real result before the actual receiver recheck."""
    f = account_native
    committed_remote(f)
    f["send"](action="menu", view="updates")
    eventually(f, lambda _: popup_snapshot(f) is not None)
    # Initial IPC can complete before the initial Finished. Establish the actual
    # mounted receiver first so that a later Finished belongs to the reload.
    eventually(f, lambda _: popup_snapshot(f)["visible"])
    eventually(f, lambda _: f["js"]("return Boolean(document.querySelector('[data-update-popover] [role=dialog]'));", "update-popover"))
    eventually(f, lambda _: popup_request(f, {"type": "get_state"}).get("ok"))
    f["send"](action="update_control", operation="hold", phase="client")
    f["wait"]("update_control", lambda event: event["operation"] == "hold")
    generation = f["state"]()["state"]["generation"]
    f["send"](action="eval", label="update-popover", script="window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
              + json.dumps({"version": 1, "request_id": "held-popup-state", "expected_generation": generation,
                            "action": {"type": "get_state"}}) + "}).catch(()=>{});")
    f["wait"]("update_held", lambda event: event["phase"] == "client")
    if retirement == "rebuild":
        f["send"](action="update_control", operation="close")
        eventually(f, lambda _: popup_snapshot(f) is None)
        f["send"](action="menu", view="updates")
        eventually(f, lambda _: popup_snapshot(f) is not None)
    else:
        start = len(f["history"])
        f["send"](action="eval", label="update-popover", script="location.reload();")
        eventually(f, lambda _: any(event.get("event") == "page_load" and event.get("label") == "update-popover"
                                   and event.get("finished") for event in f["history"][start:]))
    eventually(f, lambda _: popup_request(f, {"type": "get_state"}).get("ok"))
    marker = f["js"]("window.popupMarker=crypto.randomUUID();return window.popupMarker;", "update-popover")
    f["send"](action="update_control", operation="release", phase="client")
    f["wait"]("update_released", lambda event: event["phase"] == "client")
    stale = f["wait"]("reply", lambda event: event.get("label") == "update-popover" and event.get("error") is not None)
    assert stale["error"] == "untrusted_desktop_caller", stale
    assert f["js"]("return window.popupMarker;", "update-popover") == marker
    assert popup_request(f, {"type": "get_state"})["ok"]


@pytest.mark.parametrize("account_native", [{"shell": True}], indirect=True)
def test_recovery_finalization_retires_an_approved_loading_remote_popup(account_native):
    """AUTH-SETUP-176: material-producing action defeats an approved popup."""
    f = account_native
    committed_remote(f)
    f["present"]("account")
    recovery_page(f, submit=False)
    f["send"](action="update_control", operation="hold", phase="build")
    f["wait"]("update_control", lambda event: event["operation"] == "hold")
    f["send"](action="menu", view="updates")
    f["wait"]("update_held", lambda event: event["phase"] == "build")
    f["send"](action="eval", script="document.querySelector('#factor-code').form.requestSubmit();")
    eventually(f, lambda _: f["js"]("return document.body.innerText.includes('fixture-recovery');"))
    current = presentation_snapshot(f)
    f["send"](action="update_control", operation="release", phase="build")
    f["wait"]("popup_build_settled")
    assert popup_snapshot(f) is None
    start = len(f["history"])
    f["send"](action="menu", view="updates")
    refused, callbacks = presentation_outcome(f, start, current["label"])
    assert callbacks[-1]["result"] == '"dispatched"' and refused["label"] == current["label"]
    assert popup_snapshot(f) is None
    f["js"]("[...document.querySelectorAll('button')].find(b=>b.textContent==='I have saved these codes').click();return true;")
    eventually(f, lambda _: f["js"]("return !document.body.innerText.includes('fixture-recovery');"))
    assert popup_snapshot(f) is None
    f["send"](action="menu", view="updates")
    eventually(f, lambda _: popup_snapshot(f) is not None)
    assert popup_request(f, {"type": "get_state"})["ok"]


@pytest.mark.parametrize("account_native", [{"shell": True, "lifecycle": True, "version": True}], indirect=True)
@pytest.mark.parametrize("entry", ["rebind", "signout", "preserved-account"])
@pytest.mark.parametrize("late_rejection", [False, True])
def test_actual_version_badge_recovers_after_candidate_publication(account_native, entry, late_rejection):
    """AUTH-SETUP-178: candidate denial must not permanently disable its badge.

    Unlike consumer mocks this holds a real load-blocking image, executes native
    admission and observes the actual JS Any listener on the published child.
    """
    f = account_native
    if entry == "signout":
        assert password(f)["ok"]
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    original = active(f)["label"]
    if entry in ("signout", "preserved-account"):
        f["present"]("account")
        eventually(f, lambda _: f["js"]("return Boolean(document.querySelector('h1'));"))
    f["policy"]["hold_version_document"] = True
    if late_rejection:
        f["send"](action="update_control", operation="hold", phase="state", exclude=original)
        f["wait"]("update_control", lambda event: event["operation"] == "hold")
    start = len(f["history"])
    if entry == "signout":
        f["policy"]["hold_revocation"] = True
        f["send"](action="menu", view="sign-out")
    else:
        f["send"](action="runtime_rebind", origin=f["alternate_origin"])
        f["wait"]("runtime_rebind")
    assert f["version_document_entered"].wait(timeout=8), "React document's subresource did not start"
    denied = f["wait"]("updater_probe", lambda event: event["label"] != original and "Err" in event["result"])
    candidate = denied["label"]
    assert "Workbench" in denied["result"]["Err"]
    if late_rejection:
        f["wait"]("update_held", lambda event: event["phase"] == "state")
    assert not any(event.get("event") == "page_load" and event.get("label") == candidate
                   and event.get("finished") for event in f["history"][start:])
    marker = f["js"]("""
        document.querySelector('#retained-draft').value='candidate draft';
        return [window.fixtureDocument,location.href,history.length,document.querySelector('#retained-draft').value];
    """, candidate)
    f["version_document_release"].set()
    f["wait"]("replacement_finished", lambda event: event["ok"])
    if entry == "preserved-account":
        denied_retained = f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_update_state').then(()=>false,()=>true);", candidate)
        assert denied_retained
        account_return_action(f, "back")
    eventually(f, lambda _: active(f)["label"] == candidate)
    if late_rejection:
        # A real post-unlock projection reaches the mounted listener while the
        # earlier genuine rejection is still waiting to return.
        observations = f["js"]("return window.fixtureObservations;", candidate)
        assert any(item.get("event") == "avibe:desktop-client-state" for item in observations)
        f["send"](action="update_control", operation="release", phase="state")
        f["wait"]("update_released", lambda event: event["phase"] == "state")
    admitted = f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_update_state');", candidate)
    assert "autoUpdate" in admitted, admitted
    # Retain the independent positive native read even when the old consumer
    # remains permanently disabled and the following assertion fails.
    (f["root"] / "candidate-admission.json").write_text(json.dumps({
        "denied": denied, "admitted": admitted, "marker": marker,
        "observations": f["js"]("return window.fixtureObservations;", candidate),
    }, indent=2))
    deadline = time.monotonic() + 5
    while not f["js"]("return window.fixtureListenerSnapshot().length>0;", candidate):
        assert time.monotonic() < deadline, {
            "regression": "same admitted document never restored its updater subscription",
            "native_available": admitted, "candidate": candidate, "original_denial": denied,
        }
        time.sleep(.03)
    f["js"]("document.querySelector('button[aria-controls]').click();return true;", candidate)
    eventually(f, lambda _: f["js"]("return Boolean(document.querySelector('[role=switch][aria-label=\"Update automatically\"]:not(:disabled)'));", candidate))
    listens = f["js"]("return window.fixtureListenerSnapshot();", candidate)
    assert listens and listens[-1]["target"] == {"kind": "Any"}, listens
    assert any(event.get("event") == "updater_ready" and event.get("label") == candidate
               and event["result"] == {"Ok": None} for event in f["history"])
    start_setting = len(f["history"])
    f["js"]("document.querySelector('[role=switch][aria-label=\"Update automatically\"]').click();return true;", candidate)
    eventually(f, lambda _: any(event.get("event") == "updater_setting" and event.get("label") == candidate
                               for event in f["history"][start_setting:]))
    setting = next(event for event in f["history"][start_setting:] if event.get("event") == "updater_setting")
    assert setting["result"] == {"Ok": not admitted["autoUpdate"]}
    eventually(f, lambda _: f["js"]("return window.fixtureObservations.some(item=>item.event==='avibe:desktop-update-state'&&item.payload.autoUpdate==="
                                   + json.dumps(not admitted["autoUpdate"]) + ");", candidate))
    assert f["js"]("return document.querySelector('[role=switch][aria-label=\"Update automatically\"]').getAttribute('aria-checked');", candidate) == str(not admitted["autoUpdate"]).lower()
    assert f["js"]("return [window.fixtureDocument,location.href,history.length,document.querySelector('#retained-draft').value];", candidate) == marker
    if entry == "signout":
        assert f["state"]()["credential_count"] == 0
        assert f["revocation_received"].wait(timeout=5)
        assert sum(method == "DELETE" and path == "/api/mobile/session" for method, path, _, _ in f["calls"]) == 1
        f["revocation_release"].set()
    (f["root"] / "version-consumer-result.json").write_text(json.dumps(f["js"]("return window.fixtureObservations;", candidate), indent=2))


@pytest.mark.parametrize("account_native", [{"shell": True, "lifecycle": True, "version": True}], indirect=True)
def test_recovered_version_badge_consumes_only_the_correlated_pending_open(account_native):
    """AUTH-SETUP-178: observe readiness/receipt before the held follow-up read.

    The pending-open listener normally starts an explicit update check after its
    read. Teardown ends this fixture while that genuine read is held, so this
    case proves delivery/consumption without invoking discovery or installation.
    """
    f = account_native
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    original = active(f)["label"]
    f["policy"]["hold_version_document"] = True
    f["send"](action="runtime_rebind", origin=f["alternate_origin"])
    assert f["version_document_entered"].wait(timeout=8)
    denied = f["wait"]("updater_probe", lambda event: event["label"] != original and "Err" in event["result"])
    candidate = denied["label"]
    f["send"](action="update_control", operation="hold", phase="state", exclude=original, skip=1)
    f["wait"]("update_control", lambda event: event["operation"] == "hold")
    f["send"](action="shell_control", operation="queue_updates")
    f["wait"]("shell_control", lambda event: event["operation"] == "queue_updates")
    f["send"](action="shell_status")
    pending = f["wait"]("shell_status")["pending_update"]
    assert pending is not None
    start = len(f["history"])
    f["version_document_release"].set()
    eventually(f, lambda _: any(event.get("event") == "update_held" and event["phase"] == "state"
                               for event in f["history"][start:]))
    eventually(f, lambda _: any(event.get("event") == "update_delivery" and event["id"] == pending
                               and event["result"] == '"handled"' for event in f["history"][start:]))
    assert active(f)["label"] == candidate
    f["send"](action="shell_status")
    assert f["wait"]("shell_status")["pending_update"] is None
    f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_update_panel_ready');", candidate)
    f["send"](action="shell_status")
    assert f["wait"]("shell_status")["pending_update"] is None
    handled = [event for event in f["history"][start:] if event.get("event") == "update_delivery"
               and event["result"] == '"handled"']
    assert len(handled) == 1 and handled[0]["id"] == pending, handled
    assert f["js"]("return Boolean(document.querySelector('[role=dialog]'));", candidate)
    (f["root"] / "pending-open-result.json").write_text(json.dumps({
        "pending": pending, "handled": handled, "consumer": f["js"]("return window.fixtureObservations;", candidate),
        "boundary": "terminated while genuine follow-up state result held, before check/install",
    }, indent=2))


@pytest.mark.parametrize("account_native", [{"shell": True, "version": True}], indirect=True)
def test_candidate_cannot_borrow_a_live_popup_receiver(account_native):
    """AUTH-SETUP-177: an actual candidate cannot borrow a valid popup's admission.

    The live popup is a positive control while an actual remote load remains
    unpublished; role mocks alone do not exercise this simultaneous IPC path.
    """
    f = account_native
    remote, _ = committed_remote(f)
    f["present"]("instances")
    account = active(f)["label"]
    f["identity"]["instances"] = [catalog_entry(), catalog_entry("other")]
    assert f["request"]({"type": "refresh_instances"})["ok"]
    f["send"](action="menu", view="updates")
    eventually(f, lambda _: popup_snapshot(f) is not None)
    eventually(f, lambda _: popup_request(f, {"type": "get_state"}).get("ok"))
    f["policy"]["hold_version_document"] = True
    dispatch(f, {"type": "select_instance", "instance_id": "other"})
    assert f["version_document_entered"].wait(timeout=8)
    candidate = next(view["label"] for view in f["state"]()["views"]
                     if view["label"] not in [remote, account, "update-popover"])
    assert popup_request(f, {"type": "get_state"})["ok"]
    for action in [{"type": "get_state"}, {"type": "update_set_auto_update", "enabled": True}]:
        envelope = {"version": 1, "request_id": "candidate-cannot-borrow-popup",
                    "expected_generation": f["state"]()["state"]["generation"], "action": action}
        result = f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
                         + json.dumps(envelope) + "}).then(()=>({allowed:true}),error=>({error:String(error)}));", candidate)
        # The real capability boundary rejects this remote candidate before the
        # Rust command owner. A live popup must not lend its local capability.
        assert result.get("error", "").startswith(
            f'desktop_client_request not allowed on window "main", webview "{candidate}"'), result
    assert f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_update_state').then(()=>false,()=>true);", candidate)
    f["policy"]["hold_version_document"] = False
    f["version_document_release"].set()
    eventually(f, lambda _: active(f)["label"] == candidate)
    assert f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_update_state').then(()=>false,()=>true);", candidate)
    assert f["js"]("return window.fixtureListenerSnapshot();", candidate) == []
    assert not [event for event in f["history"] if event.get("event") == "updater_setting"
                and event.get("label") == candidate]


@pytest.mark.parametrize("account_native", [{"shell": True, "version": True}], indirect=True)
def test_retired_version_document_cannot_apply_a_delayed_success_to_remote(account_native):
    """AUTH-SETUP-177/178: a real successful read cannot revive a retired document.

    Hold the original local consumer's real native result, commit a remote
    document, then deliver it. This adds native receiver retirement to UI's
    callback-order controls without manufacturing state or completion events.
    """
    f = account_native
    f["identity"]["instances"] = [catalog_entry()]
    assert password(f)["ok"]
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    local = active(f)["label"]
    eventually(f, lambda _: f["js"]("return window.fixtureListenerSnapshot().length>0;", local))
    eventually(f, lambda _: any(event.get("event") == "updater_ready" and event.get("label") == local
                               and "Ok" in event["result"] for event in f["history"]))
    f["send"](action="update_control", operation="hold", phase="state")
    f["wait"]("update_control", lambda event: event["operation"] == "hold")
    start = len(f["history"])
    f["send"](action="shell_control", operation="queue_updates")
    f["wait"]("shell_control", lambda event: event["operation"] == "queue_updates")
    f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_update_panel_ready');", local)
    eventually(f, lambda _: any(event.get("event") == "update_held" and event["phase"] == "state"
                               for event in f["history"][start:]))
    held = [event for event in f["history"][start:] if event.get("event") == "updater_probe"
            and event.get("label") == local]
    assert held and "Ok" in held[-1]["result"], held
    f["present"]("instances")
    dispatch(f, {"type": "select_instance", "instance_id": "studio"})
    eventually(f, lambda state: state["state"]["connection"]["committed"]["kind"] == "remote")
    remote = active(f)["label"]
    assert local not in [view["label"] for view in f["state"]()["views"]]
    marker = f["js"]("return window.fixtureDocument;", remote)
    f["send"](action="update_control", operation="release", phase="state")
    f["wait"]("update_released", lambda event: event["phase"] == "state")
    assert f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_update_state').then(()=>false,()=>true);", remote)
    assert f["js"]("return [window.fixtureDocument,window.fixtureListenerSnapshot()];", remote) == [marker, []]
    assert not [event for event in f["history"] if event.get("event") == "updater_ready"
                and event.get("label") == remote and "Ok" in event["result"]]
    (f["root"] / "retired-success-result.json").write_text(json.dumps({
        "held": held, "retired": local, "remote": remote, "marker": marker,
    }, indent=2))


def dispatch(f, action, request_id="a5-operation"):
    envelope = {"version": 1, "request_id": request_id, "expected_generation": f["state"]()["state"]["generation"],
                "action": action}
    f["send"](action="eval", script="window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
              + json.dumps(envelope) + "}).catch(()=>{});")


def active(f):
    f["send"](action="presentation_status")
    return f["wait"]("presentation_status")


def owned_error_ready(f, previous=None):
    status = active(f)
    return status["allowed"] and status["label"] != previous and "view=sign-out" in (status["url"] or "")


def eventually(f, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        state = f["state"]()
        if predicate(state):
            return state
        assert time.monotonic() < deadline, state
        time.sleep(.03)


@pytest.mark.parametrize("escape", ["cancel", "sign_out"])
def test_held_candidate_never_gets_authority_and_escape_retires_only_its_roles(account_native, escape):
    """A5: actual document socket held before retirement; escape must beat its completion."""
    f = account_native
    f["identity"]["instances"] = [catalog_entry()]
    assert password(f)["ok"]
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    original = active(f)
    marker = f["js"]("window.retainedMarker=crypto.randomUUID();return window.retainedMarker;")
    f["present"]("instances")
    hidden = f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_client_request',{request:"
                    + json.dumps({"version": 1, "request_id": "hidden", "expected_generation": 0,
                                  "action": {"type": "get_state"}})
                    + "}).then(()=>({allowed:true}),error=>({allowed:false,error:String(error)}));", original["label"])
    assert not hidden["allowed"], hidden
    f["hold"].arm("studio.avibe.bot", "/")
    dispatch(f, {"type": "select_instance", "instance_id": "studio"})
    assert f["hold"].entered.wait(10)
    staged = f["state"]()
    assert staged["state"]["connection"]["committed"]["kind"] == "local"
    assert any(view["label"] == original["label"] for view in staged["views"])
    assert not (f["root"] / "instance-recency.json").exists()
    dispatch(f, {"type": escape})
    eventually(f, lambda state: len(state["views"]) == 1 and not state["views"][0]["label"].startswith("main-account-"))
    f["hold"].release.set()
    if escape == "cancel":
        assert active(f)["label"] == original["label"]
        assert f["js"]("return window.retainedMarker;") == marker
        assert f["state"]()["credential_count"] == 1
    else:
        assert active(f)["label"] != original["label"]
        assert f["state"]()["credential_count"] == 0
    assert not (f["root"] / "instance-recency.json").exists()
    assert f["state"]()["instance_stores"] == 0


@pytest.mark.parametrize("failure,code", [
    ("show_failure", "window_unavailable"),
    ("retire_failure", "window_unavailable"),
    ("credential_write_failure", "credentials_unavailable"),
    ("connection_record_failure", "connection_record_unavailable"),
])
def test_terminal_selection_failure_converges_without_success_or_recency(account_native, failure, code):
    """A5: await OS/persistence, keep a visible error receiver and explicit safe cleanup."""
    f = account_native
    f["identity"]["instances"] = [catalog_entry(), catalog_entry("other")]
    assert password(f)["ok"]
    if failure == "retire_failure":
        dispatch(f, {"type": "select_instance", "instance_id": "studio"})
        eventually(f, lambda state: state["state"]["connection"]["committed"]["kind"] == "remote")
        assert f["state"]()["instance_stores"] == 1
    else:
        f["send"](action="escape")
        assert f["wait"]("escaped")["ok"]
    recency_path = f["root"] / "instance-recency.json"
    previous_recency = recency_path.read_bytes() if recency_path.exists() else None
    old = active(f)["label"]
    f["present"]("instances", "zh")
    record_path = f["root"] / "connection.json"
    original_record = record_path.read_bytes()
    if failure == "connection_record_failure":
        record_path.unlink()
        record_path.mkdir()
    else:
        f["send"](action=failure)
        f["wait"]("control_ready")
    dispatch(f, {"type": "select_instance", "instance_id": "other"})
    eventually(f, lambda state: state["state"]["enrollment"]["error_code"] == code)
    eventually(f, lambda _: owned_error_ready(f))
    status = active(f)
    assert "view=sign-out" in status["url"] and "locale=zh" in status["url"], status
    assert all(view["label"] != old for view in f["state"]()["views"])
    assert (recency_path.read_bytes() if recency_path.exists() else None) == previous_recency
    if failure == "connection_record_failure":
        assert record_path.is_dir()
        assert f["request"]({"type": "refresh_instances"})["error"]["code"] == code
        # Only the test-owned failed destination is repaired; native retry must
        # still complete the outstanding cleanup through its original owner.
        record_path.rmdir()
        record_path.write_bytes(original_record)
    else:
        assert json.loads(record_path.read_text())["last_committed"]["kind"] == "local"
    assert f["state"]()["state"]["connection"]["committed"]["kind"] == "local"
    deadline = time.monotonic() + 5
    while not f["js"]("return Boolean(document.querySelector('[role=alert]')?.textContent.trim());"):
        assert time.monotonic() < deadline
    assert "result" not in f["request"]({"type": "get_state"})
    dispatch(f, {"type": "sign_out"})
    eventually(f, lambda state: state["credential_count"] == 0 and len(state["views"]) == 1
               and not state["views"][0]["label"].startswith("main-account-"))
    eventually(f, lambda _: (active(f)["url"] or "").startswith("http://127.0.0.1"))
    assert f["state"]()["instance_stores"] == 0
    assert json.loads(record_path.read_text())["last_committed"]["kind"] == "local"


@pytest.mark.parametrize("method", ["password", "otp", "totp", "retry"])
def test_native_auth_completion_returns_retained_document_only_after_paired_success(account_native, method):
    """A5: initiating auth owner, rather than a late UI reply, returns the retained page."""
    f = account_native
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    label = active(f)["label"]
    marker = f["js"]("window.authReturnMarker=crypto.randomUUID();return window.authReturnMarker;")
    f["present"]()
    f["policy"]["paired"] = method != "retry"
    if method == "otp":
        assert f["request"]({"type": "request_otp", "email": "user@example.test"})["ok"]
        action = {"type": "verify_otp", "code": "123456"}
    elif method == "totp":
        f["policy"]["factor"] = True
        assert password(f)["state"]["account"]["status"] == "second_factor_required"
        action = {"type": "verify_second_factor", "method": "totp", "code": "123456"}
    elif method == "retry":
        assert password(f)["ok"]
        assert "view=account" in active(f)["url"]
        # get_state/refresh of an already signed-in account never auto-return.
        assert f["request"]({"type": "get_state"})["ok"]
        assert f["request"]({"type": "refresh_instances"})["ok"]
        assert "view=account" in active(f)["url"]
        f["policy"]["paired"] = True
        action = {"type": "retry_enrollment"}
    else:
        action = {"type": "sign_in_password", "email": "user@example.test", "password": "synthetic-password"}
    dispatch(f, action)
    state = eventually(f, lambda state: len(state["views"]) == 1 and state["views"][0]["label"] == label)
    assert state["state"]["account"]["status"] == "signed_in"
    assert state["state"]["enrollment"]["status"] == "paired_this_account"
    assert f["js"]("return window.authReturnMarker;") == marker
    assert state["credential_count"] == 1
    assert not (f["root"] / "instance-recency.json").exists()


def test_recovery_material_stays_in_owned_receiver_until_ack_and_late_full_event_dies_on_reload(account_native):
    """A5/A4: recovery direct result survives; full state cannot cross actual document reload."""
    f = account_native
    f["policy"].update({"paired": True, "factor": True, "gate": "enroll"})
    assert password(f)["state"]["account"]["gate"] == "enroll"
    assert f["request"]({"type": "begin_totp_enrollment"})["result"]["type"] == "totp_enrollment"
    reply = f["request"]({"type": "finish_totp_enrollment", "code": "123456"})
    assert reply["result"]["recoveryCodes"] == ["fixture-recovery"], reply
    assert reply["state"]["enrollment"]["status"] == "paired_this_account"
    assert "view=account" in active(f)["url"]
    f["send"](action="hold_account_event")
    f["wait"]("account_event_held")
    f["send"](action="eval", script="location.reload();")
    f["wait"]("loaded", lambda event: "view=account" in event["url"])
    f["js"]("window.lateFullEvents=[];window.addEventListener('avibe:desktop-client-state',e=>window.lateFullEvents.push(e.detail));return true;")
    f["send"](action="release_account_event")
    f["wait"]("account_event_released")
    assert f["js"]("return window.lateFullEvents;") == []
    assert f["request"]({"type": "get_state"})["error"] == "untrusted_desktop_caller"


def test_failed_candidate_navigation_retains_source_and_later_local_rebind_keeps_committed_remote(account_native):
    """A5: a real rejected navigation is preterminal; a local rebind cannot displace remote."""
    f = account_native
    f["identity"]["instances"] = [catalog_entry()]
    assert password(f)["ok"]
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    label = active(f)["label"]
    marker = f["js"]("window.navigationMarker=crypto.randomUUID();return window.navigationMarker;")
    f["present"]("instances")
    f["policy"]["document_redirect"] = True
    dispatch(f, {"type": "select_instance", "instance_id": "studio"})
    f["wait"]("reply", lambda event: event["error"] == "instance_navigation_timeout", timeout=36)
    assert any(view["label"] == label for view in f["state"]()["views"])
    assert not (f["root"] / "instance-recency.json").exists()
    dispatch(f, {"type": "cancel"})
    eventually(f, lambda _: active(f)["label"] == label)
    assert f["js"]("return window.navigationMarker;") == marker
    f["policy"]["document_redirect"] = False
    f["present"]("instances")
    dispatch(f, {"type": "select_instance", "instance_id": "studio"})
    eventually(f, lambda state: state["state"]["connection"]["committed"]["kind"] == "remote")
    remote = active(f)["label"]
    remote_marker = f["js"]("window.remoteMarker=crypto.randomUUID();return window.remoteMarker;")
    for command in ["runtime_offline", "runtime_rebind"]:
        f["send"](action=command)
        f["wait"](command)
    assert active(f)["label"] == remote
    assert f["js"]("return window.remoteMarker;") == remote_marker
    assert json.loads((f["root"] / "instance-recency.json").read_text())["accounts"]["synthetic-account"] == ["studio"]


@pytest.mark.parametrize("account_native", [{"shell": True}], indirect=True)
def test_physical_main_focus_close_resize_updates_and_owned_guide_keep_roles(account_native):
    """A5: actual production main callbacks consume the retained and account child roles."""
    f = account_native
    assert password(f)["ok"]
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    original = active(f)["label"]
    f["present"]("instances")
    account = active(f)["label"]
    for operation in ["hide", "focus", "close", "focus"]:
        f["send"](action="shell_control", operation=operation)
        f["wait"]("shell_control")
        expected = operation == "focus"
        eventually(f, lambda state: state["visible"] == expected)
        assert active(f)["label"] == account
    for width, height in [(1200, 800), (880, 600)]:
        f["send"](action="shell_control", operation="resize", width=width, height=height)
        f["wait"]("shell_control")
        deadline = time.monotonic() + 5
        while True:
            f["send"](action="shell_control", operation="geometry")
            geometry = f["wait"]("geometry")
            if all(view["size"] == {"width": width * geometry["scale"], "height": height * geometry["scale"]}
                   for view in geometry["views"]):
                break
            assert time.monotonic() < deadline, geometry
        assert geometry["size"] == [width, height]
        assert [button["x"] for button in geometry["buttons"]] == [12, 35, 58]
        assert all(button["top"] == 7 and button["height"] == 14 for button in geometry["buttons"])
        assert {view["physical"] for view in geometry["views"]} == {"main"}
    # The actual UI guide reuses the same admitted document and external-open handler.
    eventually(f, lambda _: f["js"]("return [...document.querySelectorAll('button')].some(b=>b.textContent.includes('Add instance'));"))
    assert f["js"]("""
        const add=[...document.querySelectorAll('button')].find(b=>b.textContent.includes('Add instance'));
        if (!add) return {missing:document.body.innerText}; add.click(); return true;
    """) is True
    eventually(f, lambda _: f["js"]("return Boolean(document.querySelector('a[href=\"https://docs.avibe.bot/get-started/install\"]'));"))
    f["send"](action="eval", script="document.querySelector('a[href=\"https://docs.avibe.bot/get-started/install\"]').click();")
    assert f["wait"]("external_browser_request")["url"] == "https://docs.avibe.bot/get-started/install"
    assert active(f)["label"] == account
    f["send"](action="shell_control", operation="updates")
    f["wait"]("loaded", lambda event: "view=updates" in event["url"])
    assert f["request"]({"type": "get_state"})["ok"]
    dispatch(f, {"type": "cancel"})
    eventually(f, lambda _: active(f)["label"] == original)


@pytest.mark.parametrize("account_native", [{"shell": True}], indirect=True)
def test_actual_main_destruction_retires_document_and_pending_auth_authority(account_native):
    f = account_native
    f["send"](action="hold", path="/auth/v1/token")
    f["wait"]("control_ready")
    dispatch(f, {"type": "sign_in_password", "email": "user@example.test", "password": "synthetic-password"})
    f["wait"]("http_held")
    f["send"](action="shell_control", operation="destroy")
    f["wait"]("shell_control")
    eventually(f, lambda state: state["views"] == [])
    f["send"](action="release")
    f["wait"]("reply", lambda event: event["error"] == "untrusted_desktop_caller")
    state = f["state"]()
    assert state["credential_count"] == 0 and state["state"]["account"]["status"] == "signed_out"
    assert state["views"] == []
    assert not any(call[1] == "/api/desktop/session" for call in f["calls"])


def test_successful_reenrollment_returns_same_remote_without_reordering_recency(account_native):
    f = account_native
    f["identity"]["instances"] = [catalog_entry()]
    assert password(f)["ok"]
    dispatch(f, {"type": "select_instance", "instance_id": "studio"})
    eventually(f, lambda state: state["state"]["connection"]["committed"]["kind"] == "remote")
    label = active(f)["label"]
    marker = f["js"]("window.remoteAuthMarker=crypto.randomUUID();return window.remoteAuthMarker;")
    recency = (f["root"] / "instance-recency.json").read_bytes()
    f["present"]()
    f["policy"]["paired"] = True
    dispatch(f, {"type": "retry_enrollment"})
    eventually(f, lambda state: len(state["views"]) == 1 and active(f)["label"] == label)
    assert f["js"]("return window.remoteAuthMarker;") == marker
    assert (f["root"] / "instance-recency.json").read_bytes() == recency


@pytest.mark.parametrize("write_failure", [False, True])
def test_final_account_ui_recovery_ack_obeys_native_persistence_result(account_native, write_failure):
    """Actual final UI buttons consume native recovery material and its persistence error."""
    f = account_native
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    label = active(f)["label"]
    f["present"]()
    f["policy"].update({"paired": True, "factor": True, "gate": "enroll"})
    assert password(f)["state"]["account"]["gate"] == "enroll"
    eventually(f, lambda _: f["js"]("return [...document.querySelectorAll('button')].some(b=>b.textContent==='Add authenticator');"))
    f["js"]("[...document.querySelectorAll('button')].find(b=>b.textContent==='Add authenticator').click();return true;")
    eventually(f, lambda _: f["js"]("return Boolean(document.querySelector('#factor-code'));"))
    if write_failure:
        f["send"](action="credential_write_failure")
        f["wait"]("control_ready")
    f["js"]("""
        const input=document.querySelector('#factor-code');
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'123456');
        input.dispatchEvent(new Event('input',{bubbles:true}));return true;
    """)
    eventually(f, lambda _: f["js"]("return !document.querySelector('#factor-code').form.querySelector('button').disabled;"))
    f["js"]("document.querySelector('#factor-code').form.requestSubmit();return true;")
    eventually(f, lambda _: f["js"]("return document.body.innerText.includes('fixture-recovery');"))
    assert "view=account" in active(f)["url"]
    before = f["state"]()["state"]
    assert before["enrollment"]["status"] == "paired_this_account"
    assert before["enrollment"]["error_code"] == ("credentials_unavailable" if write_failure else None)
    f["send"](action="eval", script="[...document.querySelectorAll('button')].find(b=>b.textContent==='I have saved these codes').click();")
    if write_failure:
        eventually(f, lambda _: f["js"]("return !document.body.innerText.includes('fixture-recovery');"))
        assert "view=account" in active(f)["url"]
        assert f["state"]()["state"]["generation"] == before["generation"]
        assert f["state"]()["credential_count"] == 0
    else:
        eventually(f, lambda state: len(state["views"]) == 1 and active(f)["label"] == label)
        assert f["state"]()["credential_count"] == 1


def recovery_page(f, *, hold_finish=False, submit=True):
    """Produce material through the actual AccountApp submit/result owner."""
    f["policy"].update({"paired": True, "factor": True, "gate": "enroll"})
    if f["state"]()["state"]["account"]["status"] == "signed_in":
        # A retained remote connection can encounter a newly required factor
        # on the actual discovery endpoint, without signing out that Runtime.
        f["policy"]["mobile_factor"] = True
        reply = f["request"]({"type": "refresh_instances"})
    else:
        reply = password(f)
    assert reply["state"]["account"]["gate"] == "enroll"
    eventually(f, lambda _: f["js"]("return [...document.querySelectorAll('button')].some(b=>b.textContent==='Add authenticator');"))
    f["js"]("[...document.querySelectorAll('button')].find(b=>b.textContent==='Add authenticator').click();return true;")
    eventually(f, lambda _: f["js"]("return Boolean(document.querySelector('#factor-code'));"))
    # The saved-codes action stays in Account on this real persistence failure,
    # letting a fresh ordinary request exercise the same native guard afterward.
    f["send"](action="credential_write_failure")
    f["wait"]("control_ready")
    if hold_finish:
        f["send"](action="hold", path="/api/two-factor/totp")
        f["wait"]("control_ready")
    f["js"]("""
        const input=document.querySelector('#factor-code');
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'123456');
        input.dispatchEvent(new Event('input',{bubbles:true}));return true;
    """)
    eventually(f, lambda _: f["js"]("return !document.querySelector('#factor-code').form.querySelector('button').disabled;"))
    if not submit:
        return
    f["send"](action="eval", script="document.querySelector('#factor-code').form.requestSubmit();")
    if hold_finish:
        f["wait"]("http_held", lambda event: event["path"] == "/api/two-factor/totp")
    else:
        eventually(f, lambda _: f["js"]("return document.body.innerText.includes('fixture-recovery');"))


def presentation_snapshot(f):
    f["send"](action="presentation_snapshot")
    return f["wait"]("presentation_snapshot")


def foreground_snapshot(f, label):
    """Await positive OS activation, which can follow the guard eval callback."""
    deadline = time.monotonic() + 5
    while True:
        current = presentation_snapshot(f)
        assert current["label"] == label, current
        if (current["visible"] and not current["minimized"] and current["focused"]
                and current["native"]["visible"] and current["native"]["key"]
                and current["native"]["application_active"]):
            return current
        assert time.monotonic() < deadline, current
        time.sleep(.02)


def link_destination(f, path):
    # A fixture-result navigation used for eval would interrupt an in-flight
    # document navigation. Wait for the real Finished event before evaluating.
    eventually(f, lambda _: any(
        event.get("event") == "loaded" and urlsplit(event["url"]).path == path
        for event in f["history"]))
    assert f["js"]("return location.pathname;") == path


def ordinary_presentation(f, entry):
    if entry.startswith("menu:"):
        f["send"](action="menu", view=entry.split(":", 1)[1])
    elif entry == "updater":
        f["send"](action="shell_control", operation="updates")
    elif entry.startswith("url:"):
        f["send"](action="eval", script="location=" + json.dumps("avibe://desktop/" + entry.split(":", 1)[1]))
    else:
        assert entry.startswith("avibe://")
        f["send"](action="native_link", url=entry)


def presentation_outcome(f, start, label):
    """Observe actual native dispatch or replacement, not a quiet time window."""
    deadline = time.monotonic() + 15
    while True:
        snapshot = presentation_snapshot(f)
        callbacks = [event for event in f["history"][start:] if event.get("event") == "guard_result"]
        if callbacks:
            # The callback can arrive after the snapshot command. Read its
            # completed queue state instead of returning that earlier sample.
            return presentation_snapshot(f), callbacks
        if snapshot["label"] != label:
            return snapshot, callbacks
        assert time.monotonic() < deadline, snapshot


@pytest.mark.parametrize("account_native", [{"shell": True}], indirect=True)
@pytest.mark.parametrize("entry,visibility", [
    ("menu:account", "visible"), ("menu:instances", "hide"), ("menu:sign-out", "minimize"),
    ("updater", "hide"), ("menu:account", "background"),
    ("url:account", "visible"), ("url:instances", "visible"),
    ("url:setup-complete", "visible"), ("url:sign-out", "visible"), ("url:updates", "visible"),
    ("avibe://settings", "hide"), ("avibe://settings/shortcuts", "minimize"),
])
def test_recovery_presentation_preserves_receiver_and_foregrounds_then_requires_fresh_request(account_native, entry, visibility):
    """AUTH-SETUP-171/176/177: native presentation cannot acknowledge recovery."""
    f = account_native
    f["send"](action="shell_control", operation="focus")
    f["wait"]("shell_control", lambda event: event["operation"] == "focus")
    recovery_page(f)
    original = presentation_snapshot(f)
    document = f["js"]("window.recoveryMarker=crypto.randomUUID();return [window.__AVIBE_DESKTOP_DOCUMENT__,window.recoveryMarker];")
    before = f["state"]()
    if f["foreground"] and visibility != "visible":
        f["send"](action="shell_control", operation=visibility)
        f["wait"]("shell_control", lambda event: event["operation"] == visibility)
        eventually(f, lambda _: (
            not presentation_snapshot(f)["visible"] if visibility == "hide"
            else not presentation_snapshot(f)["native"]["application_active"] if visibility == "background"
            else presentation_snapshot(f)["minimized"]))
    start = len(f["history"])
    ordinary_presentation(f, entry)
    current, callbacks = presentation_outcome(f, start, original["label"])
    assert current["label"] == original["label"], (entry, original, current)
    assert callbacks and callbacks[-1]["result"] == '"dispatched"', callbacks
    assert current["document"] == original["document"] and current["owner"] == original["owner"], current
    assert current["pending"] is None and not current["logout"], current
    assert f["js"]("return [window.__AVIBE_DESKTOP_DOCUMENT__,window.recoveryMarker];") == document
    assert f["js"]("return document.body.innerText.includes('fixture-recovery');")
    assert f["state"]()["state"]["generation"] == before["state"]["generation"]
    assert not [call for call in f["calls"] if call[0] == "DELETE"]
    assert f["state"]()["erase_calls"] == before["erase_calls"]
    if f["foreground"]:
        current = foreground_snapshot(f, original["label"])
        (f["root"] / "recovery-foreground.json").write_text(json.dumps(current, indent=2))
    else:
        assert not current["focused"] and not current["native"]["application_active"], current
    f["js"]("[...document.querySelectorAll('button')].find(b=>b.textContent==='I have saved these codes').click();return true;")
    eventually(f, lambda _: f["js"]("return !document.body.innerText.includes('fixture-recovery');"))
    # Explicit real lifecycle/Finished delivery must not replay the refused intent.
    f["send"](action="shell_control", operation="finished")
    f["wait"]("shell_control", lambda event: event["operation"] == "finished")
    unchanged = presentation_snapshot(f)
    assert unchanged["label"] == original["label"] and unchanged["owner"] == original["owner"], unchanged
    assert unchanged["pending"] is None and not unchanged["logout"], unchanged
    if entry in ("menu:sign-out", "url:sign-out"):
        f["policy"]["hold_revocation"] = True
    ordinary_presentation(f, entry)
    eventually(f, lambda _: presentation_snapshot(f)["label"] != original["label"])
    if entry.startswith("avibe://"):
        link_destination(f, "/settings/shortcuts" if entry.endswith("/shortcuts") else "/settings/general")
    elif entry in ("menu:sign-out", "url:sign-out"):
        # This is the full automatic AccountApp receipt consumer, without the
        # separate native receipt-observation hold used by the A4 owner tests.
        settled = eventually(f, lambda state: state["state"]["account"]["status"] == "signed_out"
                             and len(state["views"]) == 1
                             and not state["views"][0]["label"].startswith("main-account-"))
        assert settled["credential_count"] == 0 and settled["erase_calls"] > before["erase_calls"]
        assert settled["state"]["generation"] > before["state"]["generation"]
        # Local completion is observable while the server response is held.
        # Then await the real request event, without imposing arrival order.
        assert not f["revocation_release"].is_set()
        assert f["revocation_received"].wait(timeout=5), "no actual revocation request"
        assert sum(call[0] == "DELETE" and call[1] == "/api/mobile/session" for call in f["calls"]) == 1
        f["revocation_release"].set()
    else:
        destination = "updates" if entry == "updater" else entry.split(":", 1)[1]
        eventually(f, lambda _: f["js"]("return location.search;").startswith("?view=" + destination))


@pytest.mark.parametrize("account_native", [{"shell": True}], indirect=True)
def test_recovery_presentation_during_actual_finalize_preserves_late_material(account_native):
    f = account_native
    recovery_page(f, hold_finish=True)
    before = presentation_snapshot(f)
    start = len(f["history"])
    ordinary_presentation(f, "menu:instances")
    current, callbacks = presentation_outcome(f, start, before["label"])
    assert current["label"] == before["label"], current
    assert callbacks[-1]["result"] == '"dispatched"'
    assert current["owner"] == before["owner"] and not current["logout"]
    f["send"](action="release")
    eventually(f, lambda _: f["js"]("return document.body.innerText.includes('fixture-recovery');"))
    assert presentation_snapshot(f)["label"] == before["label"]


@pytest.mark.parametrize("account_native", [{"shell": True}], indirect=True)
def test_recovery_presentation_approved_before_finalize_cannot_discard_new_material(account_native):
    f = account_native
    recovery_page(f, submit=False)
    before = presentation_snapshot(f)
    f["send"](action="hold_presentation")
    f["wait"]("control_ready")
    ordinary_presentation(f, "menu:instances")
    held = f["wait"]("presentation_held")
    f["send"](action="eval", script="document.querySelector('#factor-code').form.requestSubmit();")
    eventually(f, lambda _: f["js"]("return document.body.innerText.includes('fixture-recovery');"))
    f["send"](action="release_presentation")
    result = f["wait"]("presentation_finished", lambda event: event["id"] == held["id"])
    assert not result["ok"] and result["error"] == "stale_generation", result
    after = presentation_snapshot(f)
    assert after["label"] == before["label"] and after["document"] == before["document"], after
    assert after["owner"] == before["owner"] and not after["logout"]
    assert f["js"]("return document.body.innerText.includes('fixture-recovery');")


@pytest.mark.parametrize("account_native", [{"shell": True}], indirect=True)
def test_recovery_presentation_same_url_delivery_retires_only_old_approval(account_native):
    f = account_native
    recovery_page(f)
    before = presentation_snapshot(f)
    # Observation only; the actual AccountApp listener decides and consumes.
    f["js"]("window.addEventListener('avibe:desktop-present',e=>window.observedPresentation=e.detail);return true;")
    tokens = []
    for _ in range(2):
        start = len(f["history"])
        ordinary_presentation(f, "avibe://settings")
        current, callbacks = presentation_outcome(f, start, before["label"])
        assert current["label"] == before["label"] and callbacks[-1]["result"] == '"dispatched"'
        tokens.append(f["js"]("return window.observedPresentation.presentationToken;"))
    assert tokens[0] != tokens[1]
    f["send"](action="eval", script="location=" + json.dumps("avibe://desktop/account?native_request=" + tokens[0]))
    assert f["js"]("return document.body.innerText.includes('fixture-recovery');")
    assert presentation_snapshot(f)["owner"] == before["owner"]
    assert presentation_snapshot(f)["approval"] == tokens[1]
    f["js"]("[...document.querySelectorAll('button')].find(b=>b.textContent==='I have saved these codes').click();return true;")
    eventually(f, lambda _: f["js"]("return !document.body.innerText.includes('fixture-recovery');"))
    f["send"](action="shell_control", operation="finished")
    f["wait"]("shell_control", lambda event: event["operation"] == "finished")
    assert presentation_snapshot(f)["label"] == before["label"]
    ordinary_presentation(f, "avibe://settings")
    eventually(f, lambda _: presentation_snapshot(f)["label"] != before["label"])
    link_destination(f, "/settings/general")


def account_return_action(f, route):
    if route == "escape":
        script = "window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));"
    elif route == "selected-local":
        script = """document.querySelector('[data-instance-id="local"] button').click();"""
    else:
        script = "[...document.querySelectorAll('button')].find(b=>b.textContent==='Back to Workbench').click();"
    f["send"](action="eval", script=script)
    return f["wait"]("reply", lambda event: event.get("action") == "cancel")


def local_lifecycle(f, operation, origin=None):
    f["send"](action=operation, **({"origin": origin} if origin else {}))
    f["wait"](operation)
    if operation == "runtime_offline":
        navigation = f["wait"]("navigation", lambda event: event["origin"] == "null" and event["path"] == "/")
        assert navigation["allowed"], navigation
    finished = f["wait"]("replacement_finished")
    assert finished["ok"], finished
    return presentation_snapshot(f)


@pytest.mark.parametrize("account_native", [{"shell": True, "lifecycle": True}], indirect=True)
@pytest.mark.parametrize("transition", ["rebind", "offline", "offline-online"])
@pytest.mark.parametrize("route", ["back", "escape", "selected-local"])
def test_retained_local_lifecycle_returns_through_actual_account_action(account_native, transition, route):
    """AUTH-SETUP-176: trusted local publication keeps the retained account return live."""
    f = account_native
    assert password(f)["ok"]
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    original = active(f)
    f["present"]("instances" if route == "selected-local" else "account")
    eventually(f, lambda _: f["js"]("""return Boolean(document.querySelector('[data-instance-id="local"] button'));"""
                                   if route == "selected-local" else "return [...document.querySelectorAll('button')].some(b=>b.textContent==='Back to Workbench');"))
    account = presentation_snapshot(f)
    if transition.startswith("offline"):
        published = local_lifecycle(f, "runtime_offline")
    if transition != "offline":
        published = local_lifecycle(f, "runtime_rebind", f["alternate_origin"])
    assert published["label"] == account["label"] and published["document"] == account["document"]
    assert published["runtime"]["label"] != original["label"]
    reply = account_return_action(f, route)
    # Successful return retires its invoking document. The command's final
    # receiver fence must withhold its reply, rather than revive old authority.
    assert reply["error"] == "untrusted_desktop_caller", {"reply": reply, "published": published}
    settled = eventually(f, lambda state: len(state["views"]) == 1 and not active(f)["label"].startswith("main-account-"))
    assert settled["state"]["connection"]["committed"]["kind"] == "local"
    destination = active(f)["url"]
    assert destination == ("tauri://localhost/" if transition == "offline" else f["alternate_origin"] + "/")
    if transition != "offline":
        assert active(f)["label"] == published["runtime"]["label"]


@pytest.mark.parametrize("account_native", [{"shell": True, "settings": True}], indirect=True)
def test_retained_notifications_page_replays_native_preference_on_reveal(account_native):
    """Actual SettingsNotificationsPage and native preference owner retain one document."""
    f = account_native
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    original = active(f)["label"]
    # Mount the actual settings consumer after the local Runtime is admitted,
    # as ordinary Settings navigation does; startup candidates have no authority.
    f["send"](action="eval", script="location='/settings/notifications';")
    f["wait"]("loaded", lambda event: event["url"].endswith("/settings/notifications"))
    selector = '[role="switch"][aria-label="Desktop app notifications"]'
    read_switch = "return document.querySelector(" + json.dumps(selector) + ")?.getAttribute('aria-checked');"
    eventually(f, lambda _: f["js"](read_switch) == "true")
    marker = f["js"]("window.notificationMarker=crypto.randomUUID();history.pushState({fixture:true},'', '/settings/notifications#retained');document.querySelector('#retained-draft').value='unsent fixture draft';return [window.notificationMarker,location.href,history.length];")
    f["js"]("window.preferenceEvents=[];window.miniEvents=[];window.addEventListener('avibe:desktop-notifications',e=>window.preferenceEvents.push(e.detail));window.addEventListener('avibe:desktop-pet-settings',e=>window.miniEvents.push(e.detail));return true;")
    f["present"]("account")
    account = active(f)["label"]
    f["send"](action="shell_control", operation="toggle_notifications")
    f["wait"]("shell_control", lambda event: event["operation"] == "toggle_notifications")
    f["send"](action="shell_status")
    assert f["wait"]("shell_status")["notifications_enabled"] is False
    # Both hidden retained Runtime and active Account remain denied command authority.
    for label in [original, account]:
        denied = f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_notifications_set_enabled',{enabled:true}).then(()=>false,()=>true);", label)
        assert denied
    assert f["js"]("return window.preferenceEvents;", original) == []
    reveal_start = len(f["history"])
    reply = account_return_action(f, "back")
    assert reply["error"] == "untrusted_desktop_caller"
    eventually(f, lambda _: active(f)["label"] == original)
    # This is a real producer completion (the Mini sibling's reveal callback),
    # followed by the actual retained React switch; no absence sleep is used.
    eventually(f, lambda _: any(event.get("event") == "mini_settings_dispatched" and event["label"] == original
                               for event in f["history"][reveal_start:]))
    assert f["js"](read_switch) == "false"
    assert f["js"]("return [window.notificationMarker,location.href,history.length];") == marker
    assert f["js"]("return document.querySelector('#retained-draft').value;") == "unsent fixture draft"
    assert f["js"]("return window.miniEvents.length;") > 0
    start = len(f["history"])
    f["send"](action="eval", script="document.querySelector(" + json.dumps(selector) + ").click();")
    assert f["wait"]("notification_preference_saved")["enabled"] is True
    eventually(f, lambda _: f["js"](read_switch) == "true")
    saved = [event for event in f["history"][start:] if event.get("event") == "notification_preference_saved"]
    assert len(saved) == 1, saved
    dispatched = [event["label"] for event in f["history"] if event.get("event") == "notification_preference_dispatched"]
    assert set(dispatched) == {original}, dispatched


@pytest.mark.parametrize("account_native", [{"shell": True, "lifecycle": True}], indirect=True)
@pytest.mark.parametrize("navigation_order", ["before-replacement", "candidate-held"])
def test_retained_local_replacement_cannot_reauthorize_a_navigated_return_receipt(account_native, navigation_order):
    """A trusted rebind cannot heal an account receipt invalidated by arbitrary navigation."""
    f = account_native
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    runtime = active(f)["label"]
    f["present"]("account")
    before = presentation_snapshot(f)
    if navigation_order == "candidate-held":
        f["policy"]["hold_local_document"] = True
        f["send"](action="runtime_rebind", origin=f["alternate_origin"])
        f["wait"]("runtime_rebind")
        assert f["local_document_entered"].wait(10)
    # The new validated local origin is allowed by the real production policy
    # even while its separate candidate is waiting for its document.
    origin = f["alternate_origin"] if navigation_order == "candidate-held" else f["local_origin"]
    f["send"](action="eval", label=runtime, script="location=" + json.dumps(origin + "/settings/general") + ";")
    f["wait"]("loaded", lambda event: event["url"].endswith("/settings/general"))
    navigated = presentation_snapshot(f)
    assert navigated["runtime"]["document"] != before["runtime"]["document"]
    assert navigated["returning"] == before["returning"]
    if navigation_order == "candidate-held":
        f["policy"]["hold_local_document"] = False
        f["local_document_release"].set()
        assert f["wait"]("replacement_finished")["ok"]
        replaced = presentation_snapshot(f)
    else:
        replaced = local_lifecycle(f, "runtime_rebind", f["alternate_origin"])
    assert replaced["runtime"]["label"] != runtime
    reply = account_return_action(f, "back")
    assert {"returning": replaced["returning"], "error": reply["error"], "active": active(f)["label"]} == {
        "returning": before["returning"], "error": "instance_session_missing", "active": before["label"],
    }


@pytest.mark.parametrize("account_native", [{"shell": True, "lifecycle": True}], indirect=True)
def test_retained_local_replacement_new_owner_retires_only_the_held_candidate(account_native):
    """Cancel defeats an in-flight lifecycle load; its late page cannot become a return receipt."""
    f = account_native
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    original = active(f)["label"]
    marker = f["js"]("window.retainedMarker=crypto.randomUUID();return window.retainedMarker;")
    f["present"]("account")
    account = presentation_snapshot(f)
    f["policy"]["hold_local_document"] = True
    start = len(f["history"])
    f["send"](action="runtime_rebind", origin=f["alternate_origin"])
    f["wait"]("runtime_rebind")
    assert f["local_document_entered"].wait(10)
    held = presentation_snapshot(f)
    assert held["runtime"]["label"] == original and held["returning"] == account["returning"]
    candidate = next(view["label"] for view in f["state"]()["views"]
                     if view["label"] not in [original, account["label"]])
    f["send"](action="eval", script="[...document.querySelectorAll('button')].find(b=>b.textContent==='Back to Workbench').click();")
    eventually(f, lambda _: presentation_snapshot(f)["owner"] != account["owner"])
    f["policy"]["hold_local_document"] = False
    f["local_document_release"].set()
    eventually(f, lambda _: any(event.get("event") == "replacement_finished" and event["owner"] == account["owner"]
                               for event in f["history"][start:]))
    stale = next(event for event in f["history"][start:] if event.get("event") == "replacement_finished"
                 and event["owner"] == account["owner"])
    assert not stale["ok"], stale
    # The new owner's destination follows the current rebind. The old candidate
    # must be retired; it cannot publish itself or lend its label to Cancel.
    settled = eventually(f, lambda state: len(state["views"]) == 1 and active(f)["label"] != account["label"])
    assert candidate not in [view["label"] for view in settled["views"]]
    assert active(f)["url"] == f["alternate_origin"] + "/"
    assert active(f)["label"] != original
    assert f["js"]("return window.retainedMarker ?? null;") is None
    assert marker
    dispatched = [event["label"] for event in f["history"][start:]
                  if event.get("event") in ["notification_preference_dispatched", "mini_settings_dispatched"]]
    assert candidate not in dispatched and account["label"] not in dispatched and original not in dispatched


@pytest.mark.parametrize("account_native", [{"shell": True, "lifecycle": True}], indirect=True)
def test_retained_remote_document_ignores_local_rebind_and_preference_replay(account_native):
    """Local lifecycle changes do not retarget a committed remote return receipt."""
    f = account_native
    f["identity"]["instances"] = [catalog_entry()]
    assert password(f)["ok"]
    dispatch(f, {"type": "select_instance", "instance_id": "studio"})
    eventually(f, lambda state: state["state"]["connection"]["committed"]["kind"] == "remote")
    remote = active(f)["label"]
    marker = f["js"]("window.retainedMarker=crypto.randomUUID();history.pushState({},'', '/?remote=retained');return [window.retainedMarker,location.href,history.length];")
    f["present"]("account")
    account = presentation_snapshot(f)
    start = len(f["history"])
    for operation in ["runtime_offline", "runtime_rebind"]:
        f["send"](action=operation, **({"origin": f["alternate_origin"]} if operation == "runtime_rebind" else {}))
        f["wait"](operation)
        f["wait"]("local_rebound_remote_preserved")
        snapshot = presentation_snapshot(f)
        assert snapshot["runtime"] == account["runtime"]
        assert snapshot["returning"] == account["returning"]
    f["send"](action="shell_control", operation="toggle_notifications")
    f["wait"]("shell_control", lambda event: event["operation"] == "toggle_notifications")
    for label in [remote, account["label"]]:
        assert f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_notifications_state').then(()=>false,()=>true);", label)
    assert account_return_action(f, "back")["error"] == "untrusted_desktop_caller"
    eventually(f, lambda state: len(state["views"]) == 1 and active(f)["label"] == remote)
    assert f["js"]("return [window.retainedMarker,location.href,history.length];") == marker
    assert not [event for event in f["history"][start:] if event.get("event") in
                ["notification_preference_dispatched", "mini_settings_dispatched"]]


@pytest.mark.parametrize("account_native", [{"shell": True, "lifecycle": True}], indirect=True)
def test_retained_local_return_receipt_survives_lifecycle_retry_after_retirement(account_native):
    """A valid receipt survives a lifecycle-only retry after its old role is actually detached."""
    f = account_native
    f["send"](action="escape")
    assert f["wait"]("escaped")["ok"]
    original = active(f)["label"]
    f["present"]("account")
    account = presentation_snapshot(f)
    f["send"](action="hold_retired_runtime")
    f["wait"]("control_ready")
    f["send"](action="runtime_rebind", origin=f["alternate_origin"])
    f["wait"]("runtime_rebind")
    f["wait"]("runtime_retired_held")
    roles = [view["label"] for view in f["state"]()["views"]]
    assert original not in roles
    candidate = next(label for label in roles if label != account["label"])
    assert f["js"]("return window.__TAURI_INTERNALS__.invoke('desktop_notifications_set_enabled',{enabled:false}).then(()=>false,()=>true);", candidate)
    assert presentation_snapshot(f)["returning"] == account["returning"]
    f["send"](action="runtime_rebind", origin=f["local_origin"])
    f["wait"]("runtime_rebind")
    f["send"](action="release_retired_runtime")
    assert f["wait"]("replacement_finished")["ok"]
    published = presentation_snapshot(f)
    assert published["label"] == account["label"] and published["owner"] == account["owner"]
    assert published["returning"] == {key: published["runtime"][key] for key in ["label", "document"]}
    assert published["runtime"]["label"] != original
    assert account_return_action(f, "back")["error"] == "untrusted_desktop_caller"
    eventually(f, lambda state: len(state["views"]) == 1 and active(f)["label"] == published["runtime"]["label"])
    assert active(f)["url"] == f["local_origin"] + "/"
