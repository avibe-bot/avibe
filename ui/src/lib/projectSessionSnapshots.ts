// Each project's first page of Sessions as the server last answered it, so that
// expanding a project in the sidebar or on the phone's Projects page draws its
// rows in the same frame, while the live read that replaces them is in flight
// (docs/plans/web-ui-local-cache.md). The store's rules hold here
// (lib/localSnapshots): per reader, best-effort, and the server stays
// authoritative. What this module adds:
//
//  * A synchronous read. Expanding must not wait for IndexedDB, which would show
//    one empty frame first, so the records are kept in memory as well, for the
//    reader currently named. IndexedDB is the cold-start source the memory is
//    hydrated from; a reader change or a whole-store clear empties the memory.
//  * Only a server answer is stored: the provider writes a project's first page
//    when a live read of it commits, never rows it holds optimistically.
//  * A record goes when its project or one of its Sessions leaves the reader's
//    view (an archive, a hide, a deletion), and the 40 most recently written are
//    kept.
//
// The surface is deliberately small (read, hydrate, remember, forget), so the
// local sync store can take it over without its callers changing.

import type { WorkbenchSession } from '../context/ApiContext';
import {
  deleteLocalSnapshot,
  getLocalSnapshotEpoch,
  getLocalSnapshotPrincipal,
  pruneLocalSnapshots,
  readLocalSnapshotsWithPrefix,
  writeLocalSnapshot,
} from './localSnapshots';

const SCHEMA = 1;
const KEY_PREFIX = 'projects:sessions:';
/** Projects kept, least recently written evicted first. */
const PROJECTS_KEPT = 40;
/** Evicting at the cap goes this far below it, so the full read eviction needs
 *  happens once every few new projects instead of on each. */
const EVICTION_SLACK = 8;
/** Rows kept per project: the first page the provider reads. */
export const PROJECT_SESSIONS_SNAPSHOT_ROWS = 8;

export type ProjectSessionsSnapshot = {
  sessions: WorkbenchSession[];
  next_before_id: string | null;
};

const keyOf = (projectId: string) => `${KEY_PREFIX}${projectId}`;

// Only rows every consumer can render: an unreadable record is a missing one.
const parse = (value: unknown): ProjectSessionsSnapshot | null => {
  if (!value || typeof value !== 'object') return null;
  const { sessions, next_before_id: cursor } = value as Partial<ProjectSessionsSnapshot>;
  if (!Array.isArray(sessions) || (cursor !== null && typeof cursor !== 'string')) return null;
  const rowsValid = sessions.every((row) =>
    row && typeof row === 'object'
    && typeof row.id === 'string'
    && (row.title === null || typeof row.title === 'string')
    && typeof row.agent_status === 'string'
    && typeof row.last_active_at === 'string');
  return rowsValid ? { sessions: sessions.slice(0, PROJECT_SESSIONS_SNAPSHOT_ROWS), next_before_id: cursor } : null;
};

// The records in memory, for one reader and one store generation.
type Memory = { principal: string; epoch: number; pages: Map<string, ProjectSessionsSnapshot> };
let memory: Memory | null = null;
// Advanced by every write and every forget of a project, so a hydration that
// started before either cannot put back an older record.
const touched = new Map<string, number>();

/** The memory for the reader named now, emptied when the reader or the store changed. */
function currentMemory(): Memory | null {
  const principal = getLocalSnapshotPrincipal();
  if (!principal) {
    memory = null;
    return null;
  }
  const epoch = getLocalSnapshotEpoch();
  if (!memory || memory.principal !== principal || memory.epoch !== epoch) {
    memory = { principal, epoch, pages: new Map() };
  }
  return memory;
}

/** The project's last stored first page for the current reader, synchronously. */
export function cachedProjectSessions(projectId: string): ProjectSessionsSnapshot | null {
  return currentMemory()?.pages.get(projectId) ?? null;
}

/** Load the current reader's stored pages into memory. Pages written or forgotten
 *  since the load began keep what this document did with them. */
export async function hydrateProjectSessionSnapshots(): Promise<void> {
  const start = currentMemory();
  if (!start) return;
  const before = new Map(touched);
  const stored = await readLocalSnapshotsWithPrefix(KEY_PREFIX, SCHEMA, parse);
  const now = currentMemory();
  if (now !== start) return;
  for (const [key, page] of stored) {
    const projectId = key.slice(KEY_PREFIX.length);
    if ((touched.get(projectId) ?? 0) !== (before.get(projectId) ?? 0) || now.pages.has(projectId)) continue;
    now.pages.set(projectId, page);
  }
}

/** Store the first page a live read of the project just committed, for the reader
 *  named now. Rows past the first page are not kept. */
export function rememberProjectSessions(projectId: string, page: ProjectSessionsSnapshot): void {
  const target = currentMemory();
  if (!target) return;
  touched.set(projectId, (touched.get(projectId) ?? 0) + 1);
  const added = !target.pages.has(projectId);
  const value: ProjectSessionsSnapshot = {
    sessions: page.sessions.slice(0, PROJECT_SESSIONS_SNAPSHOT_ROWS),
    next_before_id: page.next_before_id,
  };
  target.pages.set(projectId, value);
  void writeLocalSnapshot(keyOf(projectId), SCHEMA, value).then(() =>
    added ? pruneLocalSnapshots(KEY_PREFIX, PROJECTS_KEPT, { slack: EVICTION_SLACK }) : undefined,
  );
}

/** Drop the project's page, in memory and in storage, for every reader. */
export function forgetProjectSessions(projectId: string): void {
  touched.set(projectId, (touched.get(projectId) ?? 0) + 1);
  memory?.pages.delete(projectId);
  void deleteLocalSnapshot(keyOf(projectId));
}

/** The projects whose stored page holds this Session, as far as memory knows. */
export function projectsWithCachedSession(sessionId: string): string[] {
  const pages = currentMemory()?.pages;
  if (!pages) return [];
  return [...pages].filter(([, page]) => page.sessions.some((row) => row.id === sessionId)).map(([id]) => id);
}

/** The projects memory holds a page for. */
export function cachedSessionProjects(): string[] {
  return [...(currentMemory()?.pages.keys() ?? [])];
}
