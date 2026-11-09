// A Bot's avatar with the strip's signals (BotStrip): a cyan unread count at the
// top right and a pulsing mint dot at the bottom right while a Turn runs. The phone's
// center tab, a Bot conversation's header, and the Bot switch sheet draw it
// (ui/design: mobile/workbench-tab/team, mobile/bot-conversation/switch).
import clsx from 'clsx';

import { BotAvatar } from '@/components/ui/bot-avatar';
import type { BotAvatar as BotAvatarSpec } from '@/lib/bots';

const unreadLabel = (count: number) => (count > 99 ? '99+' : String(count));

export function BotFace({
  avatar,
  name,
  size,
  unread = 0,
  running = false,
  dotWell = 'bg-surface',
  inList = false,
  selected = false,
  className,
}: {
  avatar: BotAvatarSpec | null;
  name: string;
  size: number;
  unread?: number;
  running?: boolean;
  /** The surface the dot is cut out of, so it reads as sitting on the avatar's edge. */
  dotWell?: string;
  /** See `BotAvatar`: a face among other Bots, and the selected Bot. */
  inList?: boolean;
  selected?: boolean;
  className?: string;
}) {
  return (
    <span className={clsx('relative inline-grid shrink-0', className)}>
      <BotAvatar avatar={avatar} name={name} size={size} inList={inList} selected={selected} />
      {unread > 0 && (
        <span className="absolute -right-1.5 -top-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-cyan px-1 font-mono text-3xs font-bold text-accent-foreground">
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
