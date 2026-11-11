// Bots (docs/plans/bots.md): an enabled Vibey Agent plus one permanent Session.
// The Web contract is `GET /api/bots`; this module holds the pure parts every
// surface shares — the payload shapes, the Session-id namespace, and the
// default avatar a Bot shows before one is chosen.

/** What `GET /api/bots` says a Bot looks like. `null` means no avatar chosen. */
export type BotAvatar =
  | { kind: 'mascot' }
  | { kind: 'emoji'; emoji: string; hue: number }
  | { kind: 'image'; url: string };

export type Bot = {
  /** Lowercase ASCII, frozen once the Bot exists (D2). */
  handle: string;
  /** Any language. Empty means the main Bot's localized default name. */
  display_name: string;
  avatar: BotAvatar | null;
  /** The Agent's description (D1). */
  role: string;
  agent_name: string;
  /** Always `ses_<handle>`. */
  session_id: string;
  /** The main Bot: always first, never removed. */
  builtin: boolean;
  /** "Hide details" in the Bot's conversation (`conversationDetails`); on unless turned off. */
  hide_details: boolean;
};

/** The places for custom Bots, as `GET /api/bots` sends them beside the list
 *  (docs/plans/bots.md D12): `count` current custom Bots of at most `max`. The
 *  server owns what counts (`core.bots.counts_toward_limit`); the main Bot and
 *  the system Bots do not. An install from before the limit can be over it. */
export type BotLimit = { count: number; max: number };

/** Whether adding a Bot waits until one is removed. Unknown limits never block:
 *  the server still refuses, and the add dialog then says why. */
export function botLimitReached(limit: BotLimit | null | undefined): boolean {
  return limit != null && limit.count >= limit.max;
}

export function parseBotLimit(value: unknown): BotLimit | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  if (typeof raw.count !== 'number' || typeof raw.max !== 'number') return null;
  return { count: raw.count, max: raw.max };
}

/** The Bot an Agent is, as Agent lists carry it. */
export type AgentBotSummary = {
  handle: string;
  display_name: string;
  builtin: boolean;
};

/** An emoji avatar as the API accepts it (`PATCH`/`POST` `avatar`). */
export type BotEmojiAvatarInput = { emoji: string; hue: number };

/** Generated Session ids are `ses` plus characters without `_`, so `ses_` is the
 *  Bots' namespace alone (D2). Whether a Bot still owns the id is the list's call. */
export const BOT_SESSION_ID_PREFIX = 'ses_';
export const MAIN_BOT_SESSION_ID = 'ses_main';
/** The main Bot's handle (D9): its Agent is the built-in Bot with this handle. */
export const MAIN_BOT_HANDLE = 'main';

export function isBotSessionId(sessionId: string | null | undefined): boolean {
  return typeof sessionId === 'string' && sessionId.startsWith(BOT_SESSION_ID_PREFIX);
}

/** Whether an Agent can be set as a Bot from the Web (D1, D5): an enabled Vibey
 *  Agent that is not archived and not a Bot already. Built-in Agents are left
 *  out: the default `vibey` Agent stays an ordinary worker (D9), and the main
 *  Bot's Agent is one already. */
export function eligibleForBot(agent: {
  backend: string;
  enabled: boolean;
  archived?: boolean;
  source?: string;
  bot?: AgentBotSummary | null;
}): boolean {
  return agent.backend === 'vibey' && agent.enabled && !agent.archived && !agent.bot && agent.source !== 'builtin';
}

/** The Agents of the Bots as the live list knows them, or `null` until the server
 *  has returned one (a failed read is not "no Bots", and anyone but the Owner has no
 *  list): Agent rows are a snapshot, and a Bot added or removed since they were read
 *  moves its Agent at once. */
export function liveBotAgentNames(live: { bots: readonly Pick<Bot, 'agent_name'>[]; listed: boolean }): Set<string> | null {
  return live.listed ? new Set(live.bots.map((bot) => bot.agent_name)) : null;
}

/** Whether a picker offers an Agent (docs/plans/bots.md D1): enabled, not archived,
 *  and not a Bot's Agent, the main Bot's included. A Bot's Agent is internal to its
 *  Bot, so users pick the Bot, never its Agent; a removed Bot's Agent is an ordinary
 *  Agent again. `botAgents` is the live list's answer (`liveBotAgentNames`), else
 *  the row's own `bot` field decides. Mirrors `core.vibe_agents.is_selectable_agent`,
 *  whose server guards refuse moving a Session, Project, or IM scope onto a Bot's Agent. */
export function isSelectableAgent(
  agent: { name: string; enabled: boolean; archived?: boolean; bot?: AgentBotSummary | null },
  botAgents?: ReadonlySet<string> | null,
): boolean {
  if (!agent.enabled || agent.archived) return false;
  return botAgents ? !botAgents.has(agent.name) : !agent.bot;
}

/** The name a Bot goes by: its display name, the localized default for the main
 *  Bot, else its handle. */
export function botDisplayName(bot: Pick<Bot, 'display_name' | 'builtin' | 'handle'>, mainDefault: string): string {
  return bot.display_name.trim() || (bot.builtin ? mainDefault : bot.handle);
}

/** What a Bot's conversation shows besides the conversation itself (owner decision
 *  2026-11-02). With "Hide details" on, the default, it reads like talking to a
 *  person: no execution process (Activity chips and cards, the running card
 *  included), no Harness messages (queued ones included), and no background-task
 *  banner; the Bot's own messages, the user's queued ones, and the thinking bubble
 *  while it works always show. With it off,
 *  Harness messages and the banner show, and the execution process follows the
 *  Settings switch "Show Agent Activity" as in any Session. */
export function conversationDetails(hideDetails: boolean, showAgentActivity: boolean): {
  executionProcess: boolean;
  harnessMessages: boolean;
  backgroundTasks: boolean;
} {
  return {
    executionProcess: !hideDetails && showAgentActivity,
    harnessMessages: !hideDetails,
    backgroundTasks: !hideDetails,
  };
}

/** What a Bot is for: its Agent's description (D1). The main Bot's is a built-in
 *  English line, so it reads as `mainRole`, the reader's-language copy. */
export function botRole(bot: Pick<Bot, 'role' | 'builtin'>, mainRole: string): string {
  return bot.builtin ? mainRole : bot.role;
}

export function botSessionPath(bot: Pick<Bot, 'session_id'>): string {
  return `/chat/${encodeURIComponent(bot.session_id)}`;
}

/** D2's handle grammar, for the pre-creation edit: 2 to 24 characters, a letter
 *  first, and no trailing hyphen (an Agent name never ends in one). */
export const BOT_HANDLE_RE = /^[a-z][a-z0-9-]{0,22}[a-z0-9]$/;

/** The hues the avatar picker offers; a name-derived default draws from them too. */
export const BOT_AVATAR_HUES = [200, 42, 150, 330, 265, 12, 180, 90] as const;

/** The emoji the avatar picker leads with, one per role people give a Bot, in
 *  rows of eight; any other emoji is one "More" away. All are Emoji 12 or older,
 *  so every current system draws them, and fully qualified (with U+FE0F where
 *  Unicode asks for it), so a pick reads the same here and in the full set. */
export const BOT_AVATAR_EMOJIS = [
  // security, product, research, writing, design, data, engineering, operations
  '🛡️', '🧭', '🔬', '🖋️', '🎨', '📊', '💻', '🛠️',
  // testing, launches, marketing, partners and people, support, finance, legal, schedule
  '🧪', '🚀', '📣', '🤝', '🎧', '💰', '⚖️', '📅',
  // admin, translation, review, ideas, coaching, learning, health, fitness
  '🗂️', '🌐', '🔍', '💡', '🎯', '📚', '🩺', '💪',
  // travel, home, cooking, growth, media, and two for any role
  '✈️', '🏠', '🍳', '🌱', '🎬', '🤖', '🦉',
] as const;

/** A stable hue from the name, so the default avatar keeps its color as the
 *  Bot is renamed only when the name itself changes. */
export function nameHue(name: string): number {
  let hash = 0;
  for (const char of name.trim()) {
    hash = (hash * 31 + (char.codePointAt(0) ?? 0)) >>> 0;
  }
  return BOT_AVATAR_HUES[hash % BOT_AVATAR_HUES.length];
}

/** The first character of the name (a whole grapheme for CJK and emoji). */
export function nameInitial(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return '?';
  const Segmenter = (Intl as { Segmenter?: typeof Intl.Segmenter }).Segmenter;
  const first = Segmenter
    ? new Segmenter(undefined, { granularity: 'grapheme' }).segment(trimmed)[Symbol.iterator]().next().value?.segment
    : Array.from(trimmed)[0];
  return (first ?? '?').toLocaleUpperCase();
}

/** `bots` in the order `held` names them (handles), for a list the user's hand is on:
 *  each Bot shows its latest details in the place it had, and a Bot `held` does not name
 *  follows, in its order in `bots` (docs/plans/bots.md D13). */
export function keepBotOrder(bots: readonly Bot[], held: readonly string[]): Bot[] {
  const place = new Map(held.map((handle, index) => [handle, index]));
  return [...bots].sort((a, b) => (place.get(a.handle) ?? held.length) - (place.get(b.handle) ?? held.length));
}

export function parseBot(value: unknown): Bot | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  if (typeof raw.handle !== 'string' || !raw.handle) return null;
  return {
    handle: raw.handle,
    display_name: typeof raw.display_name === 'string' ? raw.display_name : '',
    avatar: parseBotAvatar(raw.avatar),
    role: typeof raw.role === 'string' ? raw.role : '',
    agent_name: typeof raw.agent_name === 'string' ? raw.agent_name : raw.handle,
    session_id: typeof raw.session_id === 'string' ? raw.session_id : `${BOT_SESSION_ID_PREFIX}${raw.handle}`,
    builtin: raw.builtin === true,
    // A server from before the switch sends none: details stay hidden, as there.
    hide_details: raw.hide_details !== false,
  };
}

function parseBotAvatar(value: unknown): BotAvatar | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  if (raw.kind === 'mascot') return { kind: 'mascot' };
  if (raw.kind === 'emoji' && typeof raw.emoji === 'string' && typeof raw.hue === 'number') {
    return { kind: 'emoji', emoji: raw.emoji, hue: raw.hue };
  }
  if (raw.kind === 'image' && typeof raw.url === 'string') return { kind: 'image', url: raw.url };
  return null;
}
