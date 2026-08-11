import { describe, expect, it } from 'vitest';
import {
  SESSION_DRAFT_STORAGE_PREFIX,
  SessionDraftLocalCache,
} from './sessionDraftLocalCache';

class MemoryStorage {
  readonly values = new Map<string, string>();

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

    cache.writeDirty('session/a', 'typing', 'server-1');
    expect(cache.read('session/a')).toEqual({
      version: 1,
      text: 'typing',
      serverUpdatedAt: 'server-1',
      dirty: true,
      mutationId: 'id-1',
      savedAt: 101,
    });

    cache.writeClean('session/a', 'typing', 'server-2');
    expect(cache.read('session/a')).toMatchObject({
      text: 'typing',
      serverUpdatedAt: 'server-2',
      dirty: false,
      mutationId: 'id-2',
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

    cache.writeClean('session-a', '', 'server-2');
    expect(cache.read('session-a')).toBeNull();
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
  });
});
