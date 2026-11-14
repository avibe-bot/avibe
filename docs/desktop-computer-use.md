# Computer use on macOS

Avibe can let agents observe and operate macOS applications through the
desktop app. Computer use is off by default and is available only while the
Avibe desktop shell is running.

## Enable computer use

1. Install Avibe in `/Applications` or `~/Applications` and open it.
2. Open the Avibe application menu or tray menu.
3. Turn on **Computer Use**.
4. Follow the macOS prompts for **Accessibility** and **Screen & System Audio
   Recording**. Older macOS versions call the second permission **Screen
   Recording**.
5. Return to Avibe after granting access. Avibe silently checks the grants
   while it is active and every five seconds. If Screen & System Audio
   Recording is missing, Avibe opens that pane and tells you to click **+** or
   drag Avibe into the list, then enable it. Return to Avibe after both
   permissions are enabled; it observes the grant transition and starts
   computer use when the grants are available.
6. If macOS requires **Quit & Reopen**, or the same fixed app still reports a
   missing permission, quit and reopen that exact installed app without
   replacing or updating it, then turn **Computer Use** on.

### Settings guide

In the desktop app, **Settings → System → Computer Use** walks through the same
setup as three numbered steps: turn on Computer Use, allow Accessibility, and
allow Screen & System Audio Recording. Each step is marked done, current, or
not yet done from the live Computer Use status, and the current step carries
its action:

- **Turn on** turns Computer Use on, exactly like the tray or application menu
  switch. It never turns Computer Use off.
- Accessibility's **Open Settings** asks macOS for the Accessibility grant
  (the system prompt lists Avibe in the pane) and opens **Privacy & Security →
  Accessibility**.
- Screen & System Audio Recording's **Open Settings** runs the same request the
  tray switch does, which lists Avibe in the pane where macOS allows it, opens
  **Privacy & Security → Screen & System Audio Recording**, and shows the
  **+** / drag guidance.

While the page is visible it reads the status about every 1.5 seconds, so a
step turns done within seconds of macOS reporting the grant; it stops reading
while the page is hidden and reads again when it returns. **Recheck** remains
as a manual fallback. Accessibility comes first because the managed driver
checks it before Screen Recording.

If macOS asks to **Quit & Reopen** after the Screen Recording grant, choose it.
When the request came from this page, the app quits without asking whether to
stop the Runtime: the Runtime keeps running, so Agent turns in flight are not
interrupted, and the relaunched app reconnects to it. It then opens the
Settings guide again by itself (within 15 minutes of the request), and the page
picks up the new status. Outside that window, Quit asks as usual, and a later
launch is not redirected.

The Settings entry appears only in the Avibe desktop app. A browser tab or
phone that opens `/settings/computer-use` directly sees the same live steps
with a notice to complete them in the Avibe desktop app on the Mac; the
actions cannot run from a browser. When the host has no live desktop shell, it
sees a short notice that the page is available in the desktop app.

Avibe requests permissions only when you turn the feature on or press a
step's **Open Settings**. It does not keep showing permission prompts in the
background. If the menu still reports that a permission is needed, open
**System Settings → Privacy & Security**, confirm that Avibe is enabled in both
permission panes, then use the off/on or quit/reopen recovery above. Merely
bringing the Avibe window to the front does not start a permission retry.

The native Computer Use control remains available if its optional state or
startup initialization cannot be opened. Avibe reports the failure in the
control and continues starting the rest of the desktop app; it does not make
the optional feature failure prevent Avibe from launching.

### Automatic recovery after a driver failure

If Computer Use was enabled but its driver previously timed out, exited, or
could not start or connect, reopening Avibe makes one automatic recovery
attempt after the service and existing permissions are available. It does
not ask for permissions again or turn on a feature you turned off.

After a failed recovery attempt, Avibe waits at least 30 seconds before the
second attempt and two minutes before the third. These later attempts run
only while the main Avibe window is visible and not minimized. Returning to
the app or opening Settings lets recovery continue; visibility is checked
at least every five seconds. An eligible failure reached while the app is
already running also receives delayed recovery using the same budget.

The status shows **Restarting the Computer Use driver…** while recovery is
pending or running, and returns to **Computer Use is ready** on success.
There are at most three automatic recovery starts per app launch, including
the initial attempt. Success does not refill that budget. If recovery fails
repeatedly, the status shows the final error and the next step: turn Computer
Use off and on, reopen Avibe, or follow the specific repair guidance.

Missing permissions, damaged assets, identity failures, and unclassified
health-check failures keep their existing guidance instead of automatically
restarting the driver. Permission setup and macOS **Quit & Reopen** continue
to work as described above.

Release builds are Developer ID signed with a fixed code identity, so macOS
keeps both grants across updates. An ad-hoc build (a manual build without
signing credentials, or a release from before signing) has a different identity
each time, so macOS may no longer apply the old grants; the first signed build
after one also needs them once more. Turn on the updated installed app in the
same two Privacy & Security panes. On some macOS 26 ad-hoc builds, ScreenCaptureKit can be
denied while macOS refuses to create the Screen Recording row. Avibe opens the
pane and guides you to click **+** or drag the exact Avibe app into the list,
then enable it. This manual addition is the accepted Phase 1 workaround; it
does not prove that automatic registration succeeded. Avibe remains in **Needs
Permission** and does not keep retrying in the background until both grants
are available. If the exact fixed app still reports a missing permission after
the grant, use the Quit & Reopen recovery above without rebuilding it.

## Stop computer use

Turn off **Computer Use** in the native Avibe application or tray menu. This
stops the desktop driver and ends computer-use access for every current agent
session. Closing the Workbench window does not stop it because the tray keeps
the desktop shell running.

The Workbench shows one read-only Computer Use status line. It refreshes without
browser caching when the page opens, every five seconds, and when the window
becomes visible or focused. It reflects the native state and gives localized
guidance for a missing permission, a driver failure, damaged state, or an
unavailable desktop shell. Until Computer Use is ready, the desktop app adds a
**Finish in Settings** link to the Settings guide above. The line does not
expose internal reason codes, turn the feature on, or issue permission changes. The line appears only in the
desktop-managed macOS Runtime; a terminal-installed Avibe Runtime does not show
a native desktop control it cannot own.

## Windows (Preview)

On Windows x64, once the desktop app reports Computer Use as supported, the
same Settings page is labelled **Preview** and shows one step: **Turn on**.
Windows has no Accessibility or Screen Recording grants, so the macOS
permission steps, **Open Settings** actions, and Quit & Reopen guidance never
appear there. The page also states that:

- Agents operate ordinary apps running under the user's Windows account.
- App windows running as administrator are refused, and the Agent says so;
  Avibe never raises its own rights. This differs from Avibe itself running
  as administrator, which keeps Computer Use unavailable until Avibe is
  reopened normally.
- Turning Computer Use off, quitting Avibe, or Avibe closing unexpectedly
  stops the driver and ends any action in progress.

The page recognizes Windows only inside the desktop app, whose window runs on
the host. A browser elsewhere keeps the macOS guide. A host that reports
Computer Use unsupported, including every older Windows build, keeps the
unsupported notice.

## Agent CLI and Skill

Agents use Computer Use through the Controller-owned CLI and the built-in
on-demand `computer-use` Skill:

```text
vibe computer observe apps
vibe computer observe windows
vibe computer observe window --arg pid=123 --arg window_id=456 --output /tmp/window.png
vibe computer click --arg pid=123 --arg window_id=456 --arg x=120 --arg y=80
vibe computer activate --arg pid=123 --arg window_id=456
```

The CLI runs only from an active Avibe Agent Turn. It takes the caller Session
from the Agent environment, and the Controller checks that Session against its
own live active-Turn registry. Turn IDs are not exported or accepted as
capabilities. An Agent cannot choose a different Session or pass a driver
`session` value. Requests use the existing Controller and `ComputerUseServer`
checks: observe-first, exact window targets, target-scoped mutation locks, and
native state/admission checks. Driver state is keyed by Avibe Session and
survives Turn boundaries until the Session is ended or archived.

Input uses background delivery by default and leaves the frontmost window and
pointer alone. When an application ignores background input, the agent can
repeat that action with `delivery_mode: "foreground"`: the driver briefly
fronts the exact window, acts, and restores the previous frontmost window.
`vibe computer activate` (`activate()` in the SDK) brings one exact window to
the front and leaves it there. Neither asks for confirmation per action.

The Skill treats readiness as observable state: the target window exists, it
is stable across observations, and its screenshot or accessibility tree is
usable. It does not wait a fixed time after launching an application.
Irreversible external effects, such as sending, paying, or deleting, still
require the agent to verify, ask for confirmation, verify again, and only then
act.

`observe window` and `zoom` can write screenshots to a caller-selected path.
Human output includes accessibility state and element tokens when available;
`--json` returns the structured service response. The CLI reports `off`,
`needs_permission`, `needs_runtime`, `starting`, `error`, `unavailable`,
`target_busy`, and safety rejections instead of retrying automatically.

## Remote and unattended use

Any Agent Turn can use Computer Use while it is on, including Turns started
from an IM channel, a scheduled task, or another Agent. Who may start a Turn is
decided by Avibe's general message-ingress rules, which are not a Computer Use
control. There is no approval prompt for each click, keystroke, or window
activation. Keep computer use off when you do not want remote or unattended
sessions to access desktop apps.

### Same-user local trust boundary

Computer Use is a cooperative control for Avibe's local Agent processes. The
Controller IPC endpoint and driver socket are reachable by other processes
running as the same macOS user. A same-user local process that discovers those
paths can call the driver directly and bypass the Python-layer
`observe_first`, window-target, and target-lock checks. Those checks govern
Avibe-owned callers; they cannot constrain an arbitrary local same-user
process. Keep the macOS account and local process environment trusted when
Computer Use is enabled.

## macOS limits

- The Mac must be logged in and unlocked for input. A locked Mac cannot be
  driven.
- A window screenshot may still contain an application's contents while the
  Mac is locked. Do not treat locking the screen as a way to hide app contents
  from an already authorized computer-use session.
- Background input asks the driver to leave focus and the physical pointer in
  place. Foreground input and window activation change the frontmost window,
  and a foreground pixel action moves the pointer. Some applications can also
  activate themselves in response to their own controls or operating-system
  behavior.
- Computer use does not operate elevated or otherwise inaccessible targets.
  The agent should report the limitation instead of repeatedly retrying.
