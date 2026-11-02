import { createContext, useContext } from 'react';

import type { Bot } from '../lib/bots';

/** What the shared add/edit dialog opens on (see `BotProfileDialog`). */
export type BotProfileRequest =
  | { kind: 'create'; tab?: 'new' | 'agent'; agentName?: string }
  | { kind: 'edit'; handle: string };

export type BotRuntimeStatus = 'idle' | 'running' | 'failed';

// The Owner's Bots (docs/plans/bots.md D1, D5) and the live state the avatar strip
// draws from. `BotsProvider.tsx` owns the read, the SSE subscription and the
// writes; this module is the component-free handle consumers import.
export interface BotsState {
  /** Bots are the instance Owner's: everyone else gets an empty, inert state. */
  enabled: boolean;
  /** In display order: the main Bot first. */
  bots: Bot[];
  /** The first read has answered (successfully or not). */
  loaded: boolean;
  /** The server has returned a list: a failed read is not "no Bots". */
  listed: boolean;
  mainBot: Bot | null;
  /** Each Bot Session's agent status; a Session not yet read is absent. */
  statusBySession: Record<string, BotRuntimeStatus>;
  botForSession: (sessionId: string | null | undefined) => Bot | undefined;
  botForAgent: (agentName: string | null | undefined) => Bot | undefined;
  botForHandle: (handle: string | null | undefined) => Bot | undefined;
  /** The name a Bot goes by, with the main Bot's localized default. */
  displayName: (bot: Bot) => string;
  /** Call after this page writes a Bot or a Bot's Agent. Every such write
   *  publishes `bots.changed` once it commits, and that event is the one refetch;
   *  only while the event stream is not live does this read the list itself. */
  afterWrite: () => Promise<void>;
  /** Move a Bot (not the main one) to `position` (1-based among the others),
   *  optimistically; resolves false when the server refused it. */
  moveBot: (handle: string, position: number) => Promise<boolean>;
  /** Open the one add/edit dialog: the strip's Add slot, a Bot's Edit profile, and
   *  the Agents page's "Set as Bot" all land here. */
  openProfile: (request: BotProfileRequest) => void;
  /** What the dialog is open on, if anything. The shell mounts the dialog under
   *  its route-activity boundary, so Settings opening over it suspends it. */
  profileRequest: BotProfileRequest | null;
  closeProfile: () => void;
}

const EMPTY: BotsState = {
  enabled: false,
  bots: [],
  loaded: false,
  listed: false,
  mainBot: null,
  statusBySession: {},
  botForSession: () => undefined,
  botForAgent: () => undefined,
  botForHandle: () => undefined,
  displayName: (bot) => bot.display_name || bot.handle,
  afterWrite: async () => undefined,
  moveBot: async () => false,
  openProfile: () => undefined,
  profileRequest: null,
  closeProfile: () => undefined,
};

export const BotsContext = createContext<BotsState>(EMPTY);

/** The Owner's Bots. Outside a `BotsProvider` (a Show Page, a test harness) this is
 *  the empty state, so a surface that merely decorates with Bot identity — a
 *  mention chip, an Inbox row — degrades to its plain rendering. */
export const useBots = (): BotsState => useContext(BotsContext);
