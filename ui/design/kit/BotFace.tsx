// A Bot's avatar with the strip's signals (BotStrip): a cyan unread count at the
// top right and a pulsing mint dot at the bottom right while a Turn runs.
import clsx from 'clsx';

import { BotAvatar } from '@/components/ui/bot-avatar';
import type { Bot } from '@/lib/bots';

const unreadLabel = (count: number) => (count > 99 ? '99+' : String(count));

export function BotFace({
  bot,
  name,
  size,
  unread = 0,
  running = false,
  dotWell = 'bg-surface',
  className,
}: {
  bot: Bot;
  name: string;
  size: number;
  unread?: number;
  running?: boolean;
  /** The surface the dot is cut out of, so it reads as sitting on the avatar's edge. */
  dotWell?: string;
  className?: string;
}) {
  return (
    <span className={clsx('relative inline-grid shrink-0', className)}>
      <BotAvatar avatar={bot.avatar} name={name} size={size} />
      {unread > 0 && (
        <span className="absolute -right-1.5 -top-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-cyan px-1 font-mono text-3xs font-bold text-accent-foreground shadow-glow-xs-cyan">
          {unreadLabel(unread)}
        </span>
      )}
      {running && (
        <span className={clsx('absolute -bottom-0.5 -right-0.5 grid size-3.5 place-items-center rounded-full', dotWell)}>
          <span className="relative size-2">
            <span className="absolute inset-0 animate-ping rounded-full bg-mint opacity-70 motion-reduce:animate-none" />
            <span className="relative block size-2 rounded-full bg-mint shadow-glow-dot-mint" />
          </span>
        </span>
      )}
    </span>
  );
}
