// A one-line composer to the main Bot (or the Bot a peek sheet shows), in thumb reach above the tab bar (variant B)
// or in its peek sheet (variant C). Sending opens the main Bot's conversation with
// the message on its way; the full composer (files, route, voice) lives there.
import { ArrowUp, Mic } from 'lucide-react';
import clsx from 'clsx';

import { BotAvatar } from '@/components/ui/bot-avatar';
import { useBots } from '@/context/BotsContext';

import { useDraft } from './draft';

export function MainBotQuickComposer({ docked = false, handle }: { docked?: boolean; handle?: string }) {
  const draft = useDraft();
  const { mainBot, botForHandle, displayName } = useBots();
  const bot = handle ? botForHandle(handle) : mainBot;
  if (!bot) return null;
  const name = displayName(bot);
  const field = (
    <div className="flex items-center gap-2 rounded-full border border-border-strong bg-surface py-1.5 pl-1.5 pr-1.5 shadow-mint-card-sm">
      <BotAvatar avatar={bot.avatar} name={name} size={30} />
      <span className="min-w-0 flex-1 truncate text-sm text-muted">{draft(`和${name}说…`, `Message ${name}…`)}</span>
      <span className="grid size-8 place-items-center rounded-full text-muted" aria-hidden="true">
        <Mic className="size-4" />
      </span>
      <span className="grid size-8 place-items-center rounded-full bg-mint text-primary-foreground opacity-60" aria-hidden="true">
        <ArrowUp className="size-4" />
      </span>
    </div>
  );
  if (!docked) return field;
  return (
    <div className={clsx('fixed inset-x-0 bottom-(--mobile-nav-height) z-30 px-4 pb-2 pt-6 md:hidden', 'bg-gradient-to-t from-background via-background/90 to-transparent')}>
      {field}
    </div>
  );
}
