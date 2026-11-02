import { createContext, useContext } from 'react';

/** Which Sessions are Bot conversations, for the Inbox's categories
 *  (docs/plans/workbench-refinements.md §1).
 *
 *  The answer is the server's: a Session is a Bot's while it is the permanent
 *  Session of a current Bot, which is exactly the Owner's live Bot list
 *  (`BotsProvider`, refetched on `bots.changed`). A removed Bot's Session is
 *  hidden, not reclassified: the Inbox re-reads on `bots.changed` and the server
 *  leaves it out. A reader who is not the Owner has no Bots.
 *
 *  `BotsProvider` lives inside the router, under the authorization it needs,
 *  while the Inbox and the badges it drives live above the router. This bridge
 *  carries the one fact the Inbox needs up to it. */
export type BotRoster = {
  sessionIds: ReadonlySet<string>;
  /** The list has answered, or the reader has no Bots to wait for. */
  known: boolean;
};

export const UNKNOWN_BOT_ROSTER: BotRoster = { sessionIds: new Set(), known: false };

/** Outside a `BotRosterProvider` (a test harness, a Show Page) there are no Bots. */
const NO_BOTS: BotRoster = { sessionIds: new Set(), known: true };

export interface BotRosterBridge {
  roster: BotRoster;
  publish: (roster: BotRoster) => void;
}

export const BotRosterContext = createContext<BotRosterBridge>({ roster: NO_BOTS, publish: () => undefined });

export const useBotRoster = (): BotRosterBridge => useContext(BotRosterContext);

export const sameBotRoster = (a: BotRoster, b: BotRoster): boolean =>
  a.known === b.known && a.sessionIds.size === b.sessionIds.size && [...a.sessionIds].every((id) => b.sessionIds.has(id));
