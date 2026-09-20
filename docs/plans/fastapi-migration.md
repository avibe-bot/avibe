# UI Server: Flask → FastAPI Migration

## Background

`vibe/ui_server.py` is a 2,380-line, 74-route Flask app served by `werkzeug.make_server(..., threaded=True)`. Every async helper from `vibe/api.py` is invoked through `asyncio.run(coro)` (9 sites), which spins up a **fresh event loop per HTTP request**.

This collides head-on with the long-lived singleton state in agent backends — most prominently `OpenCodeServerManager`, which caches `aiohttp.ClientSession`, `asyncio.Lock`, and (worst of all) `asyncio.subprocess.Process` objects. Subprocess transports bind their internal waiter `Future` to the loop that created them. The singleton outlives the loop. Any later request from a new loop that touches `process.wait()`, `lock.acquire()` etc. raises:

```
RuntimeError: got Future <Future pending> attached to a different loop
```

Two recent PRs (#282, #296) added defensive bandages: `_process_loop` / `_lock_loop` / `_http_session_loop` guards that detect and re-create per-loop primitives. These keep the lid on — but every new feature on the UI server (chat streaming, skills config, live agent state) is one missing guard away from re-introducing the bug, and Flask's WSGI threading model offers nothing better than `asyncio.run`.

The product direction makes this terminal: the UI is growing to host **chat with an agent**, **agent configuration**, **skills configuration**, and similar live, long-lived interactions. Chat in particular implies WebSocket or SSE — Flask + `asyncio.run` is the wrong substrate for that.

## Goal

Replace the Flask + `asyncio.run`-per-request model with **FastAPI + uvicorn** (ASGI, single persistent event loop). Acceptance criteria:

1. Every HTTP route in `vibe/ui_server.py` is preserved at the same path + method, returning the same response shape — the React UI in `ui/` should not need any change.
2. All async helpers in `vibe/api.py` are reachable via direct `await` from FastAPI handlers — no `asyncio.run`, no `run_coroutine_threadsafe` from request paths.
3. The dedicated `vibe-oauth-loop` daemon thread (and the `_submit_oauth_coro` helper) is **removed** — FastAPI's uvicorn loop becomes the single UI loop, the OAuth service runs natively on it.
4. The OpenCodeServerManager `_process_loop` / `_lock_loop` / `_http_session_loop` guards stay (defense in depth), but they should not trip in normal operation any more — verify via regression.
5. The test suite (currently ~112 hits across tests using `flask.Flask.test_client`-style APIs) is migrated to `fastapi.testclient.TestClient`. Tests stay green.
6. A WebSocket-capable route exists end-to-end (one trivial echo route is enough; chat itself is out of scope for this PR) — proves the substrate is ready.
7. Local install via `uv tool install` still works. The local Incus regression environment starts cleanly and serves the UI at the same host/port.

Out of scope for this PR:
- Implementing chat. Just prove the WebSocket pipe is wired.
- Implementing skills config. Just keep the existing endpoints working.
- Rewriting `vibe/api.py`'s sync wrappers into async. Migrate one by one as endpoints are touched; keep the rest sync and call via `run_in_threadpool` if needed.
- Removing the per-loop defensive guards in `OpenCodeServerManager`. They become belt-and-suspenders. Drop them in a follow-up PR after the FastAPI substrate has been in production for a release cycle.

## Solution

### Implementation strategy update after architecture review

The original plan suggested splitting `vibe/ui_server.py` into many FastAPI
routers immediately. After reviewing the current architecture, that is too
much churn for the first migration PR: it mixes the runtime substrate change
(WSGI/thread-per-request → ASGI/persistent loop) with a large file/module
reshuffle across 70+ routes.

Use a two-step design instead:

1. **Substrate migration first.** Replace Flask/Werkzeug with FastAPI/uvicorn
   while keeping `vibe/ui_server.py` as the single route surface. Add a small
   compatibility layer that supports the limited Flask API the file actually
   uses (`route`, `before_request`, `after_request`, `request`, `g`, `jsonify`,
   `redirect`, `send_file`, `Response`, and the existing test helpers). This
   keeps the first PR focused on runtime semantics and lets existing route
   handlers move mostly unchanged.
2. **Native FastAPI cleanup second.** Once tests and regression are green,
   convert routes to idiomatic FastAPI incrementally and split them into
   routers. Router extraction is valuable, but it should be a follow-up because
   it does not itself fix the cross-event-loop bug.

The compatibility layer must stay intentionally thin and local to the UI
server. It is a migration scaffold, not a new app framework. New endpoints
after this migration should use native FastAPI patterns directly.

Additional findings from the current codebase:

- `vibe/ui_server.py` has one local `_run_async()` helper that creates a fresh
  event loop in a thread for WeChat QR login and OpenCode options. This must go
  away with the Flask migration; FastAPI handlers can await those coroutines
  directly or call blocking work through `run_in_threadpool`.
- `vibe/api.py` still has sync wrapper functions that call `asyncio.run(...)`
  for Telegram auth, SOCKS-backed Discord/Lark calls, OpenCode provider
  catalog/auth operations, and OAuth web flows. UI-reachable wrappers should be
  converted as their routes are touched. Test-only or non-UI `asyncio.run`
  calls are not part of this acceptance criterion.
- Tests should not be migrated only at the end. Remote-access auth coverage is
  split across `tests/test_ui_server_fastapi.py` and
  `tests/ui_server_test_helpers.py`; keep those focused tests green after the
  substrate layer lands, then carry the rest of the UI test files surface by
  surface.
- The current `run_ui_server()` tests monkeypatch `werkzeug.make_server`.
  They need to be rewritten around `uvicorn.Server` and the nonblocking
  remote-access reconcile behavior.
- Proxy/remote address semantics are product-critical. The compatibility
  request object should preserve the existing `request.remote_addr`,
  `request.host`, forwarded header, and test `environ_base={"REMOTE_ADDR": ...}`
  behavior before any native Starlette rewrite.

### Stack choice

- **Web framework**: FastAPI (built on Starlette). Native async, first-class WebSocket, dependency-injection, OpenAPI generation, Pydantic body validation.
- **Server**: uvicorn (single worker — singleton state requires it; multi-worker is wrong for this codebase anyway because the IM controller is in-process).
- **Dependencies to add** (in `pyproject.toml`):
  - `fastapi >= 0.110`
  - `uvicorn[standard] >= 0.27`
  - `python-multipart >= 0.0.9` (for any future form / upload routes)
  - `httpx >= 0.27` (already a transitive dep; needed explicitly for `TestClient`)
- **Dependencies to remove** *after* tests pass: `flask` (transitive via something else? check), explicit `werkzeug` reference in `vibe/runtime.py` / `vibe/remote_access.py` comments stays as-is.

### Module reshape

Keep `vibe/ui_server.py` as the entry surface for the substrate migration.
After that is stable, split the routes into routers under `vibe/ui_routers/`
so a 2,380-line file doesn't grow into a 4,000-line file. Suggested split:

```
vibe/
  ui_server.py            # app factory, middleware, lifespan, run_ui_server
  ui_routers/
    __init__.py
    health.py             # /health, /status
    setup.py              # /setup/*, /auth/callback (setup wizard)
    config.py             # /config, /settings, /sessions
    backends.py           # /backend/*, /agent/*, /cli/detect
    backend_oauth.py      # /backend/<b>/auth/oauth/*, /backend/opencode/provider/*
    backend_codex.py      # /backend/codex/auth, /codex/agents, /codex/models
    backend_claude.py     # /backend/claude/auth, /claude/agents, /claude/models, /backend/claude/api-key/remove
    backend_opencode.py   # /backend/opencode/providers, /opencode/*
    im_platforms.py       # /slack/*, /discord/*, /telegram/*, /lark/*, /wechat/*
    logs.py               # /logs, /version, /upgrade, /doctor, /browse
    remote_access.py      # /remote-access/*
    files.py              # SPA catch-all, /assets/*, /favicon, attachments
```

Routers are wired in `ui_server.py` via `app.include_router(...)`. Keeps each file under ~400 lines and gives Codex a natural unit of work. This is a follow-up cleanup after the ASGI runtime is live, not a prerequisite for removing Flask.

### Translation table (Flask → FastAPI)

| Flask | FastAPI |
| --- | --- |
| `@app.route("/x", methods=["POST"])` | `@router.post("/x")` |
| `return jsonify({...})` | `return {...}` (FastAPI auto-encodes) |
| `return jsonify({...}), 400` | `raise HTTPException(status_code=400, detail={...})` *or* `return JSONResponse({...}, status_code=400)` |
| `request.json` (sync) | `payload: dict = Body(...)` — declare the body param |
| `request.args.get("x")` | `x: str = Query(None)` |
| `request.headers.get("X-Y")` | `request: Request` then `request.headers.get("X-Y")` |
| `request.cookies.get("c")` | `c: str = Cookie(None)` |
| `request.remote_addr` | `request.client.host` (with proxy-header logic in middleware) |
| `g.something = x` (per-request) | `request.state.something = x` |
| `@app.before_request` | `@app.middleware("http")` async function (see §Middleware) |
| `@app.after_request` | Same middleware, set response headers / cookies post-`await call_next(request)` |
| `Response(status=302, headers={"Location": url})` | `RedirectResponse(url, status_code=302)` |
| `send_file(path, mimetype=...)` | `FileResponse(path, media_type=...)` |
| `Response(generator(), mimetype="text/event-stream")` | `StreamingResponse(generator(), media_type="text/event-stream")` |
| `werkzeug.exceptions.HTTPException` | `fastapi.HTTPException` |
| `make_server(host, port, app).serve_forever()` | `uvicorn.run(app, host=host, port=port, log_config=...)` |

### Middleware: porting `before_request` / `after_request`

The Flask app has three security gates run on every request, plus CSRF cookie management. Port each as one async `@app.middleware("http")` function in `ui_server.py`. The current Flask hooks are:

1. **CSRF gate** (rejects writes without `X-Vibe-CSRF-Token` matching the cookie). Skip for GET/HEAD, `/health`, `/status`, `/auth/callback`, `/e2e/simulate-interaction`, and loopback-only routes. (See `vibe/ui_server.py:507–530`.)
2. **Remote access auth gate** (`@app.before_request` at line 905). Decides whether the request is local-loopback or remote, sets `g.remote_access_authenticated = bool`, redirects unauthenticated remote requests to the vibe-cloud OAuth URL.
3. **Setup wizard gate** (`@app.before_request` at line 939). When the setup wizard is incomplete, restricts navigation to `/setup/*` and a few hard-coded asset paths.
4. **CSRF cookie refresh** (`@app.after_request` at line 961). Sets the CSRF cookie if missing.
5. **Remote-access cookie refresh** (`@app.after_request` at line 967). Slides the rolling session.

Pattern: one middleware function per concern, ordered explicitly (FastAPI middlewares execute in reverse-registration order on the request, forward order on the response). Use `request.state` for inter-middleware/route handoff.

### The OAuth flow loop: collapse `vibe-oauth-loop`

`vibe/api.py` runs a dedicated daemon thread (`vibe-oauth-loop`) hosting an `asyncio` loop because Flask handlers can't `await` directly — every OAuth status read goes through `asyncio.run_coroutine_threadsafe(coro, _oauth_loop).result(timeout=...)`. With FastAPI, the handler is `async def` and the OAuth service runs on the uvicorn loop natively.

Concretely:
- Delete `_oauth_loop`, `_oauth_loop_thread`, `_start_oauth_event_loop()`, `_submit_oauth_coro()` from `vibe/api.py`.
- Every existing wrapper like:
  ```python
  def start_oauth_web(backend, ...):
      service = _get_oauth_service()
      flow = _submit_oauth_coro(service.start_web_setup(...))
      return {...}
  ```
  becomes:
  ```python
  async def start_oauth_web(backend, ...):
      service = _get_oauth_service()
      flow = await service.start_web_setup(...)
      return {...}
  ```
- The FastAPI route awaits the new async function directly.
- `_get_oauth_service()` keeps its module-level singleton, but the lazy initialization no longer needs to start a thread — it can simply hold the service. The `AgentAuthService` stays unchanged.

### `vibe/api.py` async migration: incremental

`vibe/api.py` is currently a mix of sync functions, sync wrappers around async, and a few async helpers. Don't rewrite the whole file in this PR.

Migration rule:
- If an endpoint's handler used `asyncio.run(coro)`, convert that endpoint AND the function it called into `async def` (both ends).
- If the function does CPU-bound or blocking IO (e.g. subprocess + file IO), keep it sync and call it via `await run_in_threadpool(func, ...)` from the async endpoint. FastAPI ships `fastapi.concurrency.run_in_threadpool`.
- Functions not reachable from a Flask handler currently — leave them alone.

The 9 `asyncio.run(...)` sites in `vibe/api.py` are the hit list. After migration there should be zero.

### Server lifecycle

Replace `werkzeug.make_server` + `serve_forever()` with uvicorn:

```python
def run_ui_server(host: str, port: int) -> None:
    import uvicorn
    paths.ensure_data_dirs()
    config = _load_config_safely()
    if config is not None:
        init_sentry(config, component="ui", enable_fastapi=True)
        _start_remote_access_heartbeat_safely(config)

    uvicorn_config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_config=None,  # let our logging.py handle it
        access_log=False,  # we already log via our own access middleware if needed
        loop="asyncio",
        lifespan="on",
    )
    server = uvicorn.Server(uvicorn_config)
    _server = server
    # Background remote_access reconcile thread stays as-is
    threading.Thread(target=_reconcile_remote_access_for_ui_start, args=(config,), daemon=True).start()
    server.run()  # blocks
```

Use FastAPI's `lifespan` context to start / stop the OAuth service singleton (replacing the daemon-thread loop) and the Sentry FastAPI integration:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    _init_oauth_service()       # now lives on uvicorn's loop
    yield
    # shutdown
    await _shutdown_oauth_service()

app = FastAPI(lifespan=lifespan)
```

Port-bind retry loop (currently in `make_server` wrapper for EADDRINUSE handling) lives outside uvicorn — wrap `server.run()` in the same retry loop.

### Sentry integration

`init_sentry(..., enable_flask=True)` currently activates the Flask integration. Switch to FastAPI:
- `enable_flask=True` flag in `vibe/sentry_integration.py` becomes `enable_fastapi=True`.
- Inside, swap `sentry_sdk.integrations.flask.FlaskIntegration()` for `sentry_sdk.integrations.fastapi.FastApiIntegration()` and `sentry_sdk.integrations.starlette.StarletteIntegration()`.

### Tests

Tests use `app.test_client()` (Flask) extensively. Port pattern:

| Flask test | FastAPI test |
| --- | --- |
| `client = app.test_client()` | `client = TestClient(app)` from `fastapi.testclient` |
| `client.post("/x", json={...})` | `client.post("/x", json={...})` (identical API!) |
| `client.set_cookie(name, value, domain=...)` | `client.cookies.set(name, value, domain=...)` |
| `response.get_json()` | `response.json()` |
| `response.status_code` | `response.status_code` |

`TestClient` is a thin sync wrapper around `httpx.Client`; it transparently runs the ASGI app under the hood. Most assertions stay byte-identical. Async fixtures (if any) switch to `pytest-asyncio` patterns.

The historical remote-access auth suite was later split into focused FastAPI
tests; patches like `monkeypatch.setattr("vibe.ui_server.psutil...", ...)` still
work because the module path is unchanged.

### WebSocket smoke route

Prove the substrate by adding **one trivial WebSocket route** (e.g. `/ws/echo`) in `health.py` or a new `ws.py`:

```python
@router.websocket("/ws/echo")
async def ws_echo(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            msg = await websocket.receive_text()
            await websocket.send_text(f"echo: {msg}")
    except WebSocketDisconnect:
        pass
```

The chat feature will land in a follow-up PR using this same pattern. We just need to prove the wiring on this PR.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| 74 routes — long migration, easy to miss subtle response-shape differences | Migrate one router file at a time, run the existing test for that surface after each, smoke via curl. Don't merge until ALL test files pass and a manual smoke against the React UI is clean. |
| Tests embedded in Flask test client semantics (cookies, sessions) | TestClient is largely API-compatible; deal with cookie domain quirks one test file at a time. Allocate ~25% of the migration budget for tests. |
| CSRF / remote_access middleware: ordering bugs | Port one middleware at a time with focused FastAPI tests in `tests/test_ui_server_fastapi.py`. |
| Sentry integration regression | Init in lifespan startup; verify Sentry breadcrumbs still appear via the regression env's dev key. |
| WSGI-specific subprocess launching pattern in `run_ui_server` | Uvicorn handles this natively in-process; keep the EADDRINUSE retry loop. |
| Frontend assumptions about specific status codes / response shapes | Don't refactor any payload shape during the migration. Same path, same method, same JSON. |
| Streaming responses (logs tail, OAuth status long-poll) | All current "streaming" looks like polling, not SSE. Confirm during route porting; if any real SSE shows up, convert to `StreamingResponse`. |
| Multi-worker temptation | uvicorn workers >1 = multiple Python processes = singleton state breaks everything. Stay at `workers=1`. Document this in `run_ui_server` and in CLAUDE.md if needed. |
| Local install path (`uv tool install vibe-remote`) | `pyproject.toml` already declares `vibe` as a script entry point. Adding fastapi/uvicorn as deps means a slightly bigger wheel — still well under 5 MB. |

## Implementation phases

Each phase ends with a green test run + a manual smoke. Commit between phases.

1. **Bootstrap.** Add fastapi/uvicorn/python-multipart/httpx to `pyproject.toml`. Create `vibe/ui_routers/` directory with empty `__init__.py`. Create the new `app = FastAPI(lifespan=lifespan)` in `vibe/ui_server.py` ALONGSIDE the existing Flask `app` (rename Flask app temporarily, e.g. `_legacy_flask_app`). Wire `run_ui_server` to dispatch to whichever app a `VIBE_UI_FRAMEWORK=fastapi` env var selects. Implement `/health` on FastAPI as the canary route.
2. **Middleware.** Port CSRF / remote_access auth / setup wizard gate / CSRF cookie / remote_access cookie hooks as FastAPI middleware. Keep the focused FastAPI auth tests green.
3. **Static + Setup.** Port the SPA catch-all, `/setup/*`, `/auth/callback`, asset routes. The setup wizard is high-traffic; smoke through `./scripts/run_regression.sh` end-to-end after this phase.
4. **Backends + agent CLI lifecycle.** Port `/backend/*`, `/agent/*`, `/cli/detect`. Drop `asyncio.run` from the corresponding `vibe/api.py` helpers as you go.
5. **Backend OAuth + per-provider.** Port `/backend/<b>/auth/oauth/*` and `/backend/opencode/provider/*`. Delete `vibe-oauth-loop` thread; `_submit_oauth_coro` callers become `await`. This phase **fixes the cross-loop bug at the root**.
6. **IM platform credentials.** Slack / Discord / Telegram / Feishu / WeChat routes. WeChat QR login long-poll is a candidate for `StreamingResponse` (or stays as poll — keep behavior identical).
7. **Config / settings / sessions / logs / version / remote-access / browse.** The remaining bulk.
8. **WebSocket smoke route** + frontend hello-world dialog (optional) to prove the substrate.
9. **Cleanup.** Remove `VIBE_UI_FRAMEWORK` flag, delete `_legacy_flask_app`, drop `flask` from `pyproject.toml`, regenerate lockfile. Update `vibe/sentry_integration.py` to use `FastApiIntegration`/`StarletteIntegration`.
10. **Tests.** Convert `app.test_client()` → `TestClient(app)`. The 5–6 test files using Flask testing patterns need a uniform pass.

Codex review after each phase keeps the surface area manageable.

## Acceptance checklist (final PR)

- [x] `pyproject.toml` declares fastapi + uvicorn + python-multipart + httpx; no `flask` dependency.
- [x] `vibe/ui_server.py` exports a single `app: FastAPI`. No Flask import anywhere in `vibe/`.
- [x] `vibe-oauth-loop` thread is gone. `_submit_oauth_coro` is gone.
- [x] `asyncio.run` does not appear in `vibe/api.py`.
- [x] A trivial WebSocket echo route proves the ASGI substrate end-to-end.
- [ ] All 74 existing routes return identical responses for golden inputs. UI in `ui/` is byte-identical and works against the new server.
- [ ] `./scripts/run_regression.sh` brings up a healthy container; `/health` returns ok; the React UI loads; setup wizard works; OpenCode provider OAuth flow works without cross-loop errors.
- [ ] `pytest tests/` passes.
- [ ] `vibe restart && vibe status` works locally.
- [x] Repo guidance is updated to mention FastAPI / uvicorn and prefer native FastAPI patterns for new UI routes.

## Rollback plan

The migration is too large to be a single commit. Land it as a stacked PR series (one per phase) or one big PR if Codex prefers. If a release goes out and a critical regression surfaces, the fallback is to revert the merge commit — `gh-vX.Y.Zrc*` pre-releases of the pre-FastAPI version stay installable via `uv tool install --force gh-vX.Y.ZrcN`. Document the last pre-migration tag prominently in the PR body.

## Open questions for the implementer (Codex)

1. **Phase granularity** — single PR or stacked PRs? Single is simpler to verify; stacked is easier to review. Lean stacked if review burden looks heavy.
2. **`request.remote_addr` proxy header logic** — the Flask path has bespoke handling for `X-Forwarded-For` / `X-Forwarded-Host` to support Cloudflare Tunnel. Verify Starlette's `request.client.host` honors `forwarded` properly; if not, port the bespoke logic into middleware that writes `request.state.client_host`.
3. **Logging integration** — uvicorn has its own access log format. Decide whether to disable uvicorn's access log (current Flask doesn't have one anyway) or pipe it through `vibe/logging.py`.
4. **Sentry** — confirm `FastApiIntegration` + `StarletteIntegration` is the right combination (not just one of them).
5. **Test client async helpers** — if any test currently uses Flask's `with app.test_request_context()` pattern, port to `httpx.AsyncClient` for the async equivalent.
