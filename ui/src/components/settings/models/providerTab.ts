/**
 * One owner for the tab the provider's authorization page is navigated into.
 *
 * A browser only grants `window.open` inside the gesture that asked for it, and
 * the auth URL arrives one round trip later — so the tab must be allocated by
 * the gesture that STARTS an OAuth journey, never by the response that needs it.
 * `OAuthConnectDialog` satisfies that for the journeys whose gesture lives
 * inside it (Sign in, Retry), but the re-auth journey's gesture is the confirm
 * OUTSIDE it: the dialog POSTs as it mounts, so by the time it could open a tab
 * the gesture is already over and the popup is blocked.
 *
 * Hence the handoff below. The tab is a single browser-wide resource with a
 * lifetime that spans the gesture → mount boundary, which is exactly what a
 * component ref cannot hold; threading a live `Window` through page state and a
 * prop would model that lifetime as data and copy it. One module owns the
 * allocation, handoff, claim, navigation, retention and disposal.
 */

import type { OAuthFailureClass } from './asyncLifetime';

type ProviderTabState = 'handed' | 'claimed' | 'retry-pending' | 'retry-committed';
type ProviderTabPurpose = 'initial' | 'retry';
type ProviderTabRecord = { tab: Window; state: ProviderTabState };

/** The one browser tab this feature owns, from allocation through disposal. */
let providerTab: ProviderTabRecord | null = null;

/** Reasons are carried from the shared OAuth classifier to the one disposer. */
export type ProviderTabDisposalReason = OAuthFailureClass | 'success' | 'cleanup';

const closeTab = (tab: Window) => {
  if (tab.closed) return;
  try {
    tab.close();
  } catch {
    // Cross-origin or already-closing windows can reject access.
  }
};

/** Drop an old record before a new gesture allocates the single owned tab. */
const discardOwnedTab = () => {
  if (!providerTab) return;
  closeTab(providerTab.tab);
  providerTab = null;
};

/**
 * The single `window.open` for provider handoffs: blank, and with `opener`
 * severed, because the provider page is navigated into it later and would
 * otherwise be able to drive this window (reverse tabnabbing).
 */
export function preopenProviderTab(purpose: ProviderTabPurpose = 'initial'): Window | null {
  discardOwnedTab();
  try {
    const tab = window.open('about:blank', '_blank');
    if (tab) {
      try {
        tab.opener = null;
      } catch {
        // Some browser WindowProxy implementations expose a read-only opener.
      }
      providerTab = { tab, state: purpose === 'retry' ? 'retry-pending' : 'claimed' };
    }
    return tab;
  } catch {
    return null;
  }
}

/**
 * Allocate the tab for a journey that starts outside the dialog that will use
 * it. Called from the gesture; claimed by the dialog as it opens.
 */
export function handOffProviderTab(): void {
  const tab = preopenProviderTab();
  if (tab && providerTab) providerTab.state = 'handed';
}

/**
 * Claim the handed tab, exactly once. Returns null when no gesture handed one
 * over, or when the user closed it in the meantime — both mean "this journey
 * has no tab", which the visible fallback link already covers.
 *
 * Every dialog open claims, including a journey that allocates its own tab, so
 * a handoff whose journey never opened cannot survive into a later one.
 */
export function takeHandedProviderTab(): Window | null {
  if (!providerTab || providerTab.state !== 'handed') return null;
  if (providerTab.tab.closed) {
    providerTab = null;
    return null;
  }
  providerTab.state = 'claimed';
  return providerTab.tab;
}

/** Commit the retry handoff only once the next flow is definitely starting. */
export function commitProviderTabRetry(): void {
  if (providerTab?.state === 'retry-pending') providerTab.state = 'retry-committed';
}

/**
 * Take the owned tab for navigation and end this module's ownership. A settled
 * flow therefore cannot navigate a tab later, even if its auth URL is present.
 */
export function takeProviderTabForNavigation(): Window | null {
  if (!providerTab) return null;
  const tab = providerTab.tab;
  providerTab = null;
  if (tab.closed) return null;
  return tab;
}

/**
 * Dispose the owned tab, or retain a committed Retry tab across effect cleanup.
 * The caller supplies the shared classifier's value; no second terminal
 * predicate is allowed in this owner or its callers.
 */
export function disposeProviderTab(reason?: ProviderTabDisposalReason): void {
  if (!providerTab) return;
  if (providerTab.state === 'retry-committed' && reason === 'cleanup') {
    providerTab.state = 'handed';
    return;
  }
  closeTab(providerTab.tab);
  providerTab = null;
}
