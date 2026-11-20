# The update panel names whose version it shows

Status: **approved** by the owner on 2026-11-20 (`ui/design:
update/instance-and-app`); implementation follows this plan.

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
desktop-managed host relays its progress in `/api/version`, so every surface
sees it.

## Goal

- No version number appears without saying whose it is.
- The user updates what he is looking at: one click on the instance's row
  updates the instance.
- A result names what was updated; the other subject's own row shows it is
  behind.
- An instance update shows as running until it ends, wherever it was started.
- No new setting, no confirmation dialog, and the existing boundary holds: the
  panel is drawn by the local shell, and a remote page never drives this
  computer's updater.
- Unchanged where one thing is on screen: the desktop app on its own
  computer's instance, a browser, a phone.

## Design

Frames: `ui/design/frames/update/instance-and-app.design.tsx`, drawn with
`ui/design/kit/update-subjects.tsx`.

1. **Bottom row.** While the app shows another computer's instance, the chip
   beside the instance's name shows the instance's version, read by the
   instance's page from its own `/api/version`, as in a browser. It turns gold
   with a dot when either subject can be updated, and shows a spinner while the
   instance updates, with the panel open or closed.
2. **Panel.** One panel with a section per subject: the instance first, under
   its own name, then "This computer's app" (zh: 这台电脑上的桌面应用). A
   section is the shipped panel's body with the subject's name in place of
   "Current version", and its button directly under its state. With two
   sections each button names its target ("Update alex", "Update this app");
   one section alone keeps "Update". An offer says what installing it does to
   that subject: the instance restarts, usually within a minute or two, its
   sessions and settings are kept, and work in progress may be interrupted
   (Vibey continues its own Turn after the restart; a Claude Code or Codex Turn
   ends with a Retry notice). The last check
   and the two switches are behind the subject's name, closed by default.
   The header's check asks both updaters.
3. **Result.** A finished update reads "<name> updated to <version>". After
   the instance updates, the app's own section shows its offer and button if it
   is behind, and the reverse.
4. **Narrow window.** The instance page's own badge shows the instance's
   version and asks the shell for the same panel; the page draws no popover of
   its own inside the desktop app.

New copy: the app's section name, the two named buttons, the instance's offer
sentence, the named result, and the chip's tooltip ("<name> is on
v<version>"). Everything else is shipped `update.*` copy.

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
  enumerated actions beside the four `update_*` ones (check, install with an
  exact target, the two switches, for the selected instance). The popup's
  allow-list (`update_popover::allows`) grows by exactly these. The selection
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

## Delivery

Two PRs. The Runtime's in-progress report is its own backend PR. The main PR
carries the panel, the shell and the bottom row, and reaches a user in two
halves: the shell's two-section panel with the next desktop app build, and the
bottom row once the instance runs a build that contains it.

## Todo

- [x] Owner reviews the frames.
- [ ] Runtime: in-progress report for standalone installs.
- [ ] Shell: instance update actions on the account bridge; popup sizing.
- [ ] UI: `UpdatePanel` sections, bottom row, narrow-window badge, i18n; the
      frames switch to the shipped components and the kit copy is deleted.
