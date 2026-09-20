import { ArrowRight, ShieldCheck } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { MODEL_HUB_SETTINGS_PATH } from '../models/modelHubRoutes';

export function HubOwnedAuthNotice({ onNavigate }: { onNavigate?: () => void }) {
  const { t } = useTranslation();

  return (
    <div
      role="alert"
      className="flex items-start gap-2 rounded-md border border-border bg-surface-2/60 px-3 py-2"
    >
      <ShieldCheck className="mt-0.5 size-3.5 shrink-0 text-muted" />
      <div className="flex min-w-0 flex-col gap-1">
        <p className="text-[12px] leading-relaxed text-foreground">
          {t('settings.backends.nativeAuthHubOwned')}
        </p>
        <Link
          to={MODEL_HUB_SETTINGS_PATH}
          onClick={onNavigate}
          className="inline-flex w-fit items-center gap-1 text-[12px] font-medium text-cyan-ink hover:text-cyan-ink/80"
        >
          {t('settings.backends.openModelHub')}
          <ArrowRight className="size-3" aria-hidden="true" />
        </Link>
      </div>
    </div>
  );
}
