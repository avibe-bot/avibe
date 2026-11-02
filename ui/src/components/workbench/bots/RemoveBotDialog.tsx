import { useTranslation } from 'react-i18next';

import { useApi } from '../../../context/ApiContext';
import { useBots } from '../../../context/BotsContext';
import { useToast } from '../../../context/ToastContext';
import { errorMessage } from '@/lib/errorMessage';
import type { Bot } from '@/lib/bots';
import { ConfirmDialog } from '../../ui/confirm-dialog';

/** Removing a Bot asks once, from the Agents page and the sidebar strip alike
 *  (ui/design: agents/bots-redesign/remove-bot), and says what stays: its
 *  conversation and memory are kept, and its Agent returns to the Agents list
 *  (docs/plans/bots.md D1). `onRemoved` runs after the server has removed it. */
export const RemoveBotDialog: React.FC<{
  bot: Bot;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRemoved?: () => void;
}> = ({ bot, open, onOpenChange, onRemoved }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const { afterWrite, displayName } = useBots();
  const name = displayName(bot);
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      destructive
      title={t('bots.removeConfirm.title', { name })}
      description={t('bots.removeConfirm.body')}
      confirmLabel={t('bots.menu.remove')}
      onConfirm={async () => {
        try {
          await api.removeBot(bot.handle);
          showToast(t('bots.removed', { name }), 'success');
          onOpenChange(false);
          onRemoved?.();
        } catch (err) {
          showToast(errorMessage(err) || t('bots.removeFailed'), 'error');
        } finally {
          void afterWrite();
        }
      }}
    />
  );
};
