export type SessionDraftAttachment = {
  token: string;
  name: string;
  mime: string;
  size: number;
  kind: 'image' | 'file';
  url: string;
  width?: number;
  height?: number;
};

type SessionDraftAttachmentsCacheRecord = {
  version: 1;
  generation: number;
  attachments: SessionDraftAttachment[];
};

type AttachmentsStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

type CacheState = {
  generation: number;
  attachments: SessionDraftAttachment[];
  persistenceFailed: boolean;
};

export const SESSION_DRAFT_ATTACHMENTS_STORAGE_PREFIX = 'avibe.session-draft-attachments.v1.';

function browserStorage(storage?: AttachmentsStorage): AttachmentsStorage | undefined {
  try {
    return storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined);
  } catch {
    return undefined;
  }
}

function isAttachment(value: unknown): value is SessionDraftAttachment {
  if (!value || typeof value !== 'object') return false;
  const attachment = value as Partial<SessionDraftAttachment>;
  return (
    typeof attachment.token === 'string'
    && attachment.token.length > 0
    && typeof attachment.name === 'string'
    && typeof attachment.mime === 'string'
    && typeof attachment.size === 'number'
    && Number.isFinite(attachment.size)
    && (attachment.kind === 'image' || attachment.kind === 'file')
    && typeof attachment.url === 'string'
    && attachment.url.length > 0
    && (attachment.width === undefined
      || (typeof attachment.width === 'number' && Number.isFinite(attachment.width)))
    && (attachment.height === undefined
      || (typeof attachment.height === 'number' && Number.isFinite(attachment.height)))
  );
}

function cloneAttachments(attachments: readonly SessionDraftAttachment[]): SessionDraftAttachment[] {
  return attachments.map((attachment) => ({ ...attachment }));
}

function cloneState(state: CacheState): CacheState {
  return {
    generation: state.generation,
    attachments: cloneAttachments(state.attachments),
    persistenceFailed: state.persistenceFailed,
  };
}

function sameAttachmentTokens(
  left: readonly SessionDraftAttachment[],
  right: readonly SessionDraftAttachment[],
): boolean {
  return left.length === right.length
    && left.every((attachment, index) => attachment.token === right[index]?.token);
}

function parseRecord(raw: string | null): SessionDraftAttachmentsCacheRecord | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<SessionDraftAttachmentsCacheRecord>;
    const generation = value.generation === undefined ? 0 : value.generation;
    if (
      value.version !== 1
      || typeof generation !== 'number'
      || !Number.isInteger(generation)
      || generation < 0
      || !Array.isArray(value.attachments)
      || !value.attachments.every(isAttachment)
    ) {
      return null;
    }
    return {
      version: 1,
      generation,
      attachments: cloneAttachments(value.attachments),
    };
  } catch {
    return null;
  }
}

export class SessionDraftAttachmentsCache {
  private readonly storage?: AttachmentsStorage;
  private readonly memory = new Map<string, CacheState>();

  constructor(storage?: AttachmentsStorage) {
    this.storage = storage;
  }

  read(sessionId: string): SessionDraftAttachment[] {
    return cloneAttachments(this.readState(sessionId).attachments);
  }

  write(sessionId: string, attachments: readonly SessionDraftAttachment[]): number {
    const next = cloneAttachments(attachments.filter(isAttachment));
    const current = this.readState(sessionId);
    const changed = !sameAttachmentTokens(current.attachments, next);
    if (!changed && !current.persistenceFailed) return current.generation;

    return this.persist(sessionId, {
      generation: changed ? current.generation + 1 : current.generation,
      attachments: next,
      persistenceFailed: false,
    });
  }

  clear(sessionId: string): number {
    const current = this.readState(sessionId);
    return this.persist(sessionId, {
      generation: current.generation + 1,
      attachments: [],
      persistenceFailed: false,
    });
  }

  writeIfGeneration(
    sessionId: string,
    expectedGeneration: number,
    attachments: readonly SessionDraftAttachment[],
  ): boolean {
    const current = this.readState(sessionId);
    if (current.generation !== expectedGeneration) return false;
    this.write(sessionId, attachments);
    return true;
  }

  private readState(sessionId: string): CacheState {
    const remembered = this.memory.get(sessionId);
    if (remembered?.persistenceFailed) return cloneState(remembered);

    const target = browserStorage(this.storage);
    if (!target) {
      return remembered
        ? cloneState(remembered)
        : { generation: 0, attachments: [], persistenceFailed: true };
    }
    try {
      const key = this.key(sessionId);
      const record = parseRecord(target.getItem(key));
      if (!record) {
        this.memory.delete(sessionId);
        try {
          target.removeItem(key);
        } catch {
          // Invalid storage is still treated as empty; cleanup is best effort.
        }
        return { generation: 0, attachments: [], persistenceFailed: false };
      }
      const state = {
        generation: record.generation,
        attachments: record.attachments,
        persistenceFailed: false,
      };
      this.memory.set(sessionId, state);
      return cloneState(state);
    } catch {
      return remembered
        ? cloneState(remembered)
        : { generation: 0, attachments: [], persistenceFailed: true };
    }
  }

  private persist(
    sessionId: string,
    state: CacheState,
  ): number {
    const next = cloneState(state);
    this.memory.set(sessionId, next);
    const target = browserStorage(this.storage);
    if (!target) {
      next.persistenceFailed = true;
      this.memory.set(sessionId, next);
      return next.generation;
    }
    try {
      target.setItem(
        this.key(sessionId),
        JSON.stringify({
          version: 1,
          generation: next.generation,
          attachments: next.attachments,
        } satisfies SessionDraftAttachmentsCacheRecord),
      );
      next.persistenceFailed = false;
    } catch {
      next.persistenceFailed = true;
      // The in-memory entry remains authoritative until a later write succeeds.
    }
    this.memory.set(sessionId, next);
    return next.generation;
  }

  private key(sessionId: string): string {
    return `${SESSION_DRAFT_ATTACHMENTS_STORAGE_PREFIX}${encodeURIComponent(sessionId)}`;
  }
}

export const sessionDraftAttachmentsCache = new SessionDraftAttachmentsCache();
