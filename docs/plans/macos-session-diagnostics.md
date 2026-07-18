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

## Residual Manual Matrix

- Lock/unlock and VNC disconnect/reconnect: expect no transition unless console,
  GUI-domain, ASID-match, or DNS state actually changes.
- Fast User Switching with both users logged in: expect a console-user transition
  while the Avibe GUI domain stays available and ASIDs stay matched.
- Explicit or automatic logout/login: observe GUI-domain disappearance and
  recreation, ASID mismatch/match changes, and whether the unmanaged service PID
  survives.
- Sleep/wake, VPN, and network reconfiguration: observe DNS failure/recovery
  without logging DNS servers or addresses.

These diagnostics collect evidence for the next occurrence; they do not prove
the stale-Aqua-domain hypothesis or change service lifecycle behavior.

## Todo

- [x] Add injected parsers/probes and a transition-deduplicating monitor.
- [x] Attach the monitor to service startup and shutdown on macOS only.
- [x] Add focused parsing, transition, failure, and non-macOS tests.
- [x] Run focused pytest and Ruff before delivery.
