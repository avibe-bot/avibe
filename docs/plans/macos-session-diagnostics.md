# macOS Session Diagnostics

## Background

Issue #891 cannot be explained by `PPID=1`: live Fast User Switching preserves
the Avibe account's Aqua domain, and the service and backend child keep the same
audit session ID while DNS remains healthy. The next failure must be recorded
before a restart without adding lifecycle changes or DNS workarounds.

## Goal

Add a macOS-only state-transition recorder owned by the existing service
lifecycle. It must distinguish an ordinary console-user switch from a missing,
recreated, or mismatched Aqua login domain while also recording resolver failure
and recovery.

## Design

- The service entrypoint owns one daemon monitor thread and stops it during the
  existing shutdown path. The monitor never starts, stops, or restarts Avibe.
- A snapshot contains only service PID/UID, console user, service and GUI audit
  session IDs, GUI-domain availability, ASID match, and DNS state/error class.
- Session context is checked every 60 seconds. The public-hostname resolver probe
  is bounded to two seconds, runs at startup, every five minutes, and immediately
  after a session-context transition.
- Logging is edge-triggered: one startup snapshot, then only console-user,
  GUI-domain, ASID-match, DNS-failure, and DNS-recovery transitions. Unchanged
  failures are deduplicated.
- Raw `launchctl`/resolver output, environment data, DNS servers, and resolved
  addresses never enter the log event.
- Missing tools, permissions, command timeouts, absent domains, and parse drift
  are non-fatal diagnostic states.

The obvious periodic-timer logger is intentionally rejected: it adds noise while
providing no extra evidence during a persistent failure.

## Confirmed explicit-logout result (2026-10-27)

The waiting case was reproduced on the Mac mini on **October 27, 2026**. The
owner logged out and logged back in: `loginwindow` restarted at **01:33** and
Dock/Finder appeared in the new desktop at **01:38** (Asia/Shanghai). The Avibe
service and UI server had started at 23:02 in the old session, survived logout
as PPID 1 orphans, and continued to spawn children in that dead session.

The old diagnostic recorded `launchctl print gui/501` as `command_failed` because
calling it from the dead session returned “Reentrancy avoided”; that left the
GUI-domain and ASID fields inconclusive. The stronger process-local signal was
then confirmed: `getpwuid(501)` failed with `KeyError`, while Directory Services
and `security list-keychains` also failed. Avault authorization failed in 36 ms
with `not_completed` and an underlying `errSecParam`. Only `vibe restart` run
from Terminal in the new desktop session repaired the instance; restarting from
inside Avibe would inherit the dead session.

The monitor now treats the service process's own Directory Services
`user_lookup_failed` result as the positive `stale_login_session` signal. A
`launchctl` GUI-domain command failure alone remains inconclusive. The signal
was added to the existing bounded, redacted transition monitor and is kept
separate from ordinary GUI-domain absence.

## Service outside the desktop session (2026-11-10)

On **November 10, 2026** a restart run over non-interactive SSH started the
service in audit session 106269 while the user's desktop session was 105819.
The login Keychain cannot be read from there: `gh` reported an invalid token
and Vault could not use its key, until a restart from a desktop terminal. The
startup snapshot recorded `asid_match: false` and was classified `healthy`.

The classifier now reports `outside_gui_session` when the GUI domain is
available, both audit session IDs are known, and they differ. The order is
`stale_login_session`, `no_gui_session`, `unknown`, `outside_gui_session`,
`healthy`: an ended login is the stronger finding, and a mismatch needs both
IDs. Fast User Switching keeps the two IDs equal (confirmed in the same log:
the console moved between three users while the service stayed matched), so it
stays `healthy`. A GUI ASID that changed under a surviving service means a new
desktop login, and the same restart from the desktop repairs it.

The state is a warning, not a failure: only Keychain-backed credentials are
lost. It reaches the user through the paths `stale_login_session` uses: the
service's health identity, Doctor (`warn`), the Workbench banner, and the Vault
error mapping (`avault-custody-core.md`, 13.1). Entering the desktop session at
restart is separate work: the service-start-context plan introduced by #3507,
whose step 3 this is.

## Residual Manual Matrix

The classifier's explicit no-flag cases are represented in focused tests:
Fast User Switching with both users logged in, lock/unlock, VNC, and sleep/wake
remain `healthy`; a genuinely absent GUI domain is classified as
`no_gui_session`, not stale. A real manual re-run can still validate these
shapes on additional macOS versions.

- Lock/unlock and VNC disconnect/reconnect: any diagnostic transition is
  informational; neither case may be classified as `stale_login_session`.
- Fast User Switching with both users logged in: expect a console-user transition
  while the Avibe GUI domain stays available and ASIDs stay matched.
- Explicit or automatic logout/login: observe GUI-domain disappearance and
  recreation, ASID mismatch/match changes, and whether the unmanaged service PID
  survives.
- Sleep/wake, VPN, and network reconfiguration: observe DNS failure/recovery
  without logging DNS servers or addresses.

These diagnostics record the confirmed stale-session signal and remain
best-effort; they do not start, stop, or restart service lifecycle state.

## Todo

- [x] Add injected parsers/probes and a transition-deduplicating monitor.
- [x] Attach the monitor to service startup and shutdown on macOS only.
- [x] Add focused parsing, transition, failure, and non-macOS tests.
- [x] Confirm the explicit logout/login case and record the positive process-local signal.
- [x] Expose the stale-session reason consistently through Vault, Doctor, CLI, and Web surfaces.
- [x] Classify a service outside the user's desktop session and show it on the same surfaces.
