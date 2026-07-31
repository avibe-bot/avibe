export type ChatViewMode = 'chat' | 'show-page';

const STORAGE_KEY = 'avibe.chat.show-page-sessions.v1';
export const MAX_REMEMBERED_SHOW_PAGE_SESSIONS = 200;

type ChatViewStorage = Pick<Storage, 'getItem' | 'setItem'>;

function browserStorage(storage?: ChatViewStorage): ChatViewStorage | undefined {
  return storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined);
}

function readShowPageSessions(storage?: ChatViewStorage): string[] {
  try {
    const raw = browserStorage(storage)?.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return Array.from(
      new Set(parsed.filter((value): value is string => typeof value === 'string' && value.length > 0)),
    ).slice(-MAX_REMEMBERED_SHOW_PAGE_SESSIONS);
  } catch {
    return [];
  }
}

export function readChatViewMode(sessionId: string, storage?: ChatViewStorage): ChatViewMode {
  if (!sessionId) return 'chat';
  return readShowPageSessions(storage).includes(sessionId) ? 'show-page' : 'chat';
}

export function writeChatViewMode(
  sessionId: string,
  mode: ChatViewMode,
  storage?: ChatViewStorage,
): void {
  if (!sessionId) return;
  try {
    const remembered = readShowPageSessions(storage).filter((id) => id !== sessionId);
    if (mode === 'show-page') remembered.push(sessionId);
    browserStorage(storage)?.setItem(
      STORAGE_KEY,
      JSON.stringify(remembered.slice(-MAX_REMEMBERED_SHOW_PAGE_SESSIONS)),
    );
  } catch {
    // View memory is best-effort in private browsing and restricted storage contexts.
  }
}
