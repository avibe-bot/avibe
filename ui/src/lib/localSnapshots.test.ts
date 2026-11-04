// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { afterEach, describe, expect, it } from 'vitest';

import {
  LOCAL_SNAPSHOT_MAX_BYTES,
  clearLocalSnapshots,
  readLocalSnapshot,
  setLocalSnapshotPrincipal,
  writeLocalSnapshot,
} from './localSnapshots';

// The store is what keeps one reader's cached Inbox away from the next person to
// sign in on the same device, and what keeps a stale shape from reaching a
// renderer. Nothing else guards either: callers trust what it returns.

const asText = (value: unknown) => (typeof value === 'string' ? value : null);
const read = (key: string, schema = 1) => readLocalSnapshot(key, schema, asText);
// Principal changes delete other readers' records in the background; let that
// transaction land before asserting on it.
const idle = () => new Promise((resolve) => setTimeout(resolve, 20));

afterEach(async () => {
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
  window.localStorage.clear();
});

describe('local snapshots', () => {
  it('returns a snapshot only to the reader it was written for, and drops it when another reader signs in', async () => {
    setLocalSnapshotPrincipal('remote:alice');
    await writeLocalSnapshot('inbox', 1, 'alice rows');
    expect(await read('inbox')).toBe('alice rows');

    setLocalSnapshotPrincipal('remote:bob');
    await idle();
    expect(await read('inbox')).toBeNull();

    // Alice signing back in finds nothing: Bob's sign-in deleted her records.
    setLocalSnapshotPrincipal('remote:alice');
    await idle();
    expect(await read('inbox')).toBeNull();
  });

  it('neither reads nor writes while the reader is unknown', async () => {
    await writeLocalSnapshot('inbox', 1, 'nobody');
    setLocalSnapshotPrincipal('local');
    expect(await read('inbox')).toBeNull();
    setLocalSnapshotPrincipal(null);
    expect(await read('inbox')).toBeNull();
  });

  it('ignores a record written under another schema, one its parser rejects, and one too large to store', async () => {
    setLocalSnapshotPrincipal('local');
    await writeLocalSnapshot('inbox', 1, 'v1 rows');
    expect(await read('inbox', 2)).toBeNull();
    expect(await readLocalSnapshot('inbox', 1, () => null)).toBeNull();

    // An oversized value replaces nothing with a stale copy: the old record goes.
    await writeLocalSnapshot('inbox', 1, 'x'.repeat(LOCAL_SNAPSHOT_MAX_BYTES + 1));
    expect(await read('inbox')).toBeNull();
  });

  it('forgets everything on clear', async () => {
    setLocalSnapshotPrincipal('local');
    await writeLocalSnapshot('inbox', 1, 'rows');
    await clearLocalSnapshots();
    expect(await read('inbox')).toBeNull();
  });

  it('refuses a write that lands after any tab of the origin signed out', async () => {
    setLocalSnapshotPrincipal('remote:alice');
    // Another tab signs out: it bumps the generation every tab shares.
    window.localStorage.setItem('avibe.sign-out-generation.v1', String(Date.now()));
    await writeLocalSnapshot('inbox', 1, 'alice rows after sign-out');
    expect(await read('inbox')).toBeNull();
  });
});
