import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

/** The small "Bot" tag an Inbox row carries when the conversation is a Bot's (D5). */
export const BotTag: React.FC<{ className?: string }> = ({ className }) => {
  const { t } = useTranslation();
  return (
    <span className={clsx('shrink-0 rounded bg-mint-soft px-1 text-[9px] font-semibold leading-[14px] text-mint-ink', className)}>
      {t('bots.tag')}
    </span>
  );
};
