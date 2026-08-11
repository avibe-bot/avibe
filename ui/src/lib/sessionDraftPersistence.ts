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
};

export type SessionDraftRead = {
  revision: number;
  pending: boolean;
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
};

type DraftSave = (draft: SessionDraftWrite) => Promise<SessionDraftSaveResult>;

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
  private readonly localCache: SessionDraftLocalCache;

  constructor(localCache = new SessionDraftLocalCache()) {
    this.localCache = localCache;
  }

  cache(sessionId: string, text: string): void {
    const current = this.entries.get(sessionId);
    const cached = this.localCache.read(sessionId);
    const baseUpdatedAt = current?.baseUpdatedAt
      ?? cached?.serverUpdatedAt
      ?? this.serverVersions.get(sessionId)
      ?? null;
    const local = this.localCache.writeDirty(sessionId, text, baseUpdatedAt);
    this.entries.set(sessionId, {
      revision: (current?.revision ?? 0) + 1,
      text,
      localId: local.mutationId,
      baseUpdatedAt,
      pending: current?.pending ?? null,
      pendingRevision: current?.pendingRevision ?? null,
      dirty: true,
      // Editing cannot resolve an already-observed version fork. Keep the
      // automatic cloud writer stopped until an authoritative read converges.
      conflict: current?.conflict ?? false,
    });
  }

  peek(sessionId: string): string | null {
    return this.localCache.read(sessionId)?.text ?? null;
  }

  save(sessionId: string, text: string, write: DraftSave): Promise<SessionDraftSaveResult> {
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

  beginRead(sessionId: string): SessionDraftRead {
    const current = this.hydrateDirty(sessionId);
    const read: SessionDraftRead = {
      revision: current?.revision ?? 0,
      pending: Boolean(current?.pending),
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
    const current = this.entries.get(sessionId);
    this.finishRead(sessionId, read);

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
        this.acceptServer(sessionId, server);
        return server.text;
      }

      this.serverVersions.set(sessionId, server.updatedAt);
      if (dirty.baseUpdatedAt !== server.updatedAt) {
        dirty.conflict = true;
      }
      return dirty.text;
    }

    this.acceptServer(sessionId, server);
    this.cleanupSuccessfulEntry(sessionId);
    return server.text;
  }

  clearSession(sessionId: string): void {
    this.entries.delete(sessionId);
    this.activeReads.delete(sessionId);
    this.serverVersions.delete(sessionId);
    this.localCache.clear(sessionId);
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
    const text = entry.text;
    const pending = (async (): Promise<SessionDraftSaveResult> => {
      const predecessorResult: SessionDraftSaveResult | undefined = await predecessor?.catch(
        (): SessionDraftSaveResult => ({ ok: false }),
      );
      const beforeWrite = this.entries.get(sessionId);
      if (!beforeWrite || beforeWrite.revision !== revision) return { ok: true };
      if (beforeWrite.conflict || predecessorResult?.conflict) {
        beforeWrite.pending = null;
        beforeWrite.pendingRevision = null;
        beforeWrite.conflict = true;
        return { ok: false, conflict: true, server: predecessorResult?.server };
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
      this.applyWriteResult(sessionId, revision, result);
      return result;
    })();

    entry.pending = pending;
    entry.pendingRevision = revision;
    return pending;
  }

  private applyWriteResult(
    sessionId: string,
    revision: number,
    result: SessionDraftSaveResult,
  ): void {
    const current = this.entries.get(sessionId);
    if (!current) return;

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
    if (result.conflict) {
      current.conflict = true;
      if (result.server) this.serverVersions.set(sessionId, result.server.updatedAt);
    }
  }

  private hydrateDirty(sessionId: string): DraftEntry | null {
    const current = this.entries.get(sessionId);
    if (current) return current;
    const cached = this.localCache.read(sessionId);
    if (!cached?.dirty) return null;
    const hydrated: DraftEntry = {
      revision: 1,
      text: cached.text,
      localId: cached.mutationId,
      baseUpdatedAt: cached.serverUpdatedAt,
      pending: null,
      pendingRevision: null,
      dirty: true,
      conflict: false,
    };
    this.entries.set(sessionId, hydrated);
    return hydrated;
  }

  private acceptServer(sessionId: string, server: SessionDraftServerState): void {
    this.serverVersions.set(sessionId, server.updatedAt);
    this.entries.delete(sessionId);
    this.localCache.writeClean(sessionId, server.text, server.updatedAt);
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
}
