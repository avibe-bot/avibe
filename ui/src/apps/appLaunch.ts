// How a modifier-held click/Enter on an app icon launches the app, plus the
// standalone browser URL a "new tab" launch needs. Kept pure (no React, no DOM)
// so EVERY launch surface — the desktop Dock, the App Library rows, the ⌘K
// results — shares ONE rule and one testable mapping instead of re-deriving it.

import { dockIdToSession } from '../context/dockDoc';
import { IS_APPLE } from '../lib/platform';
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
 * The URL that shows an app on its OWN browser tab, or null when it has no
 * standalone surface (so the caller can fall back to a window).
 *
 *  - a Show Page → its private `/show/<sid>/` page, the SAME target as the tile's
 *    "Open in New Tab" menu item and the window titlebar's external-open button
 *    (`registry.externalHref`): the page itself, full viewport, no workbench chrome.
 *  - a built-in → its in-shell `/apps/<id>` route, but only the ones that stand alone
 *    on desktop (see `STANDALONE_BUILTIN_ROUTES`).
 *
 * Pure.
 */
export function appTabHref(target: AppTabTarget): string | null {
  if (target.appId === 'showpage') {
    const sessionId = (target.sessionId ?? '').trim();
    return sessionId ? showPagePrivatePath(sessionId) : null;
  }
  const appId = target.appId.trim();
  return STANDALONE_BUILTIN_ROUTES.has(appId) ? `/apps/${appId}` : null;
}

/** `appTabHref` for a persisted Dock id (`files` / `show:<session_id>`). Pure. */
export function appTabHrefForDockId(dockId: string): string | null {
  const sessionId = dockIdToSession(dockId);
  return sessionId !== null ? appTabHref({ appId: 'showpage', sessionId }) : appTabHref({ appId: dockId });
}
