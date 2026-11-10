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
   `healthy` login session. `POST /internal/restart` says when and what to
   restart and names the install generation the caller's job would run; it
   carries no path. The service seeds the restart job in its own process, off
   the controller loop, with the launcher it restarts itself with, as a restart
   from the Web UI does, and refuses when that launcher does not select the
   named generation: the desktop session reaches the Keychain, so the service
   never runs there what a caller names. The caller prints the job and says
   that the running Avibe performs the restart. It keeps the activation lock
   across the hand-over and the service does not take it, so an upgrade hands
   over too, onto the generation it just activated.
   A caller in the desktop session seeds the job itself, as before: the desktop
   terminal stays the manual repair for a stale service. With no desktop
   session, or when the probe cannot tell, nothing changes. When the caller is
   outside and no service can take the restart (none is running, it is not
   `healthy`, it is a desktop-managed Runtime, it predates the endpoint, its
   launcher selects another generation, the caller's install is not a managed
   generation, or the request fails), the caller seeds the job and warns that
   Avibe will run outside the desktop session, so Keychain-backed credentials
   are unavailable until `vibe restart` is run from a terminal on the Mac's
   desktop. It does not refuse, because a remote restart must still be
   possible. `vibe start` with nothing running stays a local start and gives
   the same warning.
3. **Notice (macOS).** "A desktop session exists and the service is outside it"
   is the degraded state `outside_gui_session` on the surfaces that show
   `stale_login_session`: the Web banner, Doctor, and `vibe status`. A service
   in that state is not `healthy`, so step 2 does not hand it a restart.
4. **Launch at login (macOS), separate.** This replaces self-recovery by the
   service, which step 2 cannot give: a restart seeded by a stale service
   copies the ended session. What starts Avibe at the next desktop login
   replaces such a service instead, as the next paragraph describes; for a CLI
   install that is `vibe autostart` (#3515), an opt-in LaunchAgent that fires
   once per desktop login. It runs in the desktop session, so the restart it
   schedules is seeded there, as step 2 leaves any caller inside to do.

**Replacement at the next desktop login (macOS), built.** A service left in
another login session is replaced by whatever starts Avibe in the new desktop
session. For a CLI install that is the opt-in `vibe autostart` login entry
(`vibe/login_autostart.py`, decided in `macos-stale-session-auto-recovery.md`).
Its desktop counterpart is the app's own launch: opening the app replaces the
app's own Runtime instead of adopting it (`desktop/README.md`, step 3). Both
act on one decision, `service_outside_desktop_session`: the service reports an
ended login session, or its audit session is known to differ from the desktop
session's; anything inconclusive is left alone.

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
- The desktop app's replacement needs a packaged build and one real log-out and
  log-in: open the app, log out and in, and open it again. The Workbench opens
  with no stale-session banner, and the app's `bootstrap.log` (in its local
  data directory, `~/Library/Application Support/bot.avibe.desktop/`) has an
  `endpoint.query` line carrying `replace_runtime="true"` with the reason,
  followed by a `runtime.start` line.

## Todo

- [x] Step 1: login-shell `PATH` at Runtime start.
- [x] SSH probe result recorded here.
- [x] Step 2: a restart from outside the desktop session goes through the
      running service.
- [ ] Step 2 verified by a real restart over SSH.
- [x] Step 3: the `outside_gui_session` state and its notice.
- [x] Step 4, replacement at the next desktop login: `vibe autostart` (#3515)
      for CLI installs, the app's launch for the desktop app.
- [ ] Step 4 verified by a real log-out and log-in.
- [ ] Update the stale-session guidance in the `use-avibe` playbook and the
      Doctor action text once step 4 repairs a stale service at login.
