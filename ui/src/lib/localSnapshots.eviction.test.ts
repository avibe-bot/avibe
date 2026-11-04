import 'fake-indexeddb/auto';

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  clearLocalSnapshots,
  deleteLocalSnapshot,
  getLocalSnapshotEpoch,
  pruneLocalSnapshots,
  readLocalSnapshot,
  setLocalSnapshotPrincipal,
  writeLocalSnapshot,
} from './localSnapshots';

// The store calls conversation snapshots need beyond read and write: removing one
// record whoever stored it, keeping only the most recent of a kind, and telling a
// writer that the store was cleared since it read its data.

const asText = (value: unknown) => (typeof value === 'string' ? value : null);
const read = (key: string) => readLocalSnapshot(key, 1, asText);

afterEach(async () => {
  vi.restoreAllMocks();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('local snapshot eviction', () => {
  it('deletes a key for every reader, even while the reader is unknown', async () => {
    setLocalSnapshotPrincipal('local');
    await writeLocalSnapshot('chat:a', 1, 'rows of a');
    await writeLocalSnapshot('chat:b', 1, 'rows of b');
    expect(await read('chat:a')).toBe('rows of a');

    setLocalSnapshotPrincipal(null);
    await deleteLocalSnapshot('chat:a');
    setLocalSnapshotPrincipal('local');
    expect(await read('chat:a')).toBeNull();
    expect(await read('chat:b')).toBe('rows of b');
  });

  it('keeps the most recently saved records of one kind and leaves other kinds alone', async () => {
    let now = 1_000;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    setLocalSnapshotPrincipal('local');
    await writeLocalSnapshot('inbox:first-page', 1, 'inbox');
    for (const id of ['a', 'b', 'c', 'd']) {
      now += 10;
      await writeLocalSnapshot(`chat:${id}`, 1, `rows of ${id}`);
    }
    // Rewriting ``a`` makes it the most recent one.
    now += 10;
    await writeLocalSnapshot('chat:a', 1, 'rows of a again');

    await pruneLocalSnapshots('chat:', 2);

    expect(await read('chat:a')).toBe('rows of a again');
    expect(await read('chat:d')).toBe('rows of d');
    expect(await read('chat:b')).toBeNull();
    expect(await read('chat:c')).toBeNull();
    expect(await read('inbox:first-page')).toBe('inbox');
  });

  it('advances its epoch on every clear', async () => {
    const before = getLocalSnapshotEpoch();
    await clearLocalSnapshots();
    expect(getLocalSnapshotEpoch()).toBe(before + 1);
  });
});
