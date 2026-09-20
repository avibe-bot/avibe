import { SessionDraftLocalCache } from './sessionDraftLocalCache';

export type SessionDraftServerState = {
  text: string;
  updatedAt: string | null;
};

export type SessionDraftWrite = {
  text: string;
  expectedUpdatedAt: string | null;
};

export type SessionDraftSaveResult = {
  ok: boolean;
  conflict?: boolean;
  server?: SessionDraftServerState;
  /** Retry one successor conflict only when the server contains this timed-out
   *  predecessor, never when an unrelated device wrote instead. */
  retryConflictIfServerText?: string;
};

export type SessionDraftRead = {
  revision: number;
  pending: boolean;
  generation: string | null;
};

type DraftEntry = {
  revision: number;
  text: string;
  localId: string;
  baseUpdatedAt: string | null;
  pending: Promise<SessionDraftSaveResult> | null;
  pendingRevision: number | null;
  dirty: boolean;
  conflict: boolean;
  localOwned: boolean;
  rebaseOnConflict: boolean;
  rebaseOnConflictServerText?: string;
  serverEpoch: number;
};

type DraftSave = (draft: SessionDraftWrite) => Promise<SessionDraftSaveResult>;
type SessionDraftSave = (
  sessionId: string,
  draft: SessionDraftWrite,
) => Promise<SessionDraftSaveResult>;

/**
 * Keeps the latest draft available synchronously, serializes cloud writes per
 * session, and reconciles reads against the server revision each local edit was
 * based on. A real version conflict preserves the local text without silently
 * overwriting the other device's newer cloud draft.
 */
export class SessionDraftPersistence {
  private readonly entries = new Map<string, DraftEntry>();
  private readonly activeReads = new Map<string, Set<SessionDraftRead>>();
  private readonly serverVersions = new Map<string, string | null>();
  private readonly invalidations = new Map<string, string | null>();
  private readonly localCache: SessionDraftLocalCache;

  constructor(localCache = new SessionDraftLocalCache()) {
    this.localCache = localCache;
  }

  cache(sessionId: string, text: string): void {
    this.observeInvalidation(sessionId);
    const current = this.entries.get(sessionId);
    const cached = this.localCache.read(sessionId);
    const resolvesConflict = Boolean(current?.conflict && current.text !== text);
    const baseUpdatedAt = resolvesConflict && this.serverVersions.has(sessionId)
      ? this.serverVersions.get(sessionId)!
      : current?.baseUpdatedAt
        ?? cached?.serverUpdatedAt
        ?? this.serverVersions.get(sessionId)
        ?? null;
    const local = this.localCache.writeDirty(
      sessionId,
      text,
      baseUpdatedAt,
      current?.localOwned ? current.localId : undefined,
      current?.rebaseOnConflict ?? false,
      current?.rebaseOnConflictServerText,
    );
    this.entries.set(sessionId, {
      revision: (current?.revision ?? 0) + 1,
      text,
      localId: local.mutationId,
      baseUpdatedAt,
      pending: current?.pending ?? null,
      pendingRevision: current?.pendingRevision ?? null,
      dirty: true,
      // A new keystroke after a conflict is explicit user intent. Rebase that
      // edit onto the revision returned by the conflict and resume CAS writes.
      conflict: resolvesConflict ? false : current?.conflict ?? false,
      localOwned: true,
      rebaseOnConflict: current?.rebaseOnConflict ?? false,
      rebaseOnConflictServerText: current?.rebaseOnConflictServerText,
      serverEpoch: current?.serverEpoch ?? 0,
    });
  }

  peek(sessionId: string): string | null {
    this.observeInvalidation(sessionId);
    const current = this.entries.get(sessionId);
    if (current?.dirty) return current.text;
    return this.localCache.read(sessionId)?.text ?? null;
  }

  save(sessionId: string, text: string, write: DraftSave): Promise<SessionDraftSaveResult> {
    this.observeInvalidation(sessionId);
    let current = this.entries.get(sessionId);
    if (!current || current.text !== text || !current.dirty) {
      this.cache(sessionId, text);
      current = this.entries.get(sessionId)!;
    }
    return this.startSync(sessionId, current, write);
  }

  retry(sessionId: string, write: DraftSave): Promise<SessionDraftSaveResult> {
    const current = this.hydrateDirty(sessionId);
    if (!current) return Promise.resolve({ ok: true });
    return this.startSync(sessionId, current, write);
  }

  retryAll(write: SessionDraftSave): Promise<SessionDraftSaveResult[]> {
    const sessionIds = new Set(this.localCache.dirtySessionIds());
    for (const [sessionId, entry] of this.entries) {
      if (entry.dirty) sessionIds.add(sessionId);
    }
    return Promise.all(
      [...sessionIds].map((sessionId) => this.retry(
        sessionId,
        (draft) => write(sessionId, draft),
      )),
    );
  }

  beginRead(sessionId: string): SessionDraftRead {
    const current = this.hydrateDirty(sessionId);
    const read: SessionDraftRead = {
      revision: current?.revision ?? 0,
      pending: Boolean(current?.pending),
      generation: this.localCache.readInvalidation(sessionId),
    };
    const reads = this.activeReads.get(sessionId) ?? new Set<SessionDraftRead>();
    reads.add(read);
    this.activeReads.set(sessionId, reads);
    return read;
  }

  releaseRead(sessionId: string, read: SessionDraftRead): void {
    this.finishRead(sessionId, read);
    this.cleanupSuccessfulEntry(sessionId);
  }

  revision(sessionId: string): number {
    return this.entries.get(sessionId)?.revision ?? 0;
  }

  reconcileRead(
    sessionId: string,
    read: SessionDraftRead,
    server: SessionDraftServerState,
  ): string {
    this.finishRead(sessionId, read);
    const invalidation = this.localCache.readInvalidation(sessionId);
    if (read.generation !== invalidation) {
      this.discardInvalidatedSession(sessionId, invalidation);
      return '';
    }
    this.invalidations.set(sessionId, invalidation);
    const current = this.entries.get(sessionId);

    // A write that finished after this read started is newer than the read's
    // response even though both have server revisions. Preserve that result
    // until every racing read has reconciled.
    if (current && (read.pending || current.revision > read.revision || current.pending)) {
      this.cleanupSuccessfulEntry(sessionId);
      return current.text;
    }

    const cached = this.localCache.read(sessionId);
    const dirty = current?.dirty ? current : cached?.dirty ? this.hydrateDirty(sessionId) : null;
    if (dirty) {
      if (dirty.text === server.text) {
        this.acceptServer(sessionId, server, dirty.localId);
        return server.text;
      }

      this.serverVersions.set(sessionId, server.updatedAt);
      if (dirty.baseUpdatedAt !== server.updatedAt) {
        dirty.conflict = true;
      }
      return dirty.text;
    }

    if (current && this.hasOlderRead(sessionId, current.revision)) {
      // A read that began after this successful write may settle before a read
      // from the older revision. Keep one retained value as a read barrier so
      // every older response returns the newest value instead of deleting the
      // only stale-response guard.
      this.localCache.acknowledge(
        sessionId,
        current.localId,
        server.text,
        server.updatedAt,
      );
      current.text = server.text;
      current.baseUpdatedAt = server.updatedAt;
      this.serverVersions.set(sessionId, server.updatedAt);
      return current.text;
    }

    this.acceptServer(sessionId, server);
    this.cleanupSuccessfulEntry(sessionId);
    return server.text;
  }

  clearSession(sessionId: string): void {
    const invalidation = this.localCache.invalidate(sessionId);
    this.invalidations.set(sessionId, invalidation);
    this.entries.delete(sessionId);
    this.activeReads.delete(sessionId);
    this.serverVersions.delete(sessionId);
    this.localCache.clear(sessionId);
  }

  rebase(sessionId: string, server: SessionDraftServerState): void {
    this.serverVersions.set(sessionId, server.updatedAt);
    const current = this.hydrateDirty(sessionId);
    if (!current) return;
    current.baseUpdatedAt = server.updatedAt;
    current.conflict = false;
    current.rebaseOnConflict = false;
    current.rebaseOnConflictServerText = undefined;
    current.serverEpoch += 1;
    // The external revision is causally newer than every write already in
    // flight. Detach that promise chain; its eventual result is ignored by the
    // epoch guard below, while retry() starts from this exact server revision.
    current.pending = null;
    current.pendingRevision = null;
    this.localCache.rebaseDirty(sessionId, current.localId, server.updatedAt, false);
  }

  markRejectedSend(sessionId: string): void {
    const current = this.hydrateDirty(sessionId);
    if (!current) return;
    current.rebaseOnConflict = true;
    current.rebaseOnConflictServerText = undefined;
    this.localCache.markRebaseOnConflict(sessionId, current.localId);
  }

  private startSync(
    sessionId: string,
    entry: DraftEntry,
    write: DraftSave,
  ): Promise<SessionDraftSaveResult> {
    if (entry.conflict) return Promise.resolve({ ok: false, conflict: true });
    if (entry.pending && entry.pendingRevision === entry.revision) return entry.pending;

    const predecessor = entry.pending;
    const revision = entry.revision;
    const serverEpoch = entry.serverEpoch;
    const text = entry.text;
    const pending = (async (): Promise<SessionDraftSaveResult> => {
      const predecessorResult: SessionDraftSaveResult | undefined = await predecessor?.catch(
        (): SessionDraftSaveResult => ({ ok: false }),
      );
      const beforeWrite = this.entries.get(sessionId);
      if (!beforeWrite || beforeWrite.revision !== revision) return { ok: true };
      if (beforeWrite.serverEpoch !== serverEpoch) return { ok: false };
      if (predecessorResult?.server) {
        beforeWrite.baseUpdatedAt = predecessorResult.server.updatedAt;
        beforeWrite.conflict = false;
        beforeWrite.rebaseOnConflict = (
          predecessorResult.retryConflictIfServerText !== undefined
        );
        beforeWrite.rebaseOnConflictServerText = (
          predecessorResult.retryConflictIfServerText
        );
        this.localCache.rebaseDirty(
          sessionId,
          beforeWrite.localId,
          predecessorResult.server.updatedAt,
          beforeWrite.rebaseOnConflict,
          beforeWrite.rebaseOnConflictServerText,
        );
      }
      if (predecessorResult?.conflict && !predecessorResult.server) {
        beforeWrite.pending = null;
        beforeWrite.pendingRevision = null;
        beforeWrite.conflict = true;
        return { ok: false, conflict: true };
      }
      if (beforeWrite.conflict) {
        beforeWrite.pending = null;
        beforeWrite.pendingRevision = null;
        beforeWrite.conflict = true;
        return { ok: false, conflict: true };
      }

      let result: SessionDraftSaveResult;
      try {
        result = await write({ text, expectedUpdatedAt: beforeWrite.baseUpdatedAt });
      } catch {
        result = { ok: false };
      }

      // The cloud may already contain this exact text because another write
      // (notably the send transaction clearing a draft) won the race. That is
      // convergence, not a user-visible conflict.
      if (result.conflict && result.server?.text === text) {
        result = { ok: true, server: result.server };
      }
      const shouldRetryRecoveredConflict = Boolean(
        result.conflict
        && result.server
        && beforeWrite.revision === revision
        && beforeWrite.serverEpoch === serverEpoch
        && this.matchesConflictRecovery(beforeWrite, result)
      );
      this.applyWriteResult(sessionId, revision, serverEpoch, result);
      if (shouldRetryRecoveredConflict) {
        const retry = this.entries.get(sessionId);
        if (retry?.dirty && !retry.conflict && !retry.pending) {
          return this.startSync(sessionId, retry, write);
        }
      }
      return result;
    })();

    entry.pending = pending;
    entry.pendingRevision = revision;
    return pending;
  }

  private applyWriteResult(
    sessionId: string,
    revision: number,
    serverEpoch: number,
    result: SessionDraftSaveResult,
  ): void {
    const current = this.entries.get(sessionId);
    if (!current || current.serverEpoch !== serverEpoch) return;

    if (result.ok) {
      const server = result.server ?? {
        text: current.text,
        updatedAt: current.baseUpdatedAt,
      };
      this.serverVersions.set(sessionId, server.updatedAt);
      if (current.revision === revision) {
        current.pending = null;
        current.pendingRevision = null;
        current.dirty = false;
        current.conflict = false;
        current.rebaseOnConflict = false;
        current.rebaseOnConflictServerText = undefined;
        current.baseUpdatedAt = server.updatedAt;
        this.localCache.acknowledge(
          sessionId,
          current.localId,
          server.text,
          server.updatedAt,
        );
        this.cleanupSuccessfulEntry(sessionId);
      } else if (current.dirty && !current.conflict) {
        // A newer edit was made while this request was in flight. Advance only
        // that exact local mutation's cloud base before its serialized write.
        current.baseUpdatedAt = server.updatedAt;
        this.localCache.rebaseDirty(sessionId, current.localId, server.updatedAt);
      }
      return;
    }

    if (current.revision === revision) {
      current.pending = null;
      current.pendingRevision = null;
    }
    if (result.retryConflictIfServerText !== undefined && result.server) {
      // No successor has to exist yet. Preserve this uncertainty on the current
      // mutation so a later edit or a reload can recognize the timed-out write
      // if it eventually commits and becomes the next conflict response.
      current.baseUpdatedAt = result.server.updatedAt;
      current.rebaseOnConflict = true;
      current.rebaseOnConflictServerText = result.retryConflictIfServerText;
      this.localCache.rebaseDirty(
        sessionId,
        current.localId,
        result.server.updatedAt,
        true,
        result.retryConflictIfServerText,
      );
    }
    if (result.conflict) {
      if (result.server) this.serverVersions.set(sessionId, result.server.updatedAt);
      if (current.revision !== revision && result.server) {
        current.baseUpdatedAt = result.server.updatedAt;
        current.conflict = false;
        current.rebaseOnConflict = false;
        current.rebaseOnConflictServerText = undefined;
        this.localCache.rebaseDirty(sessionId, current.localId, result.server.updatedAt, false);
      } else if (
        this.matchesConflictRecovery(current, result)
        && result.server
      ) {
        current.baseUpdatedAt = result.server.updatedAt;
        current.conflict = false;
        current.rebaseOnConflict = false;
        current.rebaseOnConflictServerText = undefined;
        this.localCache.rebaseDirty(sessionId, current.localId, result.server.updatedAt, false);
      } else {
        current.conflict = true;
        current.rebaseOnConflict = false;
        current.rebaseOnConflictServerText = undefined;
        this.localCache.rebaseDirty(
          sessionId,
          current.localId,
          current.baseUpdatedAt,
          false,
        );
      }
    }
  }

  private matchesConflictRecovery(
    entry: DraftEntry,
    result: SessionDraftSaveResult,
  ): boolean {
    return Boolean(
      entry.rebaseOnConflict
      && result.server
      && (
        entry.rebaseOnConflictServerText === undefined
        || result.server.text === entry.rebaseOnConflictServerText
      )
    );
  }

  private hydrateDirty(sessionId: string): DraftEntry | null {
    this.observeInvalidation(sessionId);
    const current = this.entries.get(sessionId);
    const cached = this.localCache.read(sessionId);
    // A live tab owns its unsynced mutation. Shared localStorage is recovery
    // state for reloads and inactive tabs, not permission for one tab to replace
    // another tab's composer while navigating.
    if (current?.dirty) return current;
    if (current && (!cached || cached.mutationId === current.localId)) return current;
    if (current && cached && !cached.dirty) {
      if (current.pending) return current;
      this.entries.delete(sessionId);
      return null;
    }
    if (!cached?.dirty) return null;
    const hydrated: DraftEntry = {
      revision: (current?.revision ?? 0) + 1,
      text: cached.text,
      localId: cached.mutationId,
      baseUpdatedAt: cached.serverUpdatedAt,
      pending: current?.pending ?? null,
      pendingRevision: current?.pendingRevision ?? null,
      dirty: true,
      conflict: false,
      localOwned: false,
      rebaseOnConflict: cached.rebaseOnConflict ?? false,
      rebaseOnConflictServerText: cached.rebaseOnConflictServerText,
      serverEpoch: current?.serverEpoch ?? 0,
    };
    this.entries.set(sessionId, hydrated);
    return hydrated;
  }

  private acceptServer(
    sessionId: string,
    server: SessionDraftServerState,
    mutationId?: string,
  ): void {
    this.serverVersions.set(sessionId, server.updatedAt);
    this.entries.delete(sessionId);
    if (mutationId) {
      this.localCache.acknowledge(sessionId, mutationId, server.text, server.updatedAt);
    } else {
      this.localCache.writeClean(sessionId, server.text, server.updatedAt);
    }
  }

  private observeInvalidation(sessionId: string): void {
    const invalidation = this.localCache.readInvalidation(sessionId);
    if (!this.invalidations.has(sessionId)) {
      this.invalidations.set(sessionId, invalidation);
      return;
    }
    if (this.invalidations.get(sessionId) === invalidation) return;
    this.discardInvalidatedSession(sessionId, invalidation);
  }

  private discardInvalidatedSession(sessionId: string, invalidation: string | null): void {
    this.invalidations.set(sessionId, invalidation);
    this.entries.delete(sessionId);
    this.activeReads.delete(sessionId);
    this.serverVersions.delete(sessionId);
    this.localCache.clear(sessionId);
  }

  private finishRead(sessionId: string, read: SessionDraftRead): void {
    const reads = this.activeReads.get(sessionId);
    if (!reads) return;
    reads.delete(read);
    if (!reads.size) this.activeReads.delete(sessionId);
  }

  private cleanupSuccessfulEntry(sessionId: string): void {
    const current = this.entries.get(sessionId);
    if (current && !current.pending && !current.dirty && !this.activeReads.has(sessionId)) {
      this.entries.delete(sessionId);
    }
  }

  private hasOlderRead(sessionId: string, revision: number): boolean {
    const reads = this.activeReads.get(sessionId);
    return Boolean(reads && [...reads].some((read) => read.revision < revision));
  }
}
