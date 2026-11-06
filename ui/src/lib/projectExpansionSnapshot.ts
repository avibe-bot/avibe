// Which projects the reader left open, so the project list comes back the way
// they left it: after going back from a conversation, a reload, or a relaunch
// (docs/plans/web-ui-local-cache.md). One record per reader in the local
// snapshot store, so the store's rules hold here: per reader, best-effort, and
// whatever clears the store (sign-out, revocation, an authorization change,
// another reader on this device) takes this record with it.
//
// Only the ids are kept, not how far each project was paged: a restored project
// opens on its first page, and loads more on demand as before.

import { readLocalSnapshot, writeLocalSnapshot } from './localSnapshots';

const KEY = 'projects:expanded';
const SCHEMA = 1;
/** More open projects than anyone keeps open; a longer record is cut, not refused. */
const IDS_KEPT = 200;

type StoredExpansion = { ids: string[] };

const parse = (value: unknown): string[] | null => {
  if (!value || typeof value !== 'object') return null;
  const { ids } = value as Partial<StoredExpansion>;
  return Array.isArray(ids) && ids.every((id) => typeof id === 'string') ? ids.slice(0, IDS_KEPT) : null;
};

/** The open projects stored for the current reader; none when nothing is stored
 *  or the reader is not known yet. */
export async function readExpandedProjects(): Promise<string[]> {
  try {
    return (await readLocalSnapshot(KEY, SCHEMA, parse)) ?? [];
  } catch {
    return [];
  }
}

/** Store ``ids`` as the current reader's open projects. */
export function rememberExpandedProjects(ids: Iterable<string>): Promise<void> {
  return writeLocalSnapshot(KEY, SCHEMA, { ids: [...ids].slice(0, IDS_KEPT) } satisfies StoredExpansion);
}
