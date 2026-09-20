import { createContext, useContext } from 'react';

/**
 * Whether the shell behind the current surface draws the app sidebar at all.
 *
 * Published rather than re-derived, because only the shell can answer it. Two
 * shells draw no sidebar, and they disagree on what the answer depends on: the
 * setup wizard is a matter of the route, while a single-app tab turns on a
 * document flag frozen at mount — deliberately NOT re-read from the URL, which
 * the shell rewrites underneath it. Any surface trying to work this out from a
 * pathname could only ever cover the cases someone remembered to enumerate, and
 * would silently regress the next time a shell learns to drop its chrome.
 *
 * "At all" is the whole claim: the sidebar is hidden below `md`, and that is a
 * separate question, answered separately by whoever cares about the viewport.
 *
 * Read by the Settings menu placement, where `inline` is a claim about this
 * sidebar being on screen to sit beside — with no sidebar there is no inline
 * layout to have, whatever the owner picked for the windows that do have one.
 *
 * Defaults to true: an ordinary shell route draws a sidebar, and so does a
 * surface mounted outside the shell entirely in a test harness.
 */
export const ShellSidebarContext = createContext(true);

export function useShellHasSidebar(): boolean {
  return useContext(ShellSidebarContext);
}
