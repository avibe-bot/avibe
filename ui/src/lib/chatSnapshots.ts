// Last-known conversations, so opening a chat shows its recent messages before
// the bootstrap answers (docs/plans/web-ui-local-cache.md, Phase 2). The store's
// rules hold here too (lib/localSnapshots): per reader, best-effort, and the
// server stays authoritative. What this module adds:
//
//  * A record is the bootstrap's tail window: the Session row, its newest
//    durable transcript rows, the older-page cursor, the Agent list, the default
//    Agent, ``can_chat``, and the message font size. Only rows the server stored are kept: a Delivery
//    (a send still pending) and the claimed-input projection are not.
//  * Records are tied to the UI build that wrote them, and the 40 most recently
//    written ones are kept.
//  * Writes are owned. A writer takes an owner when the server answers and may
//    store only while that owner is still current: same reader, no whole-store
//    clear since, and no ``forgetChatSnapshot`` for that session since.
//  * What this document writes or reads is also held in memory
//    (lib/chatSnapshotMirror), so a chat it opens again paints in its first
//    frame; ``knownChatSnapshot`` reads it.

import type {
  VibeAgentBrief,
  WorkbenchMessage,
  WorkbenchSession,
  WorkbenchSessionBootstrap,
} from '../context/ApiContext';
import { isTranscriptMessage } from './chatMessageTypes';
import { chatSnapshotMirror } from './chatSnapshotMirror';
import { sessionActivityInboxAction, type InboxActivityEvent } from './inboxActivity';
import {
  deleteLocalSnapshot,
  getLocalSnapshotEpoch,
  getLocalSnapshotPrincipal,
  pruneLocalSnapshots,
  readLocalSnapshot,
  writeLocalSnapshot,
} from './localSnapshots';

// 2: the message font size joined the record, so a chat paints at its size.
const CHAT_SNAPSHOT_SCHEMA = 2;
/** Conversations kept, least recently written evicted first. */
const CHAT_SNAPSHOT_SESSIONS = 40;
/** Evicting at the cap goes this far below it, so the full read eviction needs
 *  happens once every few new conversations instead of on each. */
const CHAT_SNAPSHOT_EVICTION_SLACK = 8;
/** Rows kept per conversation: the bootstrap's tail window. */
const CHAT_SNAPSHOT_ROWS = 50;
const KEY_PREFIX = 'chat:';

const chatSnapshotKey = (sessionId: string): string => `${KEY_PREFIX}${sessionId}`;

// The UI build this document runs. There is no build constant, but the page
// loads one entry module whose file name carries a content hash, and that hash
// changes whenever any chunk it loads changes, so its URL names the build. A
// record from another build is ignored and overwritten. Without a document (or
// an entry script, as under test), this module's own URL stands in.
function currentUiBuild(): string {
  try {
    const entry = typeof document === 'undefined'
      ? null
      : document.querySelector<HTMLScriptElement>('script[type="module"][src]');
    if (entry?.src) return entry.src;
  } catch {
    // Fall back below.
  }
  return import.meta.url;
}
const UI_BUILD = currentUiBuild();

export type ChatSnapshot = {
  session: WorkbenchSession;
  messages: WorkbenchMessage[];
  next_before_id: string | null;
  agents: VibeAgentBrief[];
  default_agent_name: string | null;
  can_chat: boolean;
  chat_message_font_size: number | null;
};

type StoredChatSnapshot = ChatSnapshot & { build: string };

// Advanced by ``forgetChatSnapshot`` so a read or an owner from before it is void.
const forgotten = new Map<string, number>();

/** A row the server stored: a transcript Message, not a Delivery still being
 *  sent and not the projection of a claimed input. */
const isStoredTranscriptRow = (row: WorkbenchMessage): boolean =>
  isTranscriptMessage(row)
  && !row.projection
  && row.state == null
  && (row as { delivery_id?: unknown }).delivery_id == null;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const isRow = (value: unknown): value is WorkbenchMessage =>
  isRecord(value) && typeof value.id === 'string' && typeof value.type === 'string';

const nullableString = (value: unknown): value is string | null => value === null || typeof value === 'string';

function parseChatSnapshot(sessionId: string, value: unknown): ChatSnapshot | null {
  if (!isRecord(value) || value.build !== UI_BUILD) return null;
  const { session, messages, next_before_id, agents, default_agent_name, can_chat, chat_message_font_size } = value;
  if (!isRecord(session) || session.id !== sessionId) return null;
  if (!Array.isArray(messages) || !messages.every(isRow)) return null;
  if (!nullableString(next_before_id) || !nullableString(default_agent_name)) return null;
  if (!Array.isArray(agents) || !agents.every((agent) => isRecord(agent) && typeof agent.name === 'string')) return null;
  if (typeof can_chat !== 'boolean') return null;
  if (chat_message_font_size !== null && typeof chat_message_font_size !== 'number') return null;
  return {
    session: session as WorkbenchSession,
    messages: messages.filter(isStoredTranscriptRow),
    next_before_id,
    agents: agents as VibeAgentBrief[],
    default_agent_name,
    can_chat,
    chat_message_font_size,
  };
}

/** The stored conversation for the current reader, or null. A forget that
 *  begins while the read is out voids what it found, as a clear does. What it
 *  finds is held in memory too, unless this document already holds the
 *  conversation: what it holds came from a write or a read at least as recent. */
export async function readChatSnapshot(sessionId: string): Promise<ChatSnapshot | null> {
  const request = beginChatSnapshotRequest(sessionId);
  const snapshot = await readLocalSnapshot(chatSnapshotKey(sessionId), CHAT_SNAPSHOT_SCHEMA, (value) =>
    parseChatSnapshot(sessionId, value),
  );
  if (request.generation !== (forgotten.get(sessionId) ?? 0)) return null;
  // Held only while it is still this reader's and the store was not cleared.
  if (snapshot && request.principal && isChatSnapshotOwnerCurrent({ ...request, principal: request.principal })
    && !chatSnapshotMirror.get(sessionId)) {
    chatSnapshotMirror.put(sessionId, snapshot);
  }
  return snapshot;
}

/** The conversation this document holds in memory for the current reader, or
 *  null: synchronous, so a chat can paint it in the commit that opens it. */
export function knownChatSnapshot(sessionId: string): ChatSnapshot | null {
  return chatSnapshotMirror.get(sessionId);
}

/** The newest rows of a live transcript window, capped to what a bootstrap
 *  returns, with the cursor that pages older than them. ``olderCursor`` is the
 *  window's own cursor: it applies when every stored row is kept. */
export function chatSnapshotWindow(
  rows: WorkbenchMessage[],
  olderCursor: string | null,
): Pick<ChatSnapshot, 'messages' | 'next_before_id'> {
  const stored = rows.filter(isStoredTranscriptRow);
  const messages = stored.slice(-CHAT_SNAPSHOT_ROWS);
  const next_before_id = stored.length > messages.length ? messages[0].id : olderCursor;
  return { messages, next_before_id };
}

const fontSizeOf = (config: unknown): number | null => {
  const size = (config as { ui?: { chat_message_font_size?: unknown } } | null)?.ui?.chat_message_font_size;
  return typeof size === 'number' && Number.isFinite(size) ? size : null;
};

/** The record a bootstrap answer describes. */
export function chatSnapshotFromBootstrap(bootstrap: WorkbenchSessionBootstrap): ChatSnapshot {
  return {
    session: bootstrap.session,
    ...chatSnapshotWindow(bootstrap.messages ?? [], bootstrap.next_before_id ?? null),
    agents: bootstrap.agents ?? [],
    default_agent_name: bootstrap.default_agent_name ?? null,
    can_chat: Boolean(bootstrap.capabilities?.can_chat),
    chat_message_font_size: fontSizeOf(bootstrap.config),
  };
}

export type ChatSnapshotOwner = {
  readonly sessionId: string;
  readonly principal: string;
  readonly epoch: number;
  readonly generation: number;
};

/** What the store looked like when a request for ``sessionId`` started. Unlike
 *  an owner it may have no reader yet: a cold start names the reader only once
 *  the page's first effects have run, after the chat has sent its bootstrap. */
export type ChatSnapshotRequest = {
  readonly sessionId: string;
  readonly principal: string | null;
  readonly epoch: number;
  readonly generation: number;
};

export function beginChatSnapshotRequest(sessionId: string): ChatSnapshotRequest {
  return {
    sessionId,
    principal: getLocalSnapshotPrincipal(),
    epoch: getLocalSnapshotEpoch(),
    generation: forgotten.get(sessionId) ?? 0,
  };
}

/** The owner for a request's answer, or null when it may not be stored: the
 *  reader changed, the store was cleared, or the conversation was forgotten
 *  while it was out. A request that started before any reader was named belongs
 *  to the reader named since, unless one of those happened meanwhile. */
export function chatSnapshotOwnerForAnswer(request: ChatSnapshotRequest): ChatSnapshotOwner | null {
  const principal = request.principal ?? getLocalSnapshotPrincipal();
  if (!principal) return null;
  const owner: ChatSnapshotOwner = { ...request, principal };
  return isChatSnapshotOwnerCurrent(owner) ? owner : null;
}

/** Whether data read under ``owner`` may still be stored. */
function isChatSnapshotOwnerCurrent(owner: ChatSnapshotOwner): boolean {
  return owner.principal === getLocalSnapshotPrincipal()
    && owner.epoch === getLocalSnapshotEpoch()
    && owner.generation === (forgotten.get(owner.sessionId) ?? 0);
}

// Advanced by every write a current owner makes, so a background refresh can
// tell that someone (the opened chat) stored this conversation while it was out.
const written = new Map<string, number>();

/** How many writes of ``sessionId`` this document has made. */
export function chatSnapshotWriteCount(sessionId: string): number {
  return written.get(sessionId) ?? 0;
}

/** Store ``snapshot`` if ``owner`` is still current. ``added`` says the write
 *  may create a record, which is when the oldest ones beyond the cap go. */
export async function writeChatSnapshot(
  owner: ChatSnapshotOwner,
  snapshot: ChatSnapshot,
  { added = false }: { added?: boolean } = {},
): Promise<void> {
  if (!isChatSnapshotOwnerCurrent(owner)) return;
  written.set(owner.sessionId, chatSnapshotWriteCount(owner.sessionId) + 1);
  chatSnapshotMirror.put(owner.sessionId, snapshot);
  const value: StoredChatSnapshot = { ...snapshot, build: UI_BUILD };
  await writeLocalSnapshot(chatSnapshotKey(owner.sessionId), CHAT_SNAPSHOT_SCHEMA, value);
  if (added) await pruneLocalSnapshots(KEY_PREFIX, CHAT_SNAPSHOT_SESSIONS, { slack: CHAT_SNAPSHOT_EVICTION_SLACK });
}

/** Delete the stored conversation and void every owner taken for it so far. */
export function forgetChatSnapshot(sessionId: string): Promise<void> {
  forgotten.set(sessionId, (forgotten.get(sessionId) ?? 0) + 1);
  chatSnapshotMirror.clear(sessionId);
  return deleteLocalSnapshot(chatSnapshotKey(sessionId));
}

/** A ``session.activity`` event that takes the Session out of the reader's
 *  Inbox (archived, or moved to the background) deletes its conversation. */
export function forgetChatSnapshotOnActivity(data: InboxActivityEvent & { session_id?: string | null }): void {
  if (data.session_id && sessionActivityInboxAction(data) === 'drop') void forgetChatSnapshot(data.session_id);
}
