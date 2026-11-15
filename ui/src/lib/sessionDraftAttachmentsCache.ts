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
  attachments: SessionDraftAttachment[];
};

type AttachmentsStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

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

function parseRecord(raw: string | null): SessionDraftAttachmentsCacheRecord | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<SessionDraftAttachmentsCacheRecord>;
    if (
      value.version !== 1
      || !Array.isArray(value.attachments)
      || !value.attachments.every(isAttachment)
    ) {
      return null;
    }
    return {
      version: 1,
      attachments: cloneAttachments(value.attachments),
    };
  } catch {
    return null;
  }
}

export class SessionDraftAttachmentsCache {
  private readonly storage?: AttachmentsStorage;
  private readonly memory = new Map<string, SessionDraftAttachment[]>();

  constructor(storage?: AttachmentsStorage) {
    this.storage = storage;
  }

  read(sessionId: string): SessionDraftAttachment[] {
    const target = browserStorage(this.storage);
    if (!target) return cloneAttachments(this.memory.get(sessionId) ?? []);
    try {
      const key = this.key(sessionId);
      const record = parseRecord(target.getItem(key));
      if (!record) {
        this.memory.delete(sessionId);
        target.removeItem(key);
        return [];
      }
      this.memory.set(sessionId, record.attachments);
      return cloneAttachments(record.attachments);
    } catch {
      return cloneAttachments(this.memory.get(sessionId) ?? []);
    }
  }

  write(sessionId: string, attachments: readonly SessionDraftAttachment[]): void {
    const next = cloneAttachments(attachments.filter(isAttachment));
    if (!next.length) {
      this.clear(sessionId);
      return;
    }

    this.memory.set(sessionId, next);
    try {
      browserStorage(this.storage)?.setItem(
        this.key(sessionId),
        JSON.stringify({ version: 1, attachments: next } satisfies SessionDraftAttachmentsCacheRecord),
      );
    } catch {
      // The in-memory entry still protects this tab when storage is blocked or full.
    }
  }

  clear(sessionId: string): void {
    this.memory.delete(sessionId);
    try {
      browserStorage(this.storage)?.removeItem(this.key(sessionId));
    } catch {
      // Best-effort cleanup when browser storage is blocked.
    }
  }

  private key(sessionId: string): string {
    return `${SESSION_DRAFT_ATTACHMENTS_STORAGE_PREFIX}${encodeURIComponent(sessionId)}`;
  }
}

export const sessionDraftAttachmentsCache = new SessionDraftAttachmentsCache();
