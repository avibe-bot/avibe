# macOS stale-session automatic recovery proposal

## Decision requested

Part 1 detects an Avibe process that survived an ended macOS login session and
asks the user to run `vibe restart` from the current desktop. This proposal
covers automatic replacement after the next login only; it is design material,
not an implementation in this PR.

## Recommendation

Install one per-user macOS `LaunchAgent` in the user's `gui/<uid>` domain,
owned by the selected Avibe installation. It should use `RunAtLoad` and start a
new service only after the user has logged into the desktop. The service's
existing `service.lock` and orphan handling remain the single takeover
mechanism: a new instance must first observe the old owner, wait for its
bounded retirement policy, and never kill an unrelated process merely because
it has the same executable name.

The LaunchAgent should be an opt-in, explicit lifecycle integration rather
than an unconditional installer side effect. The Tauri shell's launch-at-login
setting should enable or disable the same owner-controlled registration and
should adopt an existing daemon only after a fresh-session health check. If the
health check reports `stale_login_session`, the shell must not adopt it: it
should leave the stale owner untouched, start the new instance through the
normal lifecycle path, and let `service.lock`/orphan handling settle the
handoff.

## Lifecycle semantics

- `vibe start`: ensure the per-user launcher is installed when the user has
  opted into launch-at-login, then start the service in the current session.
- `vibe stop`: stop the current instance but retain the launcher preference;
  `RunAtLoad` must not immediately defeat an intentional stop. This likely
  needs a short-lived disabled marker or launchctl kickstart suppression that
  the next explicit `start` clears.
- `vibe restart`: stop and start the current generation in the same session;
  it is still the explicit repair for an already-stale instance.
- `vibe upgrade`: install the new generation, preserve the launch preference,
  and replace/reload the LaunchAgent atomically. Do not leave a plist pointing
  at a removed uv-tool path or an old desktop bundle.
- Uninstall must remove only Avibe's labeled LaunchAgent and its marker. It
  must stop the owned service first, preserve user data, and never remove a
  user-created unrelated launch item.

## Installation variants and shell adoption

For uv-tool installs, the launcher should point at a stable Avibe launcher or
an absolute interpreter/tool path that `vibe upgrade` can repair. It must not
capture a transient virtualenv path. For desktop-bundled installs, the
launcher should point at the signed bundle's stable helper/CLI entry point;
Tauri and CLI must agree on the label, owner metadata, and service lock.

The desktop shell currently has launch-at-login and adopts an existing daemon.
Adoption needs a freshness gate: compare the service's session identity with the
current GUI session and reject a `stale_login_session` result. Otherwise the
shell can reconnect to a dead-session daemon and recreate the incident. The
shell should also distinguish "launcher started this generation" from "found a
healthy pre-existing service" for safe stop/quit behavior.

## Headless and SSH-only Macs

A LaunchAgent in `gui/<uid>` is intentionally a desktop-session feature. A
headless Mac or SSH-only user should not receive a fake GUI launcher or a
Keychain dialog expectation. Keep explicit `vibe start`/`stop`/`restart`
semantics available over SSH, report `no_gui_session` when a GUI-only action is
impossible, and document a separate system/service-manager integration if
headless boot is needed. Automatic login-session replacement should remain
disabled unless the user selects a headless-compatible mode and its credential
backend is usable without Aqua.

## Linux parity

Do not force macOS LaunchAgent concepts into Linux. The Linux parity contract
should be an optional per-user `systemd --user` unit with `WantedBy=default.target`
and the same freshness, lock, stop-marker, upgrade, and uninstall semantics.
Where systemd is unavailable, retain manual lifecycle commands. The shared
contract is "start once per current user session and never adopt a stale
instance," not a shared unit-file format.

## Rollback and trade-offs

The recommended launcher gives the best recovery for the confirmed logout /
login failure and keeps Avibe local-first, but it adds another lifecycle owner,
installer migration work, and platform-specific stop semantics. A simpler
alternative is to have only the Tauri shell repair stale daemons on startup;
that avoids a new launcher but does not recover uv-tool/headless CLI users and
fails when the shell is not installed or disabled. A fully automatic stale
process kill is rejected: it risks interrupting work and crosses the existing
service ownership safety boundary.

Rollback should be a versioned launcher migration: disable the new launcher,
remove only its label and marker, and fall back to the existing manual/Tauri
startup path. Keep the service lock and state files intact so rollback does not
wipe credentials, pairing, remote access, or workspace data. Before shipping,
owner decisions are needed on launch-at-login default/opt-in, intentional-stop
marker duration, Tauri adoption UX, and whether headless Linux/macOS
registration is in scope.
