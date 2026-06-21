# Phase 2 — Hot platform reconcile (apply platform changes with no service restart)

## Background

Phase 1 (PR #566) made platform config changes do a **service-only restart** that
keeps the Web UI process alive. Phase 2 removes the restart entirely for the
common case: enabling/disabling a platform should just start/stop **that one
platform's background task**, not bounce the whole service.

## Current mechanics (verified)

- `MultiIMClient` (`modules/im/multi.py`) runs **one daemon thread per platform**;
  each thread calls the adapter's blocking `client.run()`. It has only
  all-or-nothing `stop()`/`shutdown()` — no per-platform start/stop.
- The IM runtime blocks on a worker thread (`Controller._run_im_runtime`,
  `core/controller.py:513`), spawned from `controller.run()`; the main thread
  owns the asyncio loop.
- `IMFactory.create_clients()` builds `im_clients` **once** in
  `Controller._init_modules` (`controller.py:144-227`). `_refresh_config_from_disk`
  hot-reloads a SUBSET (message filters/i18n/require_mention) but NOT platform
  enablement, credentials, settings managers, or agent routes.
- Internal command channel: `core/internal_server.py` serves HTTP over a unix
  socket (`dispatch.sock`) on the controller's loop; `vibe/internal_client.py` is
  the client. Existing commands: dispatch_async, dispatch, turn-state, cancel,
  health. The UI process already calls it (e.g. bootstrap → turn_state).

## Design

1. **`MultiIMClient.add_client(platform, client)` / `remove_client(platform)`** —
   start one new daemon thread / signal-stop + join one thread, mutating
   `self.clients` + `self._threads` under a lock (routing reads must not see a
   half-removed client). `run()` idles until explicit stop, even when the
   runtime client set is empty.
2. **`Controller.reconcile_platforms(new_config)`** (async, on the loop) — diff
   the new enabled set vs running and:
   - removed → `remove_client` + drop its settings manager;
   - added → build client via `IMFactory`/descriptor, formatter, settings
     manager, agent route, register callbacks, `add_client` (start its thread);
   - enabled + runtime credential/signature change → `remove_client` +
     `add_client` rebuild;
   - recompute `enabled_platforms` + the derived `primary_platform`;
   - keep the single `MultiIMClient` wrapper alive for all topologies.
   Sync `stop()` calls wrapped in `asyncio.to_thread`/`call_soon_threadsafe`.
3. **`POST /internal/reconcile-platforms`** on the internal server → calls
   `controller.reconcile_platforms`.
4. **`POST /api/config`** (UI process): after saving, if the platform fields
   changed, send the internal reconcile command instead of scheduling a restart.
   The platforms page stops calling `control('restart')` for enable/disable.

### Always-wrap / workbench-only shape

The controller and `IMFactory.create_client()` now always expose a
`MultiIMClient` runtime wrapper:

- 4-platform / 1-platform configs: wrapper owns those external IM clients.
- Workbench-only / disabled-all configs: wrapper owns zero external IM clients
  and idles; `AvibeBot` stays registered separately in `controller.im_clients`
  as the in-process delivery target.

This avoids single-vs-multi branching during reconcile and prevents disabling
all IM platforms from making `im_client.run()` return, which would otherwise
stop the controller loop.

## Per-adapter hot-stop verdict

| Platform | Clean single-stop? | Notes |
|---|---|---|
| Slack | ✅ | async stop-event + socket close |
| Discord | ✅ | discord.py `client.close()` |
| Telegram | ✅ | polling loop breaks on event |
| WeChat | ✅ | poll-task cancel + stop event |
| **Feishu/Lark** | ✅ | lark-oapi exposes no public stop, so `FeishuBot.stop()` uses the SDK client's private async `_disconnect()`, then stops the SDK module event loop to release its infinite `_select()` sleep and joins the nested `feishu-ws` thread |

## Decisions (locked — full scope, "一次到位")

- **D1 — Feishu: patch for clean hot-stop.** Expose a stop on the nested lark
  WS thread so Feishu can be hot-removed like the others (no fallback restart).
- **D2 — Credential changes: rebuild, not per-adapter reconnect.** A credential
  edit on an enabled platform = `remove_client` + `add_client` (tear down the
  old adapter, build a fresh one with the new config, start it). Reuses the
  add/remove machinery — no per-adapter "reconnect" code. Works for all 5 once
  every adapter stops cleanly (D1).
- **D3 — Trigger.** `POST /api/config` decides reconcile-vs-restart by diffing
  the platform fields; manual Dashboard/Service restart buttons unchanged. With
  full scope, platform enable/disable/credential changes ALL hot-reconcile (no
  restart); a service-only restart remains only for non-platform config that
  the running service doesn't hot-apply.

## Risks & mitigations

- Routing to a half-removed client → KeyError: mutate `clients` under a lock;
  routing reads a snapshot.
- A stuck `client.run()` won't join: bounded `join` timeout + the thread is a
  daemon (dies at process exit); log a leak warning.
- Callback re-registration: a newly added client must have callbacks wired
  before its thread starts so early inbound isn't dropped.
- Settings-manager file lifecycle for a newly added platform: lazy-load on
  first access (existing pattern).
- Feishu stop relies on lark-oapi private internals because the SDK has no
  public close API. Keep the test coverage around `_disconnect()` + loop stop so
  SDK upgrades fail loudly if those internals change.
- If the internal reconcile socket is unavailable after saving platform config,
  `/api/config` schedules the existing service-only restart fallback instead of
  leaving the persisted config unapplied.
