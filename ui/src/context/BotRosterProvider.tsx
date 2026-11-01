import { useCallback, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

import { BotRosterContext, UNKNOWN_BOT_ROSTER, sameBotRoster, type BotRoster } from './BotRosterContext';

/** Holds the roster `BotsProvider` publishes, for the providers above the router.
 *  Unknown until a `BotsProvider` has answered: badges wait for it rather than
 *  count a Bot's unread in the wrong category. */
export const BotRosterProvider = ({ children }: { children: ReactNode }) => {
  const [roster, setRoster] = useState<BotRoster>(UNKNOWN_BOT_ROSTER);
  const publish = useCallback((next: BotRoster) => {
    setRoster((previous) => (sameBotRoster(previous, next) ? previous : next));
  }, []);
  const value = useMemo(() => ({ roster, publish }), [roster, publish]);
  return <BotRosterContext.Provider value={value}>{children}</BotRosterContext.Provider>;
};
