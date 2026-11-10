# The context the service starts in

## Background

The service has no context of its own. It starts with the environment
variables and, on macOS, the login session of whatever started it, and every
Agent Session inherits both from there.

- **2026-11-10 15:14.** A restart run over non-interactive SSH produced a
  service with `PATH=/usr/bin:/bin:/usr/sbin:/sbin` in an audit session that was
  not the desktop's. Sessions lost `vibe`, `gh` and `uv`; `gh` reported an
  invalid token because the Keychain cannot be read from that session. The
  token was never invalid: a restart from a desktop terminal restored it
  without a new login. The startup diagnostic recorded `asid_match: false` and
  classified the service as healthy.
- **2026-11-09 16:05** (#3429). The service started in the right session, the
  GUI login ended when WindowServer crashed, and every restart issued from
  inside Avibe copied the ended session into the successor.

Both are one cause: the starter's context becomes the service's, whether it was
wrong at the start or went stale later.

## Goal

A service that is started or restarted by any caller has the user's `PATH` and,
on macOS when the user has a desktop session, runs inside that session. When
that is not possible, the user is told. A start from a desktop terminal is
unchanged.

## Solution

1. **`PATH` (macOS and Linux).** A Runtime start asks the login shell for its
   `PATH` (`vibe/login_path.py`, the Python counterpart of the desktop's
   `login_path.rs`). A caller that already has every login-shell entry keeps its
   `PATH` unchanged; otherwise the login shell's `PATH` replaces the entries the
   two share, the caller's own leading entries stay first (a service unit's
   virtualenv), and its other own entries follow. A desktop-managed Runtime is left alone. Every failure keeps
   the caller's `PATH`.
2. **Login session (macOS).** The running service is already in the desktop
   session, with the right environment and privacy permissions, so a restart it
   seeds itself yields a successor in the same context, as restarts from the
   Web UI and from Agent shells already do. At the shared restart boundary
   (`schedule_restart`), a caller whose audit session is not `gui/<uid>`'s
   hands the restart to the running service when its health identity reports a
   `healthy` login session: `POST /internal/restart` carries
   `schedule_restart`'s arguments, the service seeds the restart job in its own
   process, off the controller loop, and the caller prints that job and says
   that the running Avibe performs the restart. The caller keeps the activation
   lock across the hand-over and the service does not take it, so an upgrade
   hands over too.
   A caller in the desktop session seeds the job itself, as before: the desktop
   terminal stays the manual repair for a stale service. With no desktop
   session, or when the probe cannot tell, nothing changes. When the caller is
   outside and no service can take the restart (none is running, it is not
   `healthy`, it is a desktop-managed Runtime, it predates the endpoint, or the
   request fails), the caller seeds the job and warns that Avibe will run
   outside the desktop session, so Keychain-backed credentials are unavailable
   until `vibe restart` is run from a terminal on the Mac's desktop. It does not
   refuse, because a remote restart must still be possible. `vibe start` with
   nothing running stays a local start and gives the same warning.
3. **Notice (macOS).** "A desktop session exists and the service is outside it"
   is the degraded state `outside_gui_session` on the surfaces that show
   `stale_login_session`: the Web banner, Doctor, and `vibe status`. A service
   in that state is not `healthy`, so step 2 does not hand it a restart.
4. **Launch at login (macOS), separate.** This replaces self-recovery by the
   service, which step 2 cannot give: a restart seeded by a stale service
   copies the ended session. Launch at login is being built as `vibe autostart`,
   an opt-in LaunchAgent that fires once per desktop login and starts Avibe, or
   restarts a service that is stale or in another audit session. See
   `macos-stale-session-auto-recovery.md`.

Rejected: only warning or refusing (a remote restart would never work);
`launchctl asuser` (keeps the caller's `PATH`, and cannot cross sessions from
SSH, see the probe below); a one-shot launchd job in `gui/<uid>` (it enters the
desktop session, but loses the privacy permissions the terminal was granted,
see the probe below); a kept-alive LaunchAgent (a second lifecycle owner that
runs between logins; the login-only agent of step 4 is not one); hard-coded
`PATH` additions (misses nvm, bun, cargo);
starting every service through launchd (the same loss of permissions, and it
changes the working terminal path for no gain).

## SSH probe (2026-11-11)

A read-only probe run over SSH from another machine settled step 2's entry
mechanism:

- `launchctl asuser <uid>` from an SSH session fails: "Could not switch to
  audit session: Operation not permitted".
- A one-shot launchd job in `gui/<uid>` does land in the desktop session, with
  the Keychain reachable. But a process launchd starts is its own responsible
  process for macOS privacy permissions: it loses what the terminal, or sshd,
  was granted, such as Full Disk Access, and with it workspaces on an external
  volume. So step 2 does not use launchd.
- The running service is in the desktop session with the right environment and
  permissions, which is what step 2 builds on.

## Validation that code cannot give

- Step 2 needs one real `vibe restart` over SSH on a Mac whose service is in
  the desktop session: the command says that the running Avibe performs the
  restart, the startup diagnostic of the new service records `asid_match: true`,
  and `gh` reads its token.
- Step 4 needs one real log-out and log-in on a Mac.

## Todo

- [x] Step 1: login-shell `PATH` at Runtime start.
- [x] SSH probe result recorded here.
- [x] Step 2: a restart from outside the desktop session goes through the
      running service.
- [ ] Step 2 verified by a real restart over SSH.
- [x] Step 3: the `outside_gui_session` state and its notice.
- [ ] Step 4: launch at login (`vibe autostart`), then the log-out/log-in
      verification.
- [ ] Update the stale-session guidance in the `use-avibe` playbook and the
      Doctor action text once step 4 repairs a stale service at login.
