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

export interface PartitionedAppRows {
  /** Docked rows, ordered to match the Dock `order`, so this group's dockId
   *  sequence IS the Dock order — a drag-reorder maps 1:1 onto the existing
   *  `PUT /api/dock/order`. Carries the drag handles (§7.1e). */
  docked: AppRow[];
  /** Installed-but-undocked rows, kept in their incoming (stable) derivation
   *  order — built-ins canonical, then pins by install order. Rendered below the
   *  docked group with no handle; not part of the Dock order. */
  undocked: AppRow[];
}

/**
 * Split the Apps-view rows (from deriveAppRows) into the DOCKED group and the
 * installed-but-undocked group (§7.1e work item 2). Docked rows are sorted by
 * their index in the Dock `order`, so the group's dockId sequence equals
 * `order` and a framer-motion reorder of it can persist straight through
 * `PUT /api/dock/order`. Undocked rows keep their incoming stable order. Pure —
 * no React/DOM, no input mutation — so the partition is unit-testable.
 */
export function partitionByDock(rows: readonly AppRow[], order: readonly string[]): PartitionedAppRows {
  const rank = new Map(order.map((id, index) => [id, index] as const));
  const docked = rows
    .filter((row) => row.docked)
    .sort((a, b) => (rank.get(a.dockId) ?? 0) - (rank.get(b.dockId) ?? 0));
  const undocked = rows.filter((row) => !row.docked);
  return { docked, undocked };
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
