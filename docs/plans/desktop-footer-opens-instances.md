# Desktop sidebar footer: one click opens Instances

## Change contract (2026-11-28)

Base: `origin/master` `44d55006982c893492a8a8b28ad56db1333bf912`, fetched before creating the task worktree.

Owner, 2026-11-28 22:21: clicking the instance name at the bottom left of Avibe Desktop goes straight
to the Switch instance page, as the iPhone's avatar does; the popover in between is removed, so
switching no longer needs a second click on 「切换实例」.

- A click, Enter or Space on the signed-in footer row presents the native Instances page
  (`avibe://desktop/instances`), through the same navigation guard as before. This holds in every
  connection state, offline and switching included: the page is where the way back to this computer is.
- Nothing opens in between. The button has no chevron, no `aria-haspopup` and no `aria-expanded`.
  The row keeps the status glyph and the instance name; hover and focus styling are unchanged.
- The account (email) and 「在此设备退出」 are reached on the Instances page, in its account card
  (`docs/plans/desktop-instances-account-card.md`). No second sign-out is added.
- Signed out, the row still presents sign-in directly.

This restores the row's behavior from before #4311 and removes that change's popover
(`DesktopSidebarAccountMenu`), its spec, its frame (`chrome/sidebar-account-menu`) and its plan.

## What stays, and why

- The `sign-out` presentation view stays in the bridge and the shell. The sidebar no longer sends
  it, but the shell's own menu does (`desktop-client-sign-out`), and so does the popover of any
  instance still on an older version: Desktop draws each instance's own Workbench.
- `desktopClient.switchInstance` stays: the shell's menu reads it (`desktopAccountCopy.test.ts`).
- The Instances page, the Account page, the native presentation and the iPhone are unchanged.
- Leaving the page without switching is unchanged: Back to Workbench or Esc sends `cancel`, and the
  shell shows the Workbench document it kept.

## Copy

`devices.rename.howDesktop` described a path that no longer exists (choose the account under
「账户」; the card's heading is 「账号」 and its email is not a control). It now names the real one:
click the instance name at the bottom left, then Sign out on this device under Account.

## Evidence and limits

Unit: `DesktopAccountRow.test.tsx` (direct presentation in each connection state, keyboard, no
menu semantics, signed-out entry). Browser: `e2e/desktop-client/client.spec.ts` (production build,
hermetic shell projection: the real guard and URL producer emit `avibe://desktop/instances` after
one click, local and remote) and `instances-account.spec.ts` (the page's account card and its
sign-out request). Frames: `chrome/bottom-row/*` (the row, at rest and under the pointer) and
`desktop/instances/*` (the page). Installed native window transitions are not exercised here.

## Where it ships

The sidebar row is part of the Workbench that the shown instance serves, and no shell code changes.
An instance on a CLI install gets it with an instance release, including when Desktop on another
computer shows it or adopts it on its own computer. An instance whose Runtime a Desktop app embedded
gets it with that computer's Desktop package (`desktop/README.md`).
