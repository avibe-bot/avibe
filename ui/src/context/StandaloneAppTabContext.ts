import { createContext, useContext } from 'react';

/**
 * Whether THIS document was opened as a single-app tab (`?standalone=1`, see
 * `apps/appLaunch.ts`). The shell freezes the flag at mount and publishes it here so
 * the app pages under the route Outlet can render FULL-BLEED — no page header, no
 * rounded card, no shell chrome — instead of the padded workbench page layout.
 *
 * Deliberately separate from the `windowed` prop an app body receives inside an
 * AppWindow: `windowed` also carries window semantics (ephemeral terminal sessions,
 * close guards, persisted window state), while a standalone tab is still the ROUTE
 * mount and keeps that route's behavior — it only drops the chrome.
 *
 * Defaults to false, so a page mounted outside the shell (tests, storybook-ish
 * harnesses) keeps the normal padded layout.
 */
export const StandaloneAppTabContext = createContext(false);

export function useStandaloneAppTab(): boolean {
  return useContext(StandaloneAppTabContext);
}
