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
2. **Login session (macOS).** At the shared start and restart boundary, a
   caller that is not in the user's current desktop session hands the work to a
   one-shot launchd job in `gui/<uid>`, which carries the caller's environment
   and leaves nothing installed. The SSH probe below confirms or replaces this
   entry mechanism before it is built. With no desktop session the start proceeds as
   today and says that Keychain-backed credentials are unavailable. When the
   session cannot be entered the restart proceeds and warns; it does not refuse,
   because a remote restart must still be possible.
3. **Notice (macOS).** "A desktop session exists and the service is outside it"
   becomes a degraded state on the surfaces that show `stale_login_session`
   today: the Web banner, Doctor, and `vibe status`.
4. **Self-recovery (macOS), separate.** A service that finds itself stale while
   a new desktop session exists restarts itself once through step 2. This waits
   until step 2 has been verified from a session that really ended.

Rejected: only warning or refusing (a remote restart would never work);
`launchctl asuser` (keeps the caller's `PATH`, and is not known to cross
sessions without root); a resident LaunchAgent (a second lifecycle owner, which
is the launch-at-login feature in `macos-stale-session-auto-recovery.md`, not
needed for these failures); hard-coded `PATH` additions (misses nvm, bun, cargo);
starting every service through launchd (changes the working terminal path for
no gain).

## Validation that code cannot give

- A read-only probe run over SSH from another machine decides step 2's entry
  mechanism: it submits a one-shot job into `gui/<uid>` and reports the job's
  audit session and Keychain reach.
- Step 2 from an ended session needs one real log-out and log-in on a Mac.
- A service started by launchd is no longer attributed to the terminal for
  macOS privacy permissions (folders, screen recording); check for new prompts.

## Todo

- [x] Step 1: login-shell `PATH` at Runtime start.
- [ ] SSH probe result recorded here.
- [ ] Steps 2 and 3.
- [ ] Log-out/log-in verification, then step 4.
- [ ] Update the stale-session guidance in the `use-avibe` playbook and the
      Doctor action text once any restart repairs it.
