# The update panel names whose version it shows

Status: **approved** by the owner on 2026-11-20 and built (`ui/design:
update/instance-and-app`, drawn by the shipped components). Not yet run on a
real desktop app: see the last Todo item.

## Background

The desktop app connected to an instance on another computer has two things
that update separately: that instance, and the desktop app on this computer.
The owner clicked the version in the sidebar's bottom row, beside the
instance's name, updated, and found afterwards that the desktop app had been
updated and the instance had not.

What the shipped interface does (3.2.8rc4):

| Where a version is shown | Whose it is | What it opens | What its Update updates |
| --- | --- | --- | --- |
| Sidebar bottom row, right of the instance's name; the only version at desktop width (`DesktopAccountRowView`) | This computer's desktop app. Only the accessible name says so | The update panel in the shell's own popup (`update_popover.rs`, `DesktopUpdatePanel`), titled "Version & Update" | This computer's desktop app |
| Native menu, Check for Updates | none shown | The same popup | This computer's desktop app |
| Settings header below `md`, the instance page's own `VersionBadge` | The app's when both versions match; the instance's, with a warning icon, when they differ | The shell's popup, and the page's own popover with the instance's snapshot | The page's popover offers no install; the popup updates the app |

So inside the desktop app the instance's version is not shown at desktop
width, and nothing there can update the instance.

A standalone Runtime's install is tracked only in the page that started it
(`VersionBadge` state around `POST /api/upgrade`): another page or device shows
the update as available, with its button enabled, while the install runs. A
desktop-managed host relays an install a browser asked for (`remote_request`),
so every surface sees that one; an install started at the host itself (someone
there clicks Update, or its automatic update runs) is not relayed: `/api/version`
carries only the desktop app's last check (`vibe/desktop_update_status.py`).

## Goal

- No version number appears without saying whose it is.
- The user updates what he is looking at: one click on the instance's row
  updates the instance.
- A result names what was updated; the other subject's own row shows it is
  behind.
- An instance update shows as running until it ends, wherever it was started.
  Known limit: an install started at a desktop-managed host itself is not
  reported by that host today (see Background); relaying it is a later shell
  change, outside these three PRs. Pressing Update from another surface
  during such an install does no harm: the host's updater is busy, refuses the
  request (`update_in_progress`), and the panel says another update is in
  progress.
- No new setting, no confirmation dialog, and the existing boundary holds: the
  panel is drawn by the local shell, and a remote page never drives this
  computer's updater.
- Unchanged where one thing is on screen: the desktop app on its own
  computer's instance, a browser, a phone.

## Design

Frames: `ui/design/frames/update/instance-and-app.design.tsx`, drawn by the
shipped components (`UpdateSubjectsPanel`, `InstanceVersionChipView`,
`DesktopUpdatePanels`).

1. **Bottom row.** While the app shows another computer's instance, the chip
   beside the instance's name shows the instance's version, read by the
   instance's page from its own `/api/version`, as in a browser. It turns gold
   with a dot when either subject can be updated, and shows a spinner while the
   instance updates, with the panel open or closed. The chip keeps the owner
   in its accessible name; the download icon the shipped chip adds for an app
   update is dropped, as on every other surface (gold and a dot say it).
2. **Panel.** One panel with a section per subject: the instance first, under
   its own name, then "This computer's app" (zh: 这台电脑上的桌面应用). A
   section is the shipped panel's body with the subject's name in place of
   "Current version" (drawn as the section's heading: foreground, medium), and
   its button directly under its state. With two
   sections each button names its target ("Update alex", "Update this app");
   one section alone keeps "Update". An offer says what installing it does to
   that subject: the instance restarts, usually within a minute or two, and
   work in progress may be interrupted
   (Vibey continues its own Turn after the restart; a Claude Code or Codex Turn
   ends with a Retry notice); this computer's Avibe restarts and the instance
   is not affected. An account that cannot manage the instance sees its
   section without a button or switches; an instance that cannot be reached
   shows the bottom row's words for a lost connection, and one that no longer
   accepts the app's sign-in says that its update state cannot be read here
   (the app has no reconnect action to point to, so the line names none).
   The last check
   and the two switches are behind the subject's name, closed by default.
   The header's check asks both updaters.
3. **Result.** A finished update reads "<name> updated to <version>". After
   the instance updates, the app's own section shows its offer and button if it
   is behind, and the reverse.
4. **Narrow window.** The instance page's own badge shows the instance's
   version and asks the shell for the same panel; the page draws no popover of
   its own inside the desktop app.

New copy: the app's section name, the two named buttons, each subject's offer
sentence, the named result, and the chip's label ("<name> is on v<version>").
Everything else is shipped `update.*` copy. In the bottom row the offline icon
sits 2 px left of where it ships, so it no longer touches the name.

## Trust boundary

The remote page keeps exactly what it has today: it may ask the shell to
present the panel (`avibe://updates`), nothing more.

The instance's section is filled and acted on by the shell itself, not by the
instance's page:

- The shell's native HTTP client, which already signs in to the selected
  instance, reads the instance's `/api/version` and sends the install to the
  instance's existing endpoint (`POST /api/upgrade` for a standalone Runtime,
  `POST /api/desktop/update/request` with the exact target for a
  desktop-managed host), and the check and switches to `/api/update/*`. These
  are the requests a browser sends, under the same origin, CSRF and
  authorization checks: the instance decides, and an account that cannot manage
  the instance gets the section without a button.
- The bundled popup page reaches this only through the account bridge, with new
  enumerated actions beside the four `update_*` ones (read again, check,
  install, the two switches, for the selected instance). The popup's
  allow-list (`update_popover::allows`) grows by exactly these five. The selection
  generation fences them as it fences the others.
- What comes back from the instance is display data: versions validated as
  versions and drawn as text, and an enumerated phase.

Because the shell speaks the instance's released HTTP contract rather than its
page, a new desktop app can update an old instance.

Implementation gate: confirm the native client's session passes the instance's
origin and CSRF checks for these POSTs without weakening them.

## Also needed

- A standalone Runtime reports an install or restart in progress in
  `/api/version` (additive), so every surface shows it and disables Update. The
  browser and phone panels keep their look.
- The bottom row is drawn by the instance's page, so it shows the instance's
  version once the instance runs a build with this change. The panel is drawn
  by the shell, so it has both sections as soon as the desktop app is updated.
- The popup window's height follows the panel (it is fixed at 520 px today).
- The version-skew warning (`dashboard.runtimeVersionSkew`) stays for an
  instance on the same computer; two computers on different versions is normal.

## Decided

- The last check and the switches are closed by default in the two-section
  panel (both open is about 650 px tall).
- After the desktop app updates and restarts, the panel does not reopen by
  itself; the chip then shows the instance's version, with its dot if the
  instance is behind.

## Contracts

### A. The Runtime reports an install in progress (`/api/version`)

A standalone Runtime's `get_version_info()` gains one additive field. A
desktop-managed host does not carry it; its `remote_request` already says this.

```json
"install": null | {
  "phase": "installing" | "restarting",
  "target": "3.2.8rc4" | null,
  "started_at": "2026-11-20T07:03:17Z"
}
```

- `installing` while an installer owns the install lock
  (`atomic_upgrade_lock`): `POST /api/upgrade`, `vibe upgrade`, and the
  automatic update alike. Only an installer marks the lock; its other holders
  (a plain restart, pruning, launcher repair, activation) report nothing.
  `restarting` while the restart that a committed install scheduled is pending;
  any other restart reports nothing. `null` otherwise, including after a failed
  install.
- `target` and `started_at` come from the installer's own record; a record
  whose holder is gone reports nothing.
- Reading it never waits for the lock and adds no blocking work to the request.
- Older Workbenches ignore the field. Nothing else in the answer changes, and
  `POST /api/upgrade` keeps its released behaviour.

### B1. The shell reads and updates the selected instance

State. The full client state the bundled account pages read
(`DesktopClientState`) gains `instance_update`; the projection a Workbench
page receives (`runtime_projection`) does not.

```ts
instance_update: null | {            // null while the committed connection is local
  instance_id: string;               // the committed remote instance
  status: 'loading' | 'ready' | 'unreachable' | 'unauthorized';
  snapshot: UpdateSnapshot | null;   // the instance's /api/version, allow-listed fields only
  can_manage: boolean;               // /api/session capabilities.can_manage_instance
  request: null | {                  // the last action sent from this shell
    action: 'install' | 'check' | 'prereleases' | 'auto_update';
    phase: 'sending' | 'accepted' | 'failed';
    target: string | null;           // install only
    error_code: string | null;       // a code, never upstream text
    updated_at: string;
  };
  fetched_at: string | null;
}
```

`snapshot` keeps the `UpdateSnapshot` fields
(`ui/src/components/update/types.ts`) plus contract A's `install`, each reduced
to display data: versions that parse as versions, enumerated phases, booleans,
timestamps, and bounded `host_actions`. The free-text `error` becomes the fixed
string `"error"` when set and `null` otherwise; `build` keeps only `kind`.
Anything else is dropped; an answer that fails validation is `unreachable`.
`unauthorized` is an instance that answers but no longer accepts the app's
sign-in; the panel says so in its own line, and no update action starts a
sign-in.

Actions, in the account bridge's v1 envelope, beside the four `update_*` ones:

| Action | Request to the instance |
| --- | --- |
| `instance_update_refresh` | `GET /api/version`, `GET /api/session` |
| `instance_update_check` | `POST /api/update/check` |
| `instance_update_set_prereleases {enabled}` | `POST /api/update/prereleases` |
| `instance_update_set_auto_update {enabled}` | `POST /api/update/auto-update` |
| `instance_update_install {target_version}` | standalone: `POST /api/upgrade` with `confirm_interrupt: true`; desktop-managed: `POST /api/desktop/update/request` with the exact target and `confirm_interrupt: true` |

- Callers: the bundled account page in the main window, and the update popup
  on its own page (`update_popover::allows` grows by exactly these). A
  Workbench document, local or remote, can call none of them, and still none
  of the four `update_*` ones on a remote page.
- Requests go through the shell's native HTTP client with the sign-in the app
  made for that instance: its session cookie, `Origin` set to the instance's
  origin, and the `vibe_csrf_token` cookie echoed in `X-Vibe-CSRF-Token`. The
  instance's origin, CSRF and authorization checks are not changed.
- Every action is fenced by the selection generation: a reply that arrives
  after the selection changed is dropped, and no action is sent for an
  instance that is no longer the committed one.
- `install` requires `target_version` to equal the snapshot's `latest` at the
  time of the click. A desktop-managed host installs exactly that target. A
  standalone Runtime's `POST /api/upgrade` takes no target and installs what it
  finds latest when it runs, so the result names the version the snapshot
  reports as `current` after the restart, not the one that was clicked. A
  standalone install keeps its HTTP request open for the
  whole install (about a minute); the shell does not treat a long wait as a
  failure, and follows `install` and `current` in the snapshot to its end.
- While the popup is open, or a request is not settled, the shell rereads the
  snapshot every few seconds; otherwise it reads it when the popup opens.
- The popup window is as tall as its panel needs, within the main window.

### B2. The interface

- `UpdatePanel` draws a section per subject; the popup draws two when
  `instance_update` is not null, and today's single panel otherwise.
- The bottom row and the narrow-window badge are the instance's page: they
  read the page's own `/api/version`, and still only ask the shell to present
  the panel. The page reads it when it loads, when its window comes back to
  the front or becomes visible, every five seconds from the moment it asks the
  shell for the panel until its window is in front again (the panel is another
  window, and takes the focus), and every three seconds while an install or its
  restart is reported. A standalone Runtime looks its latest release up on
  every version read, which is why these are no faster; caching that lookup in
  the Runtime is a possible later change. When the Runtime then answers with a version other than
  the one the install started from, the page loads itself again, since its
  interface changed with the version; an install that ends on the same version
  changes nothing on the page.
- A subject whose check failed or never ran keeps the single panel's button
  for it (retry, or "Check for updates"), in its own section; it runs the
  panel's one check. A source build keeps its package version beside its name.
- A standalone Runtime's page reads `install` from `/api/version` at the same
  moments, and when its own panel opens: an install started elsewhere shows as
  running, and Update is disabled until it ends.

## Delivery

Three PRs, each mergeable alone:

1. **Runtime** (contract A): useful by itself, any surface.
2. **Shell** (contract B1): the bridge actions and the state; no visible change
   until the interface draws them.
3. **Interface** (contract B2): the two-section panel, the bottom row, the
   narrow-window badge and the copy. The frames switch to the shipped
   components and the kit copy is deleted.

It reaches a user in two halves: the two-section panel with the next desktop
app build (2 and 3 are in the app's bundle), and the bottom row once the
instance runs a build that contains 3.

## Todo

- [x] Owner reviews the frames.
- [x] Runtime: in-progress report for standalone installs (contract A).
- [x] Shell: instance update actions on the account bridge; popup sizing (B1).
- [x] Interface: `UpdatePanel` sections, bottom row, narrow-window badge, i18n;
      the frames switch to the shipped components and the kit copy is deleted (B2).
- [ ] One run on a real desktop app connected to a remote instance.
