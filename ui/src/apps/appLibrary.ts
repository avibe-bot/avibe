// Pure projections for the App Library's two views. Kept free of React/DOM so
// the row derivation + page filtering are unit-testable in isolation; the view
// layer (LibraryApp / ShowPagesView) resolves each row's icon, title, and badge
// from the registry + loaded Show Pages.

import { showDockId } from '../context/DockContext';

export type AppRowKind = 'builtin' | 'showpage';

export interface AppRow {
  /** The persisted Dock id: a built-in app id verbatim, or `show:<session_id>`. */
  dockId: string;
  kind: AppRowKind;
  /** Built-in app id (files / terminal / editor / library) when kind === 'builtin'. */
  builtinId?: string;
  /** Session id when kind === 'showpage'. */
  sessionId?: string;
  /** Whether this app currently sits in the Dock (a member of `order`). */
  docked: boolean;
  /** Can be removed from the Apps LIST entirely (Show Pages yes — the page
   *  survives; built-ins never leave the list). */
  removable: boolean;
}

/**
 * Project the two-layer state into the Apps-view rows: the INSTALLED set, not
 * the docked subset (§7.1c). Rows are every built-in (including the Library
 * itself — it now lists itself, dockable like the rest) in canonical order,
 * then every installed Show Page (`pins`) in pin order. Each row carries a
 * ``docked`` flag (is it in ``order`` right now?) that drives the
 * dock/undock toggle; the docked subset no longer decides membership, so an
 * undocked built-in or undocked page still appears in the list.
 */
export function deriveAppRows(
  builtinIds: readonly string[],
  pins: readonly { session_id: string }[],
  order: readonly string[],
): AppRow[] {
  const docked = new Set(order);
  const rows: AppRow[] = [];
  const seen = new Set<string>();
  for (const id of builtinIds) {
    if (seen.has(id)) continue;
    seen.add(id);
    rows.push({ dockId: id, kind: 'builtin', builtinId: id, docked: docked.has(id), removable: false });
  }
  for (const pin of pins) {
    const sid = pin.session_id;
    if (!sid) continue;
    const dockId = showDockId(sid);
    if (seen.has(dockId)) continue;
    seen.add(dockId);
    rows.push({ dockId, kind: 'showpage', sessionId: sid, docked: docked.has(dockId), removable: true });
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
