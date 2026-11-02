import type { Bot } from '@/lib/bots';

import { useDraft } from './draft';

/** What a Bot is for, in the reader's language. A Bot's role is its Agent's description, and the main
 *  Bot's is a built-in English line, so the main Bot gets a localized one (kit draft copy). */
export function useBotRole(): (bot: Bot) => string {
  const draft = useDraft();
  return (bot) =>
    bot.builtin ? draft('你的第一联系人：和你商量，替你分派和跟进', 'Your first point of contact: talks it through, hands work on, follows up') : bot.role;
}
