// Pure route helpers for the mobile Dock drawer + the mobile Show Page surface.
// Kept free of React/DOM so the id → route mapping is unit-testable in isolation
// (the drawer component and the Library open-path both consume these). On mobile
// there is no window layer, so every Dock tile opens as a full-screen `/apps/*`
// route instead of a workbench window (§7.1b).

import { dockIdToSession } from '../../context/DockContext';

/**
 * The in-shell route that frames a pinned Show Page full-screen on mobile
 * (`/apps/show/:sessionId`). This is the mobile counterpart to the desktop
 * Show Page window; the Dock drawer and the Library AI rows navigate here
 * instead of opening the raw `/show/` surface in a new tab. Pure.
 */
export function showAppRoutePath(sessionId: string): string {
  return `/apps/show/${encodeURIComponent(sessionId)}`;
}

/**
 * The full-screen route a Dock tile opens on mobile. A built-in id maps to its
 * `/apps/<id>` route (files / terminal / editor / library — the routes declared
 * in App.tsx); a `show:<session_id>` pin maps to the full-screen Show Page
 * route. Mirrors the desktop Dock's per-kind open behavior, minus the window
 * layer. Pure — no DOM, unit-testable.
 */
export function mobileRouteForDockId(dockId: string): string {
  const sessionId = dockIdToSession(dockId);
  return sessionId !== null ? showAppRoutePath(sessionId) : `/apps/${dockId}`;
}
