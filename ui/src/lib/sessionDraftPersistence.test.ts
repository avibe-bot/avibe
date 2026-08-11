import { describe, expect, it, vi } from 'vitest';
import {
  SESSION_DRAFT_MUTATION_PREFIX,
  SessionDraftLocalCache,
} from './sessionDraftLocalCache';
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

  it('keeps each live tab dirty mutation authoritative until it is acknowledged', async () => {
    const storage = new MemoryStorage();
    const firstCache = new SessionDraftLocalCache(storage, () => 'tab-a', () => 1);
    const secondCache = new SessionDraftLocalCache(storage, () => 'tab-b', () => 2);
    const first = new SessionDraftPersistence(firstCache);
    await first.save('session-a', 'from tab A', async () => ({ ok: false }));
    secondCache.writeDirty('session-a', 'from tab B', null);
    const writes: SessionDraftWrite[] = [];

    await first.retryAll(async (_sessionId, draft) => {
      writes.push(draft);
      return { ok: true, server: { text: draft.text, updatedAt: 'rev-1' } };
    });

    expect(writes).toEqual([{ text: 'from tab A', expectedUpdatedAt: null }]);
    expect(secondCache.read('session-a')).toMatchObject({
      text: 'from tab B',
      dirty: true,
      mutationId: 'tab-b',
    });
  });

  it('does not delete an adopted mutation when this tab starts editing', () => {
    const storage = new MemoryStorage();
    const firstCache = new SessionDraftLocalCache(storage, () => 'tab-a', () => 1);
    const secondCache = new SessionDraftLocalCache(storage, () => 'tab-b', () => 2);
    secondCache.writeDirty('session-a', 'from tab B', null);
    const first = new SessionDraftPersistence(firstCache);

    first.beginRead('session-a');
    first.cache('session-a', 'from tab A');

    expect(storage.getItem(
      `${SESSION_DRAFT_MUTATION_PREFIX}session-a:tab-b`,
    )).not.toBeNull();
    expect(storage.getItem(
      `${SESSION_DRAFT_MUTATION_PREFIX}session-a:tab-a`,
    )).not.toBeNull();
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

  it('keeps the saved revision when a newer read settles before an older read', async () => {
    const persistence = new SessionDraftPersistence(localCache());
    const olderRead = persistence.beginRead('session-a');
    await persistence.save('session-a', 'newer', async () => ({
      ok: true,
      server: { text: 'newer', updatedAt: 'rev-2' },
    }));
    const newerRead = persistence.beginRead('session-a');

    expect(persistence.reconcileRead(
      'session-a',
      newerRead,
      { text: 'newer', updatedAt: 'rev-2' },
    )).toBe('newer');
    expect(persistence.revision('session-a')).toBe(1);
    expect(persistence.reconcileRead(
      'session-a',
      olderRead,
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

  it('retries every dirty session discovered after a reload', async () => {
    const storage = new MemoryStorage();
    const cache = localCache(storage);
    cache.writeDirty('session-a', 'offline A', 'rev-a');
    cache.writeDirty('session-b', 'offline B', 'rev-b');
    cache.writeClean('session-c', 'synced C', 'rev-c');
    const persistence = new SessionDraftPersistence(cache);
    const calls: Array<{ sessionId: string; draft: SessionDraftWrite }> = [];

    await persistence.retryAll(async (sessionId, draft) => {
      calls.push({ sessionId, draft });
      return {
        ok: true,
        server: { text: draft.text, updatedAt: `synced-${sessionId}` },
      };
    });

    expect(calls).toEqual([
      {
        sessionId: 'session-a',
        draft: { text: 'offline A', expectedUpdatedAt: 'rev-a' },
      },
      {
        sessionId: 'session-b',
        draft: { text: 'offline B', expectedUpdatedAt: 'rev-b' },
      },
    ]);
    expect(cache.read('session-a')?.dirty).toBe(false);
    expect(cache.read('session-b')?.dirty).toBe(false);
    expect(cache.read('session-c')).toMatchObject({ text: 'synced C', dirty: false });
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

  it('preserves a conflicted draft and resumes syncing after the user edits it', async () => {
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

    persistence.cache('session-a', 'local edit resolved');
    await expect(persistence.save('session-a', 'local edit resolved', write)).resolves.toMatchObject({
      ok: true,
    });
    expect(write).toHaveBeenCalledWith({
      text: 'local edit resolved',
      expectedUpdatedAt: 'rev-2',
    });
  });

  it('rebases the next edit onto the server revision returned by a write conflict', async () => {
    const persistence = new SessionDraftPersistence(localCache());
    const conflict = vi.fn(async (): Promise<SessionDraftSaveResult> => ({
      ok: false,
      conflict: true,
      server: { text: 'other device', updatedAt: 'rev-2' },
    }));

    await expect(persistence.save('session-a', 'local edit', conflict)).resolves.toMatchObject({
      ok: false,
      conflict: true,
    });
    await persistence.save('session-a', 'local edit', conflict);
    expect(conflict).toHaveBeenCalledTimes(1);

    const resolved = vi.fn(async (draft: SessionDraftWrite): Promise<SessionDraftSaveResult> => ({
      ok: true,
      server: { text: draft.text, updatedAt: 'rev-3' },
    }));
    persistence.cache('session-a', 'local edit resolved');
    await persistence.save('session-a', 'local edit resolved', resolved);
    expect(resolved).toHaveBeenCalledWith({
      text: 'local edit resolved',
      expectedUpdatedAt: 'rev-2',
    });
  });

  it('rebases a newer edit made before the previous write conflict returns', async () => {
    const persistence = new SessionDraftPersistence(localCache());
    const first = deferred<SessionDraftSaveResult>();
    const firstSave = persistence.save('session-a', 'before send', async () => first.promise);
    await Promise.resolve();

    persistence.cache('session-a', 'typed after send');
    const successor = vi.fn(async (draft: SessionDraftWrite): Promise<SessionDraftSaveResult> => ({
      ok: true,
      server: { text: draft.text, updatedAt: 'rev-3' },
    }));
    const secondSave = persistence.save('session-a', 'typed after send', successor);
    first.resolve({
      ok: false,
      conflict: true,
      server: { text: '', updatedAt: 'clear-revision' },
    });

    await expect(firstSave).resolves.toMatchObject({ ok: false, conflict: true });
    await expect(secondSave).resolves.toMatchObject({ ok: true });
    expect(successor).toHaveBeenCalledWith({
      text: 'typed after send',
      expectedUpdatedAt: 'clear-revision',
    });
  });

  it('rebases a successor from the authoritative state after an uncertain write', async () => {
    const persistence = new SessionDraftPersistence(localCache());
    const first = deferred<SessionDraftSaveResult>();
    const firstSave = persistence.save('session-a', 'first', async () => first.promise);
    await Promise.resolve();

    persistence.cache('session-a', 'second');
    const successor = vi.fn(async (draft: SessionDraftWrite): Promise<SessionDraftSaveResult> => ({
      ok: true,
      server: { text: draft.text, updatedAt: 'rev-2' },
    }));
    const secondSave = persistence.save('session-a', 'second', successor);
    first.resolve({
      ok: false,
      server: { text: '', updatedAt: 'reconciled-revision' },
    });

    await expect(firstSave).resolves.toMatchObject({ ok: false });
    await expect(secondSave).resolves.toMatchObject({ ok: true });
    expect(successor).toHaveBeenCalledWith({
      text: 'second',
      expectedUpdatedAt: 'reconciled-revision',
    });
  });

  it('rebases and retries a rejected-send restoration without another edit', async () => {
    const persistence = new SessionDraftPersistence(localCache());
    persistence.cache('session-a', 'submitted');
    persistence.cache('session-a', '');
    persistence.cache('session-a', 'submitted');

    persistence.rebase('session-a', { text: '', updatedAt: 'clear-revision' });
    const write = vi.fn(async (draft: SessionDraftWrite): Promise<SessionDraftSaveResult> => ({
      ok: true,
      server: { text: draft.text, updatedAt: 'restored-revision' },
    }));
    await persistence.retry('session-a', write);

    expect(write).toHaveBeenCalledWith({
      text: 'submitted',
      expectedUpdatedAt: 'clear-revision',
    });
  });

  it('persists rejected-send recovery and retries its first conflict after reload', async () => {
    const storage = new MemoryStorage();
    const first = new SessionDraftPersistence(localCache(storage));
    first.cache('session-a', 'submitted');
    first.markRejectedSend('session-a');

    const restored = new SessionDraftPersistence(localCache(storage));
    const writes: SessionDraftWrite[] = [];
    const result = await restored.retry('session-a', async (draft) => {
      writes.push(draft);
      if (writes.length === 1) {
        return {
          ok: false,
          conflict: true,
          server: { text: '', updatedAt: 'clear-revision' },
        };
      }
      return {
        ok: true,
        server: { text: draft.text, updatedAt: 'restored-revision' },
      };
    });

    expect(result).toMatchObject({ ok: true });
    expect(writes).toEqual([
      { text: 'submitted', expectedUpdatedAt: null },
      { text: 'submitted', expectedUpdatedAt: 'clear-revision' },
    ]);
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

  it('discards a read response captured before the session was archived', () => {
    const cache = localCache();
    const persistence = new SessionDraftPersistence(cache);
    const read = persistence.beginRead('session-a');

    persistence.clearSession('session-a');
    expect(persistence.reconcileRead(
      'session-a',
      read,
      { text: 'stale pre-archive draft', updatedAt: 'old-revision' },
    )).toBe('');
    expect(cache.read('session-a')).toBeNull();
  });

  it('discards a pre-archive read when another tab invalidates the session', () => {
    const storage = new MemoryStorage();
    const first = new SessionDraftPersistence(new SessionDraftLocalCache(
      storage,
      () => 'tab-a',
      () => 1,
    ));
    const second = new SessionDraftPersistence(new SessionDraftLocalCache(
      storage,
      () => 'tab-b-archive',
      () => 2,
    ));
    const read = first.beginRead('session-a');

    second.clearSession('session-a');
    expect(first.reconcileRead(
      'session-a',
      read,
      { text: 'stale pre-archive draft', updatedAt: 'old-revision' },
    )).toBe('');
    expect(first.peek('session-a')).toBeNull();
  });
});
