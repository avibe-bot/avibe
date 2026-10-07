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
   while it is active and every five seconds. If the menu still says a
   permission is needed, turn **Computer Use** off and on once.
6. If macOS requires **Quit & Reopen**, or the same fixed app still reports a
   missing permission, quit and reopen that exact installed app without
   replacing or updating it, then turn **Computer Use** on.

Avibe requests permissions only when you turn the feature on. It does not keep
showing permission prompts in the background. If the menu still reports that a
permission is needed, open **System Settings → Privacy & Security**, confirm
that Avibe is enabled in both permission panes, then use the off/on or
quit/reopen recovery above. Merely bringing the Avibe window to the front does
not start a permission retry.

An ad-hoc app update can change the app's code identity, so macOS may no longer
apply the old grants. Turn on the updated installed app in the same two Privacy
& Security panes. On some macOS 26 ad-hoc builds, ScreenCaptureKit can be
denied while macOS refuses to create the Screen Recording row. In that case
Avibe remains in **Needs Permission** and does not keep retrying in the
background. If an owner or support procedure provides a manual System Settings
add/grant recovery for the exact installed app, follow that procedure without
rebuilding the app; manual addition is a documented workaround for the OS
limitation, not proof that automatic registration succeeded. Avibe remains in
**Needs Permission** until the updated app has both grants.

## Stop computer use

Turn off **Computer Use** in the native Avibe application or tray menu. This
stops the desktop driver and ends computer-use access for every current agent
session. Closing the Workbench window does not stop it because the tray keeps
the desktop shell running.

## Remote and shared-channel use

Computer use follows Avibe's existing user binding rules. For Slack, Discord,
Telegram, Feishu/Lark, WeChat, or another shared channel, enable
`require_bind` so only bound users can ask an agent to operate the Mac.

There is no approval prompt for each click or keystroke. Keep computer use off
when you do not want remote or unattended sessions to access desktop apps.

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
