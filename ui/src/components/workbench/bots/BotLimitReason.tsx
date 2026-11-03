import { useTranslation } from 'react-i18next';

import type { BotLimit } from '@/lib/bots';

/** Why adding a Bot waits, with the places taken right beside it (docs/plans/bots.md D12): the
 *  main Bot is named, since it does not count, so `5/5` reads true next to a list of six. The
 *  Agents page's Add card and "Set as Bot", and the add dialog, all say it in these words. */
export function BotLimitReason({ limit, mainName }: { limit: BotLimit; mainName: string }) {
  const { t } = useTranslation();
  return (
    <>
      {t('bots.limit.reached', { max: limit.max, main: mainName })}
      <span className="ml-1.5 whitespace-nowrap font-mono">{t('bots.limit.places', { taken: limit.count, max: limit.max })}</span>
    </>
  );
}
