import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import { useBots } from '../../context/BotsContext';
import { cn } from '../../lib/utils';
import { BotAvatar } from './bot-avatar';
import { badgeVariants } from './badge-variants';

/** A `@<bot:handle>` mention as a chip: the Bot's avatar and its current display
 *  name (D3). Off the Owner's surfaces, or for a Bot since removed, it keeps the
 *  label the message was sent with. */
export const BotMentionChip: React.FC<{ handle: string; fallback: ReactNode; className?: string }> = ({
  handle,
  fallback,
  className,
}) => {
  const { t } = useTranslation();
  const { botForHandle, displayName } = useBots();
  const bot = botForHandle(handle);
  return (
    <span className={cn(badgeVariants({ variant: 'success' }), 'gap-1 py-px pl-0.5 align-baseline font-medium', className)}>
      {bot ? (
        <>
          <BotAvatar avatar={bot.avatar} name={displayName(bot)} size={16} />
          <span className="truncate">{t('bots.mentionName', { name: displayName(bot) })}</span>
        </>
      ) : (
        <span className="truncate pl-1.5">{fallback}</span>
      )}
    </span>
  );
};
