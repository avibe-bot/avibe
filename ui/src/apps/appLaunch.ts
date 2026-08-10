// How a modifier-held click/Enter on an app icon launches the app, plus the
// standalone browser URL a "new tab" launch needs. Kept pure (no React, no DOM)
// so EVERY launch surface — the desktop Dock, the App Library rows, the ⌘K
// results — shares ONE rule and one testable mapping instead of re-deriving it.

import { dockIdToSession } from '../context/dockDoc';
import { IS_APPLE, isIosDevice, isStandalonePwa } from '../lib/platform';
import { showPagePrivatePath } from './showPageAvatar';

/**
 * What a click (or Enter) on an app icon should do:
 *  - `newTab`    — the platform's tab modifier (⌘ on Apple, Ctrl elsewhere): hand the
 *                  app to the browser as a real tab, matching the universal web
 *                  convention for a modifier-click.
 *  - `newWindow` — Alt or Shift held: another workbench window for the same app (Alt
 *                  kept the pre-⌘ behavior; Shift is the browser's own new-window
 *                  modifier, so both read naturally).
 *  - `activate`  — plain click: focus / un-minimize / launch (surface-defined).
 */
export type AppLaunchIntent = 'newTab' | 'newWindow' | 'activate';

/** The modifier flags a launch intent is read from — satisfied by React's mouse
 *  AND keyboard synthetic events, so ⌘-click and ⌘+Enter map identically. */
export interface LaunchModifiers {
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}

/**
 * Map held modifiers to a launch intent. The tab modifier is platform-aware — ⌘
 * (without Ctrl) on Apple, Ctrl (without ⌘) elsewhere — the same idiom the Editor and
 * Terminal chords use: on macOS Ctrl+click is the right-click gesture, so it must not
 * mean "new tab" there (see `isAppleContextClick`). `isApple` is injectable so the
 * rule is testable on both platforms.
 */
export function appLaunchIntent(event: LaunchModifiers, isApple: boolean = IS_APPLE): AppLaunchIntent {
  if (isApple ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey) return 'newTab';
  if (event.altKey || event.shiftKey) return 'newWindow';
  return 'activate';
}

/**
 * Whether a MOUSE click is really macOS's right-click gesture (Ctrl held on an Apple
 * platform). Such a click belongs to the context menu — the surface must do nothing,
 * or the gesture would open the menu AND launch the app in browsers that still emit
 * the click. Only mouse handlers ask this; a Ctrl+Enter keypress is a normal launch,
 * which is why the check is separate from `appLaunchIntent`.
 */
export function isAppleContextClick(
  event: Pick<LaunchModifiers, 'ctrlKey'> & { detail?: number },
  isApple: boolean = IS_APPLE,
): boolean {
  if (!isApple || !event.ctrlKey) return false;
  // Enter/Space on a <button> (or a role="button") dispatches a synthetic CLICK whose
  // `detail` is 0, unlike a real mouse click (≥1). Without this check a Ctrl+Enter
  // keypress on Apple would be swallowed as a right-click and launch nothing — the
  // keyboard path this guard is explicitly not meant to cover.
  return (event.detail ?? 1) > 0;
}

/**
 * How the tab modifier is WRITTEN in a hint — the ⌘ glyph on Apple, the word `Ctrl`
 * elsewhere (Apple users read glyphs, everyone else reads names). Not translated: it
 * names a physical key. Interpolate it into `apps.dock.newTabChord` rather than
 * hard-coding a chord in any locale string, so the hint can never disagree with
 * `appLaunchIntent`.
 */
export function tabModifierLabel(isApple: boolean = IS_APPLE): string {
  return isApple ? '⌘' : 'Ctrl';
}

/** An app to resolve a standalone browser URL for: a built-in by id, or a
 *  Show Page by session id (`appId: 'showpage'`). */
export interface AppTabTarget {
  appId: string;
  sessionId?: string | null;
}

export interface AppTabContext {
  iosStandalone: boolean;
}

function currentAppTabContext(): AppTabContext {
  return { iosStandalone: isIosDevice() && isStandalonePwa() };
}

/**
 * Built-ins whose `/apps/<id>` route really renders the app STANDALONE on desktop
 * (App.tsx: `AppsFileBrowserPage` / `AppsTerminalPage` / `AppsEditorPage`).
 *
 * An allowlist, not a denylist, because the route is NOT a given: `/apps/library`
 * deliberately opens a Library WINDOW and redirects the tab to `/` on desktop (it also
 * serves the retired `/admin/show-pages` bookmark), so handing it to a new tab would
 * spawn a second whole workbench instead of the app — and `preview` has no route at
 * all. Both must fall back to a window. A new app therefore opts in HERE once its
 * standalone route exists, and until then degrades to the plain-click behavior.
 */
const STANDALONE_BUILTIN_ROUTES = new Set(['files', 'terminal', 'editor']);

/**
 * Marks a tab that was opened to show ONE app, so `AppShell` mounts without the
 * persisted workbench window layout (`WindowManagerProvider` otherwise restores
 * `avibe.workbench.windows.v1` on every desktop mount, and `WindowLayer` floats those
 * windows above the route outlet — a ⌘-clicked Files tab would open underneath a
 * restored Library window, and a maximized one would hide it outright).
 *
 * A URL param rather than shell state, so the mode survives a reload/bookmark of the
 * tab and stays invisible to the in-shell navigations (the sidebar Apps launcher, a
 * chat's "open in editor") that legitimately WANT the workbench layout.
 */
export const APP_TAB_PARAM = 'standalone';

/**
 * The URL that shows an app on its OWN browser tab, or null when it has no safe
 * standalone surface (so the caller can fall back to an in-app route/window).
 *
 * Installed iOS PWAs deliberately return null for every internal app. WebKit
 * opens `_blank` from a Home-Screen app in a secondary browser context and can
 * restore that context after process eviction, including its transient blank
 * document. Internal Avibe apps already have first-class in-shell routes and
 * windows, so there is no reason to expose that restoration failure mode.
 *
 *  - a Show Page → its private `/show/<sid>/` page, the SAME target as the tile's
 *    "Open in New Tab" menu item and the window titlebar's external-open button
 *    (`registry.externalHref`): the page itself, full viewport, no workbench chrome.
 *  - a built-in → its in-shell `/apps/<id>` route — only the ones that stand alone on
 *    desktop (see `STANDALONE_BUILTIN_ROUTES`) — flagged with `APP_TAB_PARAM` so the
 *    shell suppresses the restored window layer over it.
 *
 * Supplying `context` makes the policy deterministic in tests; production
 * callers default to the current display context.
 */
export function appTabHref(
  target: AppTabTarget,
  context: AppTabContext = currentAppTabContext(),
): string | null {
  if (context.iosStandalone) return null;
  if (target.appId === 'showpage') {
    const sessionId = (target.sessionId ?? '').trim();
    return sessionId ? showPagePrivatePath(sessionId) : null;
  }
  const appId = target.appId.trim();
  return STANDALONE_BUILTIN_ROUTES.has(appId) ? `/apps/${appId}?${APP_TAB_PARAM}=1` : null;
}

/**
 * Whether THIS document was opened as a standalone app tab (`appTabHref` above).
 * Read once at shell mount: the answer must not flip while the tab lives, or a later
 * in-tab navigation would re-restore the layout the tab was opened to avoid — and,
 * worse, a suppressed-then-enabled save would clobber the real layout with `[]`.
 *
 * Takes the raw `location.search` so it stays pure and testable.
 */
export function isStandaloneAppTab(search: string): boolean {
  if (!search) return false;
  try {
    return new URLSearchParams(search).get(APP_TAB_PARAM) === '1';
  } catch {
    return false;
  }
}

/**
 * Whether a pathname is one of the built-in app routes a standalone tab can land on
 * (`STANDALONE_BUILTIN_ROUTES`). Combined with `isStandaloneAppTab`, this is what tells
 * the shell to drop ALL chrome — sidebar, mobile header/tab bar, page padding — so the
 * app owns the whole viewport. Kept here, next to the allowlist it reads, so a new
 * standalone app opts into both behaviors in one place.
 *
 * Only the exact route matches: a standalone tab that navigates elsewhere (e.g. Files →
 * `/chat/<id>`) is an ordinary page again and gets the chrome back.
 */
export function isStandaloneAppRoutePath(pathname: string): boolean {
  const id = pathname.startsWith('/apps/') ? pathname.slice('/apps/'.length) : '';
  return STANDALONE_BUILTIN_ROUTES.has(id);
}

/** `appTabHref` for a persisted Dock id (`files` / `show:<session_id>`). */
export function appTabHrefForDockId(
  dockId: string,
  context?: AppTabContext,
): string | null {
  const sessionId = dockIdToSession(dockId);
  return sessionId !== null
    ? appTabHref({ appId: 'showpage', sessionId }, context)
    : appTabHref({ appId: dockId }, context);
}
