export type SessionDraftCacheRecord = {
  version: 1;
  text: string;
  serverUpdatedAt: string | null;
  dirty: boolean;
  mutationId: string;
  savedAt: number;
  rebaseOnConflict?: boolean;
  rebaseOnConflictServerText?: string;
};

type DraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
  & Partial<Pick<Storage, 'key' | 'length'>>;
type CreateId = () => string;
type Now = () => number;

export const SESSION_DRAFT_STORAGE_PREFIX = 'avibe.session-draft.v1.';
export const SESSION_DRAFT_MUTATION_PREFIX = 'avibe.session-draft-mutation.v1.';
export const SESSION_DRAFT_INVALIDATION_PREFIX = 'avibe.session-draft-invalidation.v1.';

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
      || (value.rebaseOnConflict !== undefined && typeof value.rebaseOnConflict !== 'boolean')
      || (
        value.rebaseOnConflictServerText !== undefined
        && (
          typeof value.rebaseOnConflictServerText !== 'string'
          || value.rebaseOnConflict !== true
        )
      )
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
  private readonly memoryInvalidations = new Map<string, { token: string; persisted: boolean }>();

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
    try {
      const dirty = this.readDirtyMutations(target, sessionId);
      const stored = this.readRecord(target, this.key(sessionId));
      if (stored?.dirty) dirty.push(stored); // Legacy single-record cache.
      if (dirty.length) {
        return dirty.reduce((latest, record) => (
          this.compareMutationOrder(record, latest) > 0
            ? record
            : latest
        ));
      }
      return stored;
    } catch {
      return null;
    }
  }

  readInvalidation(sessionId: string): string | null {
    const memory = this.memoryInvalidations.get(sessionId);
    // A failed write leaves an older token readable in storage. The newer
    // process-local token must win until it can be persisted.
    if (memory && !memory.persisted) return memory.token;
    const target = browserStorage(this.storage);
    if (!target) return memory?.token ?? null;
    try {
      const stored = target.getItem(this.invalidationKey(sessionId));
      return stored || memory?.token || null;
    } catch {
      return memory?.token ?? null;
    }
  }

  invalidate(sessionId: string): string {
    const token = this.createId();
    let persisted = false;
    try {
      const target = browserStorage(this.storage);
      target?.setItem(this.invalidationKey(sessionId), token);
      persisted = Boolean(target);
    } catch {
      // Recorded below so this token remains newer than stale stored state.
    }
    this.memoryInvalidations.set(sessionId, { token, persisted });
    return token;
  }

  dirtySessionIds(): string[] {
    const target = browserStorage(this.storage);
    if (!target || typeof target.length !== 'number' || typeof target.key !== 'function') return [];
    const sessionIds = new Set<string>();
    try {
      const keys = this.keys(target);
      for (const key of keys) {
        if (key.startsWith(SESSION_DRAFT_MUTATION_PREFIX)) {
          const separator = key.indexOf(':', SESSION_DRAFT_MUTATION_PREFIX.length);
          if (separator < 0) continue;
          const record = this.readRecord(target, key);
          if (!record?.dirty) continue;
          try {
            sessionIds.add(decodeURIComponent(key.slice(
              SESSION_DRAFT_MUTATION_PREFIX.length,
              separator,
            )));
          } catch {
            // Ignore malformed keys that were not written by this cache.
          }
          continue;
        }
        if (!key.startsWith(SESSION_DRAFT_STORAGE_PREFIX)) continue;
        const record = this.readRecord(target, key);
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

  writeDirty(
    sessionId: string,
    text: string,
    serverUpdatedAt: string | null,
    previousMutationId?: string,
    rebaseOnConflict = false,
    rebaseOnConflictServerText?: string,
  ): SessionDraftCacheRecord {
    const record: SessionDraftCacheRecord = {
      version: 1,
      text,
      serverUpdatedAt,
      dirty: true,
      mutationId: this.createId(),
      savedAt: this.now(),
      ...(rebaseOnConflict ? { rebaseOnConflict: true } : {}),
      ...(rebaseOnConflict && rebaseOnConflictServerText !== undefined
        ? { rebaseOnConflictServerText }
        : {}),
    };
    try {
      const target = browserStorage(this.storage);
      target?.setItem(this.mutationKey(sessionId, record.mutationId), JSON.stringify(record));
      if (target && previousMutationId) {
        target.removeItem(this.mutationKey(sessionId, previousMutationId));
      }
    } catch {
      // The in-memory persistence layer still protects this tab when storage is unavailable.
    }
    return record;
  }

  writeClean(sessionId: string, text: string, serverUpdatedAt: string | null): SessionDraftCacheRecord | null {
    if (!text) {
      try {
        browserStorage(this.storage)?.removeItem(this.key(sessionId));
      } catch {
        // Concurrent dirty mutations use separate keys and remain recoverable.
      }
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

  rebaseDirty(
    sessionId: string,
    mutationId: string,
    serverUpdatedAt: string | null,
    rebaseOnConflict = false,
    rebaseOnConflictServerText?: string,
  ): void {
    const target = browserStorage(this.storage);
    if (!target) return;
    try {
      const mutationKey = this.mutationKey(sessionId, mutationId);
      const mutation = this.readRecord(target, mutationKey);
      if (mutation?.dirty && mutation.mutationId === mutationId) {
        target.setItem(mutationKey, JSON.stringify({
          ...mutation,
          serverUpdatedAt,
          rebaseOnConflict: rebaseOnConflict || undefined,
          rebaseOnConflictServerText: rebaseOnConflict
            ? rebaseOnConflictServerText
            : undefined,
        }));
        return;
      }
      // Compatibility with dirty records created before mutations used
      // independent keys.
      const legacy = this.readRecord(target, this.key(sessionId));
      if (!legacy?.dirty || legacy.mutationId !== mutationId) return;
      target.setItem(this.key(sessionId), JSON.stringify({
        ...legacy,
        serverUpdatedAt,
        rebaseOnConflict: rebaseOnConflict || undefined,
        rebaseOnConflictServerText: rebaseOnConflict
          ? rebaseOnConflictServerText
          : undefined,
      }));
    } catch {
      // Best-effort cache metadata; the live in-memory entry remains authoritative.
    }
  }

  markRebaseOnConflict(
    sessionId: string,
    mutationId: string,
    serverText?: string,
  ): void {
    const target = browserStorage(this.storage);
    if (!target) return;
    try {
      const mutationKey = this.mutationKey(sessionId, mutationId);
      const mutation = this.readRecord(target, mutationKey);
      if (mutation?.dirty && mutation.mutationId === mutationId) {
        target.setItem(mutationKey, JSON.stringify({
          ...mutation,
          rebaseOnConflict: true,
          rebaseOnConflictServerText: serverText,
        }));
        return;
      }
      const legacy = this.readRecord(target, this.key(sessionId));
      if (!legacy?.dirty || legacy.mutationId !== mutationId) return;
      target.setItem(this.key(sessionId), JSON.stringify({
        ...legacy,
        rebaseOnConflict: true,
        rebaseOnConflictServerText: serverText,
      }));
    } catch {
      // The live entry still carries the recovery marker in this tab.
    }
  }

  acknowledge(
    sessionId: string,
    mutationId: string,
    text: string,
    serverUpdatedAt: string | null,
  ): void {
    const target = browserStorage(this.storage);
    if (!target) return;
    try {
      const mutationKey = this.mutationKey(sessionId, mutationId);
      const mutation = this.readRecord(target, mutationKey);
      if (mutation?.dirty && mutation.mutationId === mutationId) {
        // Dirty mutations use independent keys, so acknowledging this tab can
        // never replace another tab's concurrently-written text.
        // A reload replays the newest stored mutation. Once that winner reaches
        // the cloud, every mutation at or before its ordering frontier is stale;
        // otherwise the next reload would reveal and replay an older hidden key.
        // Retire older keys first so a partial storage failure leaves the winner
        // available for an idempotent retry instead of exposing an older draft.
        this.retireMutationsBefore(target, sessionId, mutation);
        target.removeItem(mutationKey);
        if (!text) {
          target.removeItem(this.key(sessionId));
          return;
        }
        this.write(sessionId, {
          ...mutation,
          text,
          serverUpdatedAt,
          dirty: false,
          savedAt: this.now(),
          rebaseOnConflict: undefined,
          rebaseOnConflictServerText: undefined,
        });
        return;
      }

      // Compatibility with the former single-record cache. New mutations never
      // use this path, so current tabs retain atomic cross-tab ownership.
      const legacy = this.readRecord(target, this.key(sessionId));
      if (!legacy || legacy.mutationId !== mutationId) return;
      if (!text) {
        target.removeItem(this.key(sessionId));
        return;
      }
      this.write(sessionId, {
        ...legacy,
        text,
        serverUpdatedAt,
        dirty: false,
        savedAt: this.now(),
        rebaseOnConflict: undefined,
        rebaseOnConflictServerText: undefined,
      });
    } catch {
      // The cloud acknowledgement succeeded; a blocked cache is non-fatal.
    }
  }

  clear(sessionId: string): void {
    try {
      const target = browserStorage(this.storage);
      if (!target) return;
      target.removeItem(this.key(sessionId));
      for (const key of this.keys(target)) {
        if (key.startsWith(this.mutationPrefix(sessionId))) target.removeItem(key);
      }
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

  private mutationPrefix(sessionId: string): string {
    return `${SESSION_DRAFT_MUTATION_PREFIX}${encodeURIComponent(sessionId)}:`;
  }

  private mutationKey(sessionId: string, mutationId: string): string {
    return `${this.mutationPrefix(sessionId)}${encodeURIComponent(mutationId)}`;
  }

  private invalidationKey(sessionId: string): string {
    return `${SESSION_DRAFT_INVALIDATION_PREFIX}${encodeURIComponent(sessionId)}`;
  }

  private keys(target: DraftStorage): string[] {
    if (typeof target.length !== 'number' || typeof target.key !== 'function') return [];
    return Array.from({ length: target.length }, (_, index) => target.key!(index))
      .filter((key): key is string => Boolean(key));
  }

  private readRecord(target: DraftStorage, key: string): SessionDraftCacheRecord | null {
    const raw = target.getItem(key);
    const record = parseRecord(raw);
    if (raw && !record) target.removeItem(key);
    return record;
  }

  private readDirtyMutations(
    target: DraftStorage,
    sessionId: string,
  ): SessionDraftCacheRecord[] {
    const prefix = this.mutationPrefix(sessionId);
    return this.keys(target)
      .filter((key) => key.startsWith(prefix))
      .map((key) => this.readRecord(target, key))
      .filter((record): record is SessionDraftCacheRecord => Boolean(record?.dirty));
  }

  private compareMutationOrder(
    left: SessionDraftCacheRecord,
    right: SessionDraftCacheRecord,
  ): number {
    if (left.savedAt !== right.savedAt) return left.savedAt - right.savedAt;
    if (left.mutationId === right.mutationId) return 0;
    return left.mutationId > right.mutationId ? 1 : -1;
  }

  private retireMutationsBefore(
    target: DraftStorage,
    sessionId: string,
    acknowledged: SessionDraftCacheRecord,
  ): void {
    const prefix = this.mutationPrefix(sessionId);
    for (const key of this.keys(target)) {
      if (!key.startsWith(prefix)) continue;
      const record = this.readRecord(target, key);
      if (
        record?.dirty
        && this.compareMutationOrder(record, acknowledged) < 0
      ) {
        target.removeItem(key);
      }
    }
  }
}
