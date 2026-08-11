export type SessionDraftSaveResult = { ok: boolean };
export type SessionDraftRead = {
  revision: number;
  pending: boolean;
};

type DraftEntry = {
  revision: number;
  text: string;
  pending: Promise<SessionDraftSaveResult> | null;
  dirty: boolean;
};

type DraftSave = () => Promise<SessionDraftSaveResult>;

/**
 * Serializes per-session draft writes and closes the read-after-write gap that
 * appears when navigation remounts ChatPage before its fire-and-forget PUT
 * finishes. The latest text stays available for a read that raced a write.
 */
export class SessionDraftPersistence {
  private readonly entries = new Map<string, DraftEntry>();
  private readonly activeReads = new Map<string, Set<SessionDraftRead>>();

  save(sessionId: string, text: string, write: DraftSave): Promise<SessionDraftSaveResult> {
    const previous = this.entries.get(sessionId);
    const revision = (previous?.revision ?? 0) + 1;
    const entry: DraftEntry = {
      revision,
      text,
      pending: null,
      dirty: true,
    };
    const predecessor = previous?.pending;
    const pending = (async (): Promise<SessionDraftSaveResult> => {
      await predecessor?.catch(() => undefined);
      // A newer edit superseded this write while it was waiting. Its successor
      // carries this promise as the predecessor and will write the latest text.
      if (this.entries.get(sessionId)?.revision !== revision) return { ok: true };

      let result: SessionDraftSaveResult;
      try {
        result = await write();
      } catch {
        result = { ok: false };
      }
      const current = this.entries.get(sessionId);
      if (current?.revision === revision) {
        if (result.ok) {
          current.pending = null;
          current.dirty = false;
          if (!this.activeReads.has(sessionId)) this.entries.delete(sessionId);
        } else {
          current.pending = null;
          current.dirty = true;
        }
      }
      return result;
    })();
    entry.pending = pending;
    this.entries.set(sessionId, entry);
    return pending;
  }

  beginRead(sessionId: string): SessionDraftRead {
    const current = this.entries.get(sessionId);
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

  reconcileRead(sessionId: string, read: SessionDraftRead, serverText: string): string {
    const current = this.entries.get(sessionId);
    this.finishRead(sessionId, read);
    if (!current) return serverText;
    const localWins = read.pending || current.revision > read.revision || current.pending || current.dirty;
    const text = localWins ? current.text : serverText;
    this.cleanupSuccessfulEntry(sessionId);
    return text;
  }

  clearSession(sessionId: string): void {
    this.entries.delete(sessionId);
    this.activeReads.delete(sessionId);
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
