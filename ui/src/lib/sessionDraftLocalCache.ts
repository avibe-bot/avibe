export type SessionDraftCacheRecord = {
  version: 1;
  text: string;
  serverUpdatedAt: string | null;
  dirty: boolean;
  mutationId: string;
  savedAt: number;
};

type DraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
  & Partial<Pick<Storage, 'key' | 'length'>>;
type CreateId = () => string;
type Now = () => number;

export const SESSION_DRAFT_STORAGE_PREFIX = 'avibe.session-draft.v1.';

function browserStorage(storage?: DraftStorage): DraftStorage | undefined {
  try {
    return storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined);
  } catch {
    return undefined;
  }
}

function defaultId(): string {
  return globalThis.crypto?.randomUUID?.()
    ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function parseRecord(raw: string | null): SessionDraftCacheRecord | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<SessionDraftCacheRecord>;
    if (
      value.version !== 1
      || typeof value.text !== 'string'
      || (value.serverUpdatedAt !== null && typeof value.serverUpdatedAt !== 'string')
      || typeof value.dirty !== 'boolean'
      || typeof value.mutationId !== 'string'
      || !value.mutationId
      || typeof value.savedAt !== 'number'
      || !Number.isFinite(value.savedAt)
    ) {
      return null;
    }
    return value as SessionDraftCacheRecord;
  } catch {
    return null;
  }
}

export class SessionDraftLocalCache {
  private readonly storage?: DraftStorage;
  private readonly createId: CreateId;
  private readonly now: Now;

  constructor(
    storage?: DraftStorage,
    createId: CreateId = defaultId,
    now: Now = Date.now,
  ) {
    this.storage = storage;
    this.createId = createId;
    this.now = now;
  }

  read(sessionId: string): SessionDraftCacheRecord | null {
    const target = browserStorage(this.storage);
    if (!target) return null;
    const key = this.key(sessionId);
    try {
      const raw = target.getItem(key);
      const record = parseRecord(raw);
      if (raw && !record) target.removeItem(key);
      return record;
    } catch {
      return null;
    }
  }

  dirtySessionIds(): string[] {
    const target = browserStorage(this.storage);
    if (!target || typeof target.length !== 'number' || typeof target.key !== 'function') return [];
    const sessionIds = new Set<string>();
    try {
      const keys = Array.from(
        { length: target.length },
        (_, index) => target.key!(index),
      );
      for (const key of keys) {
        if (!key?.startsWith(SESSION_DRAFT_STORAGE_PREFIX)) continue;
        const raw = target.getItem(key);
        const record = parseRecord(raw);
        if (raw && !record) {
          target.removeItem(key);
          continue;
        }
        if (!record?.dirty) continue;
        try {
          sessionIds.add(decodeURIComponent(key.slice(SESSION_DRAFT_STORAGE_PREFIX.length)));
        } catch {
          // Ignore malformed keys that were not written by this cache.
        }
      }
    } catch {
      return [];
    }
    return [...sessionIds];
  }

  writeDirty(sessionId: string, text: string, serverUpdatedAt: string | null): SessionDraftCacheRecord {
    return this.write(sessionId, {
      version: 1,
      text,
      serverUpdatedAt,
      dirty: true,
      mutationId: this.createId(),
      savedAt: this.now(),
    });
  }

  writeClean(sessionId: string, text: string, serverUpdatedAt: string | null): SessionDraftCacheRecord | null {
    if (!text) {
      this.clear(sessionId);
      return null;
    }
    return this.write(sessionId, {
      version: 1,
      text,
      serverUpdatedAt,
      dirty: false,
      mutationId: this.createId(),
      savedAt: this.now(),
    });
  }

  rebaseDirty(sessionId: string, mutationId: string, serverUpdatedAt: string | null): void {
    const current = this.read(sessionId);
    if (!current || current.mutationId !== mutationId || !current.dirty) return;
    this.write(sessionId, { ...current, serverUpdatedAt });
  }

  acknowledge(
    sessionId: string,
    mutationId: string,
    text: string,
    serverUpdatedAt: string | null,
  ): void {
    const current = this.read(sessionId);
    if (!current || current.mutationId !== mutationId) return;
    if (!text) {
      this.clear(sessionId);
      return;
    }
    this.write(sessionId, {
      ...current,
      text,
      serverUpdatedAt,
      dirty: false,
      savedAt: this.now(),
    });
  }

  clear(sessionId: string): void {
    try {
      browserStorage(this.storage)?.removeItem(this.key(sessionId));
    } catch {
      // Draft caching is best-effort when storage is blocked or full.
    }
  }

  private write(sessionId: string, record: SessionDraftCacheRecord): SessionDraftCacheRecord {
    try {
      browserStorage(this.storage)?.setItem(this.key(sessionId), JSON.stringify(record));
    } catch {
      // The in-memory persistence layer still protects this tab when storage is unavailable.
    }
    return record;
  }

  private key(sessionId: string): string {
    return `${SESSION_DRAFT_STORAGE_PREFIX}${encodeURIComponent(sessionId)}`;
  }
}
