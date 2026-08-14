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
 * allocation, and whichever journey opens next claims it.
 */

/** Allocated by a gesture, not yet claimed by the journey it was opened for. */
let handedTab: Window | null = null;

/**
 * The single `window.open` for provider handoffs: blank, and with `opener`
 * severed, because the provider page is navigated into it later and would
 * otherwise be able to drive this window (reverse tabnabbing).
 */
export function preopenProviderTab(): Window | null {
  try {
    const tab = window.open('about:blank', '_blank');
    if (tab) {
      try {
        tab.opener = null;
      } catch {
        // Some browser WindowProxy implementations expose a read-only opener.
      }
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
  handedTab = preopenProviderTab();
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
  const tab = handedTab;
  handedTab = null;
  if (!tab || tab.closed) return null;
  return tab;
}
