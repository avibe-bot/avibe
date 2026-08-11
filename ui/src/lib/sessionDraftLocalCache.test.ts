import { describe, expect, it } from 'vitest';
import {
  SESSION_DRAFT_INVALIDATION_PREFIX,
  SESSION_DRAFT_MUTATION_PREFIX,
  SESSION_DRAFT_STORAGE_PREFIX,
  SessionDraftLocalCache,
} from './sessionDraftLocalCache';

class MemoryStorage {
  readonly values = new Map<string, string>();

  get length(): number {
    return this.values.size;
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null;
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

describe('SessionDraftLocalCache', () => {
  it('stores dirty and clean versioned records per session', () => {
    const storage = new MemoryStorage();
    let sequence = 0;
    const cache = new SessionDraftLocalCache(
      storage,
      () => `id-${++sequence}`,
      () => 100 + sequence,
    );

    const dirty = cache.writeDirty('session/a', 'typing', 'server-1');
    expect(cache.read('session/a')).toEqual({
      version: 1,
      text: 'typing',
      serverUpdatedAt: 'server-1',
      dirty: true,
      mutationId: 'id-1',
      savedAt: 101,
    });
    expect([...storage.values.keys()]).toEqual([
      `${SESSION_DRAFT_MUTATION_PREFIX}session%2Fa:id-1`,
    ]);

    cache.acknowledge('session/a', dirty.mutationId, 'typing', 'server-2');
    expect(cache.read('session/a')).toMatchObject({
      text: 'typing',
      serverUpdatedAt: 'server-2',
      dirty: false,
      mutationId: 'id-1',
    });
    expect([...storage.values.keys()]).toEqual([
      `${SESSION_DRAFT_STORAGE_PREFIX}session%2Fa`,
    ]);
  });

  it('removes an empty clean draft and ignores stale acknowledgements or rebases', () => {
    const storage = new MemoryStorage();
    const cache = new SessionDraftLocalCache(storage, () => 'current', () => 1);
    cache.writeDirty('session-a', 'text', null);

    cache.rebaseDirty('session-a', 'stale', 'server-1');
    expect(cache.read('session-a')?.serverUpdatedAt).toBeNull();
    cache.acknowledge('session-a', 'stale', 'old text', 'server-1');
    expect(cache.read('session-a')).toMatchObject({ text: 'text', dirty: true });

    cache.acknowledge('session-a', 'current', 'text', 'server-1');
    expect(cache.read('session-a')).toMatchObject({
      text: 'text',
      dirty: false,
      mutationId: 'current',
      serverUpdatedAt: 'server-1',
    });

    cache.writeClean('session-a', '', 'server-2');
    expect(cache.read('session-a')).toBeNull();
  });

  it('enumerates only dirty session records for reconnect replay', () => {
    const storage = new MemoryStorage();
    let sequence = 0;
    const cache = new SessionDraftLocalCache(storage, () => `id-${++sequence}`, () => sequence);
    cache.writeDirty('session/a', 'offline A', 'server-1');
    storage.setItem(`${SESSION_DRAFT_STORAGE_PREFIX}broken`, '{not-json');
    cache.writeClean('session-b', 'synced B', 'server-2');
    cache.writeDirty('session-c', 'offline C', null);

    expect(cache.dirtySessionIds()).toEqual(['session/a', 'session-c']);
    expect(storage.getItem(`${SESSION_DRAFT_STORAGE_PREFIX}broken`)).toBeNull();
  });

  it('acknowledges one tab without replacing another tab mutation', () => {
    const storage = new MemoryStorage();
    const first = new SessionDraftLocalCache(storage, () => 'tab-a', () => 1);
    const second = new SessionDraftLocalCache(storage, () => 'tab-b', () => 2);

    first.writeDirty('session-a', 'from tab A', null);
    const removeItem = storage.removeItem.bind(storage);
    storage.removeItem = (key) => {
      if (key === `${SESSION_DRAFT_MUTATION_PREFIX}session-a:tab-a`) {
        second.writeDirty('session-a', 'from tab B', null);
      }
      removeItem(key);
    };
    first.acknowledge('session-a', 'tab-a', 'from tab A', 'rev-a');

    expect(second.read('session-a')).toMatchObject({
      text: 'from tab B',
      dirty: true,
      mutationId: 'tab-b',
    });
    expect(storage.getItem(
      `${SESSION_DRAFT_MUTATION_PREFIX}session-a:tab-b`,
    )).not.toBeNull();
    first.writeClean('session-a', '', 'clear-revision');
    expect(second.read('session-a')).toMatchObject({
      text: 'from tab B',
      dirty: true,
      mutationId: 'tab-b',
    });
  });

  it('persists archive invalidation across cache instances', () => {
    const storage = new MemoryStorage();
    const first = new SessionDraftLocalCache(storage, () => 'archive-1', () => 1);
    const second = new SessionDraftLocalCache(storage, () => 'unused', () => 2);

    expect(first.readInvalidation('session/a')).toBeNull();
    expect(first.invalidate('session/a')).toBe('archive-1');
    expect(second.readInvalidation('session/a')).toBe('archive-1');
    expect(storage.getItem(
      `${SESSION_DRAFT_INVALIDATION_PREFIX}session%2Fa`,
    )).toBe('archive-1');
  });

  it('keeps a newer in-memory invalidation when storage retains an older token', () => {
    const storage = new MemoryStorage();
    let sequence = 0;
    let blockWrites = false;
    const setItem = storage.setItem.bind(storage);
    storage.setItem = (key, value) => {
      if (blockWrites) throw new Error('read only');
      setItem(key, value);
    };
    const cache = new SessionDraftLocalCache(
      storage,
      () => `archive-${++sequence}`,
      () => sequence,
    );

    expect(cache.invalidate('session-a')).toBe('archive-1');
    blockWrites = true;
    expect(cache.invalidate('session-a')).toBe('archive-2');
    expect(storage.getItem(
      `${SESSION_DRAFT_INVALIDATION_PREFIX}session-a`,
    )).toBe('archive-1');
    expect(cache.readInvalidation('session-a')).toBe('archive-2');
  });

  it('discards malformed data and tolerates blocked storage', () => {
    const storage = new MemoryStorage();
    storage.setItem(`${SESSION_DRAFT_STORAGE_PREFIX}broken`, '{not-json');
    const cache = new SessionDraftLocalCache(storage);

    expect(cache.read('broken')).toBeNull();
    expect(storage.values.size).toBe(0);

    const blocked = {
      getItem: () => { throw new Error('blocked'); },
      setItem: () => { throw new Error('full'); },
      removeItem: () => { throw new Error('blocked'); },
    };
    const fallback = new SessionDraftLocalCache(blocked, () => 'id', () => 1);
    expect(() => fallback.writeDirty('session-a', 'text', null)).not.toThrow();
    expect(fallback.read('session-a')).toBeNull();
    expect(() => fallback.clear('session-a')).not.toThrow();
    expect(fallback.invalidate('session-a')).toBe('id');
    expect(fallback.readInvalidation('session-a')).toBe('id');
  });
});
