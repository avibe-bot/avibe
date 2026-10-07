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

Avibe requests permissions only when you turn the feature on. It does not keep
showing permission prompts in the background. If the menu still reports that a
permission is needed, open **System Settings → Privacy & Security**, confirm
that Avibe is enabled in both permission panes, then use the off/on or
quit/reopen recovery above. Merely bringing the Avibe window to the front does
not start a permission retry.

The native Computer Use control remains available if its optional state or
startup initialization cannot be opened. Avibe reports the failure in the
control and continues starting the rest of the desktop app; it does not make
the optional feature failure prevent Avibe from launching.

An ad-hoc app update can change the app's code identity, so macOS may no longer
apply the old grants. Turn on the updated installed app in the same two Privacy
& Security panes. On some macOS 26 ad-hoc builds, ScreenCaptureKit can be
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
guidance for a missing Accessibility or Screen Recording permission; it does
not turn the feature on or issue permission changes.

## Remote and shared-channel use

Computer use follows Avibe's existing user binding rules. For Slack, Discord,
Telegram, Feishu/Lark, WeChat, or another shared channel, enable
`require_bind` so only bound users can ask an agent to operate the Mac.

There is no approval prompt for each click or keystroke. Keep computer use off
when you do not want remote or unattended sessions to access desktop apps.

### Same-user local trust boundary

Computer Use is a cooperative control for Avibe's local agent processes. The
managed MCP server and driver socket are reachable by other processes running
as the same macOS user. A same-user local process that discovers those paths
can call the driver directly and bypass the Python-layer `require_bind`,
`observe_first`, focus-shortcut, and window-target checks. `require_bind`
protects shared-channel callers that go through Avibe; it cannot constrain an
arbitrary local same-user process. Keep the macOS account and local process
environment trusted when Computer Use is enabled.

## macOS limits

- The Mac must be logged in and unlocked for input. A locked Mac cannot be
  driven.
- A window screenshot may still contain an application's contents while the
  Mac is locked. Do not treat locking the screen as a way to hide app contents
  from an already authorized computer-use session.
- Avibe asks the driver to avoid taking focus and to leave the physical pointer
  in place. Some applications can still activate themselves in response to
  their own controls or operating-system behavior.
- Computer use does not operate elevated or otherwise inaccessible targets.
  The agent should report the limitation instead of repeatedly retrying.
