import { describe, expect, it } from 'vitest';
import {
  SESSION_DRAFT_ATTACHMENTS_STORAGE_PREFIX,
  SessionDraftAttachmentsCache,
  type SessionDraftAttachment,
} from './sessionDraftAttachmentsCache';

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

class FlakyStorage extends MemoryStorage {
  failWrites = true;

  override setItem(key: string, value: string): void {
    if (this.failWrites) throw new Error('full');
    super.setItem(key, value);
  }
}

const attachment: SessionDraftAttachment = {
  token: 'media-token',
  name: 'image.png',
  mime: 'image/png',
  size: 42,
  kind: 'image',
  url: '/api/media/media-token',
  width: 640,
  height: 480,
};

describe('SessionDraftAttachmentsCache', () => {
  it('persists only attachment metadata and restores it across cache instances', () => {
    const storage = new MemoryStorage();
    const first = new SessionDraftAttachmentsCache(storage);

    first.write('session-a', [attachment]);

    const stored = storage.getItem(`${SESSION_DRAFT_ATTACHMENTS_STORAGE_PREFIX}session-a`);
    expect(stored).toBe(JSON.stringify({ version: 1, generation: 1, attachments: [attachment] }));
    expect(new SessionDraftAttachmentsCache(storage).read('session-a')).toEqual([attachment]);
  });

  it('keeps a same-tab restore when storage is blocked', () => {
    const blocked = {
      getItem: () => { throw new Error('blocked'); },
      setItem: () => { throw new Error('blocked'); },
      removeItem: () => { throw new Error('blocked'); },
    };
    const cache = new SessionDraftAttachmentsCache(blocked);

    cache.write('session-a', [attachment]);

    expect(cache.read('session-a')).toEqual([attachment]);
    expect(() => cache.clear('session-a')).not.toThrow();
    expect(cache.read('session-a')).toEqual([]);
  });

  it('clears the per-session entry without affecting another session', () => {
    const cache = new SessionDraftAttachmentsCache(new MemoryStorage());
    cache.write('session-a', [attachment]);
    cache.write('session-b', [{ ...attachment, token: 'other-token' }]);

    cache.clear('session-a');

    expect(cache.read('session-a')).toEqual([]);
    expect(cache.read('session-b')).toEqual([{ ...attachment, token: 'other-token' }]);
  });

  it('reconciles a cached memory entry with storage changes from another tab', () => {
    const storage = new MemoryStorage();
    const cache = new SessionDraftAttachmentsCache(storage);
    cache.write('session-a', [attachment]);
    expect(cache.read('session-a')).toEqual([attachment]);

    storage.removeItem(`${SESSION_DRAFT_ATTACHMENTS_STORAGE_PREFIX}session-a`);

    expect(cache.read('session-a')).toEqual([]);
  });

  it('keeps memory authoritative after a storage write fails until a later write succeeds', () => {
    const storage = new FlakyStorage();
    const cache = new SessionDraftAttachmentsCache(storage);

    cache.write('session-a', [attachment]);
    expect(cache.read('session-a')).toEqual([attachment]);

    storage.failWrites = false;
    cache.write('session-a', [attachment]);
    expect(cache.read('session-a')).toEqual([attachment]);
  });

  it('only recovers a rejected send when its generation is still current', () => {
    const cache = new SessionDraftAttachmentsCache(new MemoryStorage());
    const sendGeneration = cache.clear('session-a');
    expect(cache.write('session-a', [])).toBe(sendGeneration);
    expect(cache.writeIfGeneration('session-a', sendGeneration, [attachment])).toBe(true);

    const newer = { ...attachment, token: 'newer-token', url: '/api/media/newer-token' };
    const newerGeneration = cache.write('session-a', [newer]);
    expect(cache.writeIfGeneration('session-a', sendGeneration, [attachment])).toBe(false);
    expect(cache.read('session-a')).toEqual([newer]);
    expect(cache.writeIfGeneration('session-a', newerGeneration, [attachment])).toBe(true);
    expect(cache.read('session-a')).toEqual([attachment]);
  });

  it('does not change generation for an identical empty write after clear', () => {
    const cache = new SessionDraftAttachmentsCache(new MemoryStorage());
    const generation = cache.clear('session-a');

    expect(cache.write('session-a', [])).toBe(generation);
    expect(cache.clear('session-a')).toBe(generation + 1);
  });
});
