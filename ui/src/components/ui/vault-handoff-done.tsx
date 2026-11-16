import { CheckCircle2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

/**
 * One line for a step the desktop app handed to this browser tab, once it is done here: this tab can
 * be closed, and the app comes back by itself where it can tell. A step handed off from a
 * conversation also links back to it (`avibe://session/<id>`). ui/design: vault/handoff-done
 */
export const VaultHandoffDone: React.FC<{ session: string | null }> = ({ session }) => {
  const { t } = useTranslation();
  return (
    <div role="status" className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-mint/40 bg-mint/10 px-4 py-3 text-sm text-mint-ink">
      <CheckCircle2 className="size-4 shrink-0" />
      <span className="min-w-0 flex-1">{t('vaults.handoff.done')}</span>
      {session ? (
        <a href={`avibe://session/${session}`} className="shrink-0 font-semibold underline underline-offset-2">
          {t('vaults.handoff.openApp')}
        </a>
      ) : null}
    </div>
  );
};
