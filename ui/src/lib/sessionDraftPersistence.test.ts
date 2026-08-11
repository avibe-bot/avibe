import { describe, expect, it, vi } from 'vitest';
import { SessionDraftLocalCache } from './sessionDraftLocalCache';
import {
  SessionDraftPersistence,
  type SessionDraftSaveResult,
  type SessionDraftWrite,
} from './sessionDraftPersistence';

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
};

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

const localCache = (storage = new MemoryStorage()) => {
  let sequence = 0;
  return new SessionDraftLocalCache(
    storage,
    () => `mutation-${++sequence}`,
    () => sequence,
  );
};

describe('SessionDraftPersistence', () => {
  it('serializes one session and rebases a newer edit onto the successful cloud revision', async () => {
    const cache = localCache();
    const persistence = new SessionDraftPersistence(cache);
    const first = deferred<SessionDraftSaveResult>();
    const calls: SessionDraftWrite[] = [];

    persistence.save('session-a', 'first', async (draft) => {
      calls.push(draft);
      return first.promise;
    });
    await Promise.resolve();
    persistence.cache('session-a', 'second');
    const second = persistence.save('session-a', 'second', async (draft) => {
      calls.push(draft);
      return { ok: true, server: { text: draft.text, updatedAt: 'rev-2' } };
    });

    expect(calls).toEqual([{ text: 'first', expectedUpdatedAt: null }]);
    first.resolve({ ok: true, server: { text: 'first', updatedAt: 'rev-1' } });
    await second;

    expect(calls).toEqual([
      { text: 'first', expectedUpdatedAt: null },
      { text: 'second', expectedUpdatedAt: 'rev-1' },
    ]);
    expect(cache.read('session-a')).toMatchObject({
      text: 'second',
      serverUpdatedAt: 'rev-2',
      dirty: false,
    });
    expect(persistence.revision('session-a')).toBe(0);
  });

  it('does not block writes for different sessions', async () => {
    const persistence = new SessionDraftPersistence(localCache());
    const first = deferred<SessionDraftSaveResult>();
    const calls: string[] = [];

    persistence.save('session-a', 'first', async () => {
      calls.push('a');
      return first.promise;
    });
    await persistence.save('session-b', 'other', async () => {
      calls.push('b');
      return { ok: true, server: { text: 'other', updatedAt: 'rev-b' } };
    });

    expect(calls).toEqual(['a', 'b']);
    first.resolve({ ok: true, server: { text: 'first', updatedAt: 'rev-a' } });
  });

  it('does not let a slow acknowledgement overwrite another tab local mutation', async () => {
    const storage = new MemoryStorage();
    const firstCache = new SessionDraftLocalCache(storage, () => 'tab-a', () => 1);
    const secondCache = new SessionDraftLocalCache(storage, () => 'tab-b', () => 2);
    const first = new SessionDraftPersistence(firstCache);
    const slow = deferred<SessionDraftSaveResult>();

    const save = first.save('session-a', 'from tab A', async () => slow.promise);
    secondCache.writeDirty('session-a', 'from tab B', null);
    slow.resolve({ ok: true, server: { text: 'from tab A', updatedAt: 'rev-1' } });
    await save;

    expect(secondCache.read('session-a')).toMatchObject({
      text: 'from tab B',
      dirty: true,
      mutationId: 'tab-b',
    });
  });

  it('keeps local text when a read starts during a pending write', async () => {
    const persistence = new SessionDraftPersistence(localCache());
    const first = deferred<SessionDraftSaveResult>();
    persistence.save('session-a', 'draft', async () => first.promise);

    const read = persistence.beginRead('session-a');
    expect(persistence.reconcileRead('session-a', read, { text: '', updatedAt: null })).toBe('draft');
    first.resolve({ ok: true, server: { text: 'draft', updatedAt: 'rev-1' } });
    await Promise.resolve();
  });

  it('retains a successful revision until an older read reconciles', async () => {
    const persistence = new SessionDraftPersistence(localCache());
    const read = persistence.beginRead('session-a');
    await persistence.save('session-a', 'newer', async () => ({
      ok: true,
      server: { text: 'newer', updatedAt: 'rev-2' },
    }));

    expect(persistence.reconcileRead(
      'session-a',
      read,
      { text: 'old', updatedAt: 'rev-1' },
    )).toBe('newer');
    expect(persistence.revision('session-a')).toBe(0);
  });

  it('recovers a failed write from local storage in a new persistence instance', async () => {
    const storage = new MemoryStorage();
    const first = new SessionDraftPersistence(localCache(storage));
    await first.save('session-a', 'offline text', async () => ({ ok: false }));

    const restored = new SessionDraftPersistence(localCache(storage));
    expect(restored.peek('session-a')).toBe('offline text');
    const read = restored.beginRead('session-a');
    expect(restored.reconcileRead(
      'session-a',
      read,
      { text: '', updatedAt: null },
    )).toBe('offline text');
    const write = vi.fn(async (draft: SessionDraftWrite) => ({
      ok: true,
      server: { text: draft.text, updatedAt: 'rev-1' },
    }));
    await restored.retry('session-a', write);
    expect(write).toHaveBeenCalledWith({
      text: 'offline text',
      expectedUpdatedAt: null,
    });
  });

  it('lets an authoritative newer cloud revision replace a clean local copy', async () => {
    const storage = new MemoryStorage();
    const first = new SessionDraftPersistence(localCache(storage));
    await first.save('session-a', 'synced', async () => ({
      ok: true,
      server: { text: 'synced', updatedAt: 'rev-1' },
    }));

    const restored = new SessionDraftPersistence(localCache(storage));
    expect(restored.peek('session-a')).toBe('synced');
    const read = restored.beginRead('session-a');
    expect(restored.reconcileRead(
      'session-a',
      read,
      { text: 'other device', updatedAt: 'rev-2' },
    )).toBe('other device');
    expect(restored.peek('session-a')).toBe('other device');
  });

  it('preserves a dirty local draft and stops automatic writes on a version conflict', async () => {
    const persistence = new SessionDraftPersistence(localCache());
    const firstRead = persistence.beginRead('session-a');
    persistence.reconcileRead('session-a', firstRead, { text: 'cloud', updatedAt: 'rev-1' });
    persistence.cache('session-a', 'local edit');

    const racingRead = persistence.beginRead('session-a');
    expect(persistence.reconcileRead(
      'session-a',
      racingRead,
      { text: 'other device', updatedAt: 'rev-2' },
    )).toBe('local edit');

    const write = vi.fn(async () => ({ ok: true }));
    await expect(persistence.retry('session-a', write)).resolves.toMatchObject({
      ok: false,
      conflict: true,
    });
    expect(write).not.toHaveBeenCalled();
    expect(persistence.peek('session-a')).toBe('local edit');
  });

  it('treats a same-text conflict as convergence and cleans the local record', async () => {
    const cache = localCache();
    const persistence = new SessionDraftPersistence(cache);

    const result = await persistence.save('session-a', '', async () => ({
      ok: false,
      conflict: true,
      server: { text: '', updatedAt: 'clear-revision' },
    }));

    expect(result).toMatchObject({ ok: true });
    expect(cache.read('session-a')).toBeNull();
    expect(persistence.revision('session-a')).toBe(0);
  });

  it('removes persistent and in-memory state when a session is archived', async () => {
    const cache = localCache();
    const persistence = new SessionDraftPersistence(cache);
    await persistence.save('session-a', 'draft', async () => ({ ok: false }));

    persistence.clearSession('session-a');

    expect(cache.read('session-a')).toBeNull();
    expect(persistence.revision('session-a')).toBe(0);
    expect(persistence.beginRead('session-a').pending).toBe(false);
  });
});
