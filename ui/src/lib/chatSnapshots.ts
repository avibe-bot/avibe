// The conversation a chat paints before its bootstrap answers, read from the
// local message store (docs/plans/local-sync-store.md section 4.3). Every page,
// streamed row, and bootstrap the client receives is written to that store at
// the API layer, under its reader fence and wipe rules, so this module only
// reads: synchronously from the store's hot set when this document holds the
// conversation, otherwise from IndexedDB.
//
// A conversation paints once a bootstrap of it was stored (that is what gives
// the Agents, the default Agent, ``can_chat``, and the message font size), and
// only its newest rows with no gap among them, at most as many as a bootstrap
// returns.

import type { VibeAgentBrief, WorkbenchMessage, WorkbenchSession, WorkbenchSessionBootstrap } from '../context/ApiContext';
import {
  knownLocalStoreChat,
  readLocalStoreChat,
  type LocalStoreChat,
  type LocalStoreChatChrome,
} from './localStore';

export type ChatSnapshot = {
  session: WorkbenchSession;
  messages: WorkbenchMessage[];
  next_before_id: string | null;
  agents: VibeAgentBrief[];
  default_agent_name: string | null;
  can_chat: boolean;
  chat_message_font_size: number | null;
};

const isAgent = (value: unknown): value is VibeAgentBrief =>
  typeof value === 'object' && value !== null && typeof (value as { name?: unknown }).name === 'string';

function snapshotOf(chat: LocalStoreChat | null): ChatSnapshot | null {
  if (!chat) return null;
  return {
    session: chat.session,
    messages: chat.messages,
    next_before_id: chat.next_before_id,
    agents: chat.chrome.agents.filter(isAgent),
    default_agent_name: chat.chrome.default_agent_name,
    can_chat: chat.chrome.can_chat,
    chat_message_font_size: chat.chrome.chat_message_font_size,
  };
}

/** The conversation for the current reader, from memory: synchronous, so a chat
 *  can paint it in the commit that opens it. Null when this document does not
 *  hold it. */
export function knownChatSnapshot(sessionId: string): ChatSnapshot | null {
  return snapshotOf(knownLocalStoreChat(sessionId));
}

/** The stored conversation for the current reader, or null. */
export async function readChatSnapshot(sessionId: string): Promise<ChatSnapshot | null> {
  return snapshotOf(await readLocalStoreChat(sessionId));
}

const fontSizeOf = (config: unknown): number | null => {
  const size = (config as { ui?: { chat_message_font_size?: unknown } } | null)?.ui?.chat_message_font_size;
  return typeof size === 'number' && Number.isFinite(size) ? size : null;
};

/** What a bootstrap says around its transcript, as the store keeps it. */
export function chatChromeFromBootstrap(bootstrap: WorkbenchSessionBootstrap): LocalStoreChatChrome {
  return {
    agents: Array.isArray(bootstrap.agents) ? bootstrap.agents : [],
    default_agent_name: bootstrap.default_agent_name ?? null,
    can_chat: Boolean(bootstrap.capabilities?.can_chat),
    chat_message_font_size: fontSizeOf(bootstrap.config),
  };
}
