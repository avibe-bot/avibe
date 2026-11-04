import type { WorkbenchMessage } from '@/context/ApiContext';
import { resultFooterParts } from './resultFooter';
import { cleanVoiceVocabulary, voiceVocabulary } from './voiceVocabulary';

/**
 * The conversation a dictation is spoken into, sent to recognition so it hears the names and
 * terms already in play: the Agent's latest reply there and the user's previous message there.
 * Recognition context only: never log, store, or show it. A resolved context also carries
 * `vocabulary`, the names to hear exactly (`voiceVocabulary`), read when the dictation starts.
 */
export type VoiceContext = { reply?: string; userPrevious?: string; vocabulary?: readonly string[] };

/** A context in hand, or one still loading (a Bot's avatar reads its conversation when held). */
export type VoiceContextSource = VoiceContext | Promise<VoiceContext | undefined> | undefined;

/** Each text is cut to its newest this many characters (UTF-16 units): what was said last is
 *  what the user is most likely answering. avibe.bot accepts this much on every path. */
export const MAX_VOICE_CONTEXT_CHARS = 8_000;

/** How long recognition waits for a context still loading before it starts without one. */
export const VOICE_CONTEXT_WAIT_MS = 1_500;

/** How many of a conversation's newest transcript rows a context is read from. */
export const VOICE_CONTEXT_TAIL_ROWS = 20;

const clipText = (text: string | undefined): string | undefined => {
  const trimmed = text?.trim();
  if (!trimmed) return undefined;
  if (trimmed.length <= MAX_VOICE_CONTEXT_CHARS) return trimmed;
  let tail = trimmed.slice(-MAX_VOICE_CONTEXT_CHARS);
  // Never start on the second half of a surrogate pair.
  const first = tail.charCodeAt(0);
  if (first >= 0xdc00 && first <= 0xdfff) tail = tail.slice(1);
  return tail.trimStart() || undefined;
};

/** The context with each text trimmed and cut to its newest `MAX_VOICE_CONTEXT_CHARS`, and the
 *  names it was given kept within avibe.bot's bounds; empty parts are dropped. */
export const clipVoiceContext = (context: VoiceContext | undefined): VoiceContext => {
  const reply = clipText(context?.reply);
  const userPrevious = clipText(context?.userPrevious);
  const vocabulary = cleanVoiceVocabulary(context?.vocabulary ?? []);
  return {
    ...(reply ? { reply } : {}),
    ...(userPrevious ? { userPrevious } : {}),
    ...(vocabulary.length ? { vocabulary } : {}),
  };
};

/** Reads the context from a conversation's transcript rows, oldest first: the body of the newest
 *  Agent reply (without Avibe's generated footer) and the newest message the user sent. A running
 *  Turn still reads the previous reply: a result row is persisted whole. */
export const voiceContextFromMessages = (
  rows: readonly Pick<WorkbenchMessage, 'author' | 'type' | 'text' | 'content'>[],
): VoiceContext => {
  let reply: string | undefined;
  let userPrevious: string | undefined;
  for (let index = rows.length - 1; index >= 0 && (reply === undefined || userPrevious === undefined); index -= 1) {
    const row = rows[index];
    if (reply === undefined && row.author === 'agent' && row.type === 'result') {
      reply = resultFooterParts(row).body;
    } else if (userPrevious === undefined && row.type === 'user') {
      userPrevious = row.text;
    }
  }
  return clipVoiceContext({ reply, userPrevious });
};

/** Loads a conversation's context from its newest rows, for a surface that does not hold its
 *  transcript (a Bot's avatar, the tab bar's center). A failure is no context. */
export const loadVoiceContext = async (
  listMessages: (sessionId: string, params: { tail: true; limit: number; cache: false }) => Promise<{ messages: WorkbenchMessage[] }>,
  sessionId: string,
): Promise<VoiceContext> => {
  try {
    const { messages } = await listMessages(sessionId, { tail: true, limit: VOICE_CONTEXT_TAIL_ROWS, cache: false });
    return voiceContextFromMessages(messages);
  } catch {
    return {};
  }
};

/** The context a take sends, clipped, with the names to hear exactly as they are when it is
 *  called (a dictation's start); a conversation still loading is waited for at most `waitMs`, then
 *  recognition goes without it. Never rejects. */
export const resolveVoiceContext = (
  source: VoiceContextSource,
  waitMs: number = VOICE_CONTEXT_WAIT_MS,
): Promise<VoiceContext> => {
  const vocabulary = voiceVocabulary();
  const withNames = (context: VoiceContext): VoiceContext => (
    vocabulary.length && !context.vocabulary ? { ...context, vocabulary } : context
  );
  return resolveConversation(source, waitMs).then(withNames);
};

const resolveConversation = async (source: VoiceContextSource, waitMs: number): Promise<VoiceContext> => {
  if (!source) return {};
  if (!(source instanceof Promise)) return clipVoiceContext(source);
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const context = await Promise.race([
      source,
      new Promise<undefined>((resolve) => {
        timer = setTimeout(() => resolve(undefined), waitMs);
      }),
    ]);
    return clipVoiceContext(context);
  } catch {
    return {};
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
};
