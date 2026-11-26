# Desktop instances: inline signed-in account

## Contract (2026-11-27)

Base: `origin/master` `e0d3854921a467321717f47fc4b2153c284f95d5`, explicitly fetched before creating the task worktree.

The signed-in Instances page replaces its email navigation row with a static
account card: Currently signed in, the email, and a full-width secondary Sign out
on this device button. The card uses the existing Desktop surface, edge, rounded-xl,
16px padding and default outline Button (rounded-lg); label/email spacing is 8px,
and the button is 20px below the email. The iOS account card supplies the content
hierarchy; Desktop tokens supply its dimensions. No new translation keys are needed:
`desktopAccount.currentAccount` and `desktopClient.signOut` already ship in the
Desktop language bundle.

## Existing behavior that changes

- Before: click the email row and chevron to open Account, then click Sign out.
  After: the email is plain text without a chevron; Sign out is in this card.
- The same `setView('sign-out')` and `leave('sign_out')` handler serves both pages.
  Native cleanup, failure/retry, Back to Workbench and confirmation behavior do
  not change; no confirmation dialog is introduced.
- The Account page is retained. It remains a native `view=account` entry and the
  setup-completion sign-in destination; an already signed-in Account document
  also shows a read-only binding line when bound. Its Back action still opens
  Instances. Removing that page would widen this change into native presentation
  and login completion, so only the Instances-to-Account link is removed.

Instance rows, selection, add, refresh, the sidebar menu and iOS are unchanged.
Pending instance actions still disable the card's action as they disabled the old
navigation row.

## Evidence and limits

Shipped frames: `desktop/instances/instances-*` (local) and
`desktop/instances/current-shared-*` (remote). They render InstanceSwitcherView.
Unit tests cover identity/fallback text, the non-interactive email, disabled state,
and the same native sign-out request. Existing bridge and binding tests keep the
native Account entry and its return path covered. Production Desktop frontend
browser checks use a hermetic shell bridge with local/remote states, English and
Chinese, both themes, success and existing failure/retry behavior. Before/after
captures use identical 880x820 viewports. Installed native credential cleanup and
window transitions are not exercised; no package or installation is produced.
