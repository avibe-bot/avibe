// Pure projections for the App Library's two views. Kept free of React/DOM so
// the row derivation + page filtering are unit-testable in isolation; the view
// layer (LibraryApp / ShowPagesView) resolves each row's icon, title, and badge
// from the registry + loaded Show Pages.

import { dockIdToSession } from '../context/DockContext';

export type AppRowKind = 'builtin' | 'showpage';

export interface AppRow {
  /** The persisted Dock id: a built-in app id verbatim, or `show:<session_id>`. */
  dockId: string;
  kind: AppRowKind;
  /** Built-in app id (files / terminal / editor …) when kind === 'builtin'. */
  builtinId?: string;
  /** Session id when kind === 'showpage'. */
  sessionId?: string;
  /** Show Pages can be removed from the Dock (the page survives); built-ins can't. */
  removable: boolean;
}

/**
 * Project the Dock order into the Apps-view rows: the docked set, in order,
 * minus the Library app itself (a manager never lists itself) and any ids this
 * client doesn't know (a future kind not yet shipped). Built-ins are matched
 * against ``builtinIds``; every ``show:<id>`` is a pinned Show Page.
 */
export function deriveAppRows(order: string[], builtinIds: ReadonlySet<string>, selfId: string): AppRow[] {
  const rows: AppRow[] = [];
  const seen = new Set<string>();
  for (const dockId of order) {
    if (seen.has(dockId)) continue;
    seen.add(dockId);
    if (dockId === selfId) continue; // the Library app is not listed among the apps it manages
    const sessionId = dockIdToSession(dockId);
    if (sessionId !== null) {
      rows.push({ dockId, kind: 'showpage', sessionId, removable: true });
    } else if (builtinIds.has(dockId)) {
      rows.push({ dockId, kind: 'builtin', builtinId: dockId, removable: false });
    }
    // else: an id this client can't resolve (unknown/future kind) → skip it
  }
  return rows;
}

export type ShowPageFilter = 'all' | 'public' | 'private' | 'offline';

export interface FilterablePage {
  session_id: string;
  visibility: 'public' | 'private' | 'offline';
  title: string | null;
}

/**
 * Filter the Show Pages inventory by visibility bucket + a free-text query over
 * title and session id. ``'all'`` keeps every visibility (including offline);
 * an empty query matches everything.
 */
export function filterShowPages<T extends FilterablePage>(pages: T[], filter: ShowPageFilter, query: string): T[] {
  const q = query.trim().toLowerCase();
  return pages.filter((page) => {
    if (filter !== 'all' && page.visibility !== filter) return false;
    if (!q) return true;
    return (page.title || '').toLowerCase().includes(q) || page.session_id.toLowerCase().includes(q);
  });
}
