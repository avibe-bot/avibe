export type SessionDraftSaveResult = { ok: boolean };

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
          this.entries.delete(sessionId);
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

  async waitForWrites(sessionId: string): Promise<void> {
    while (true) {
      const pending = this.entries.get(sessionId)?.pending;
      if (!pending) return;
      await pending;
      if (this.entries.get(sessionId)?.pending !== pending) continue;
      return;
    }
  }

  revision(sessionId: string): number {
    return this.entries.get(sessionId)?.revision ?? 0;
  }

  /**
   * Prefer local text when a write failed or changed while the read was in
   * flight. Once a clean write is known to predate the read, the server is the
   * source of truth and the temporary entry can be retired.
   */
  reconcileRead(sessionId: string, readRevision: number, serverText: string): string {
    const current = this.entries.get(sessionId);
    if (!current) return serverText;
    if (current.revision > readRevision || current.pending || current.dirty) return current.text;
    this.entries.delete(sessionId);
    return serverText;
  }
}
