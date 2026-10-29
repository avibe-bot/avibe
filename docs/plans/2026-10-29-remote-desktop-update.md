# Remote Desktop Update Contract

## Goal

An Instance Member or Owner using a browser can request an update of a
desktop-managed host. The request always enters the existing signed Rust
updater. By default the host prompts a local user before installation. A local
desktop-only preference may allow the same request to install without another
click.

For `managed_by: "desktop"`, this change does not reuse `/api/upgrade`, pip, or
uv. Non-desktop packaged installs retain the existing Python `/api/upgrade`
path, and source checkouts retain their existing build-refresh behavior. The
change does not add remote control of the desktop preference.

## Trust Boundaries

- `POST /api/desktop/update/request` is explicitly Member-level, like
  `/api/upgrade`, and remains behind the existing same-origin and CSRF
  middleware. Editors and Viewers receive the same 403 response.
- The Runtime is the only component that writes
  `desktop-update-request.json`. A request is written only after authorization,
  interruption confirmation, and desktop-managed availability checks pass.
- SSE event `desktop.update.requested` is a nudge only. The shell ignores its
  payload and acts only after reading and validating the request file.
- The shell performs a fresh release check on its current channel, requires the
  authenticated manifest's version to equal the requested target, and uses the
  existing signature, artifact verification, replacement, restart, and bundled
  Runtime hand-over path.
- `remote-install.json` is shell-owned and defaults to disabled. It is read and
  written only through Tauri IPC granted to the main Workbench window at the
  adopted loopback origin. HTTP clients can only read the resulting value from
  `/api/version`. The shell rejects preference changes while a remote request
  is active, so the delivery reported at acknowledgement stays fixed for that
  request.

## Request File

`desktop-update-request.json` lives beside `update-status.json`.

```json
{
  "schema_version": 1,
  "request_id": "UUID",
  "target": "3.2.4-rc.1",
  "state": "pending",
  "created_at": "2026-10-29T02:00:00Z",
  "expires_at": "2026-10-29T02:30:00Z"
}
```

The Runtime writes the file with create, flush, `fsync`, and atomic replace
after the Member role and browser mutation guards pass. The only states are
`pending` and `expired`. On a ten-second acknowledgement timeout, the Runtime
atomically changes the matching pending file to `expired` before returning
`desktop_shell_unavailable`. The shell re-reads the file before installation
and accepts only the same unexpired pending request.

One request may be active. A retry with the same target while that request is
non-terminal returns the existing `request_id`; another target returns
`update_in_progress`. A terminal, declined, or expired request is never reused:
the next accepted request gets a new UUID and replaces the old file.

## Status File

The shell extends schema version 1 of `update-status.json` additively:

```json
{
  "schema_version": 1,
  "phase": "available",
  "current": "3.2.3-rc.11",
  "latest": "3.2.4-rc.1",
  "channel": "test",
  "has_update": true,
  "checked_at": "2026-10-29T02:00:00Z",
  "remote_install_allowed": false,
  "remote_request": {
    "request_id": "UUID",
    "target": "3.2.4-rc.1",
    "phase": "awaiting_host",
    "progress": null,
    "error_code": null,
    "updated_at": "2026-10-29T02:00:03Z"
  }
}
```

Remote phases are:

`requested -> acknowledged -> checking -> awaiting_host | downloading ->
installing -> restarting -> completed | failed | declined | expired`

`progress` is an integer percentage only while downloading. The Runtime parser
accepts older files without the new fields and ignores unknown fields. A malformed
remote subsection is dropped without discarding a valid ordinary update result.
`/api/version` relays `remote_install_allowed` and `remote_request` as read-only
data.

The shell preserves the remote subsection when publishing an ordinary check.
It writes every remote transition atomically. A restart leaves `restarting`
persisted. On startup, the new shell compares its package version with the
target and writes `completed` on equality or `failed/version_mismatch` otherwise.
An app that never starts leaves `restarting`; the remote UI eventually reports
that completion could not be confirmed.

## Endpoint

`POST /api/desktop/update/request`

```json
{
  "target_version": "3.2.4-rc.1",
  "confirm_interrupt": false
}
```

The endpoint returns the following stable errors:

- `not_desktop_managed`
- `no_update_available`
- `update_in_progress`
- `desktop_shell_unavailable`
- `confirmation_required`, with active Agent, Watch, and Task counts

The Member or Owner caller receives a success response only after the shell has
acknowledged the same request:

```json
{
  "ok": true,
  "request_id": "UUID",
  "delivery": "prompt"
}
```

`delivery` is `prompt` or `install`, derived from the shell-written
`remote_install_allowed` value after acknowledgement. The client cannot choose
it.

Active interruption counts include running foreground Agent work and queued or
running Harness Watch and scheduled Task runs. If the controller cannot answer,
the endpoint fails closed and requires explicit interruption confirmation.

The existing Member-level `POST /api/upgrade` remains the non-desktop packaged
installer. A new Workbench may send optional `confirm_interrupt: false` and
receive the same `confirmation_required` response before retrying with `true`.
An omitted field preserves the legacy behavior and response shape so an old
cached Workbench continues to upgrade. This PR does not change the endpoint's
permission level.

## Shell Behaviour

The shell responds to the SSE nudge, reads the file once when the Runtime
notification connection starts, and polls it every five seconds as a fallback.
The fallback interval is intentionally shorter than the Runtime's ten-second
acknowledgement deadline; a 30-second interval could only discover a request
after the Runtime had expired it. The shell serializes processing with the
existing updater busy gate and records `update_in_progress` rather than running
two updater operations.

For every accepted request it performs a fresh check. A target different from
the latest authenticated manifest fails with `target_mismatch`.

With remote installs disabled, the shell:

1. records `awaiting_host`;
2. restores and focuses the main window;
3. opens the existing update popover with remote-request context;
4. sends a native notification;
5. waits for Install or Ignore.

Ignore records `declined` and does not add the version to the global skip list.
No response for 30 minutes records `expired`.

With remote installs enabled, the shell proceeds through the same install
function used by the local popover. On Windows this remains safe for unattended
use only for the existing writable per-user install. Its pre-exit write probe
fails fast as `requires_host` when the installation needs elevation.

## Browser Behaviour

For `managed_by: "desktop"` without a Tauri bridge:

- Members and Owners see **Update on host**, confirmation, interruption counts,
  delivery mode, progress, restart/reconnect state, and the terminal result.
- Editors and Viewers see guidance to update in the desktop app on the host and
  no mutation control.
- During restart, the browser retries `/api/version` and confirms completion by
  the request terminal state and resulting version. A bounded timeout says the
  update was not confirmed and directs the user to the host.

The local desktop Workbench additionally probes the preference IPC. Browsers and
older shells hide the setting when the capability is absent.

For non-desktop builds:

- a package install keeps the existing Install action and `/api/upgrade` path;
- a new Workbench shares interruption confirmation and the existing
  restart/reconnect reload behavior;
- an old Workbench remains valid because confirmation is opt-in;
- a source checkout keeps the existing revision refresh and has no install
  action.

## Platform Findings and Residuals

- macOS stages on the installation filesystem, verifies bundle identity,
  architecture, and code signature, and restores the old app if the replacement
  rename fails. After a successful rename, it does not retain the old app until
  the new app passes a startup health check. The old Runtime remains serving
  until the new shell completes hand-over, but app-start failure has no automatic
  binary rollback in this change.
- Windows verifies installer identity, proves the install directory is writable,
  copies the old per-user install, and only then exits the shell. It invokes the
  NSIS installer silently and restores the old install on failure. A directory
  that needs elevation is rejected before shell exit.
- Installation interrupts active Agent, Watch, Task, and IM work. The API
  requires `confirm_interrupt` when known active work exists, but it cannot make
  those operations resumable.
- Real tunnel and installer validation requires a signed release candidate on
  macOS and Windows. This PR uses unit, integration, and native transaction
  tests and records real-device end-to-end verification as a release residual.
