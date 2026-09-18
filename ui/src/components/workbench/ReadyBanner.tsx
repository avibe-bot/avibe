import { useTranslation } from 'react-i18next';
import { CircleCheck, X } from 'lucide-react';

import { BACKEND_LABEL, isBackend } from '../../lib/backendAccent';

// Only the banner lives here; what decides whether it may be shown, and what
// would observe the backend, are in backendReadiness.ts.

/**
 * design gJMu4 — a single 42-tall mint line above the first-task home, dismissed
 * with the trailing ✕.
 */
export const ReadyBanner: React.FC<{ backend: string; onDismiss: () => void }> = ({ backend, onDismiss }) => {
  const { t } = useTranslation();
  const name = isBackend(backend) ? BACKEND_LABEL[backend] : backend;
  return (
    <div className="flex h-[42px] w-full shrink-0 items-center gap-2.5 rounded-[10px] border border-border bg-mint-soft px-4">
      <CircleCheck className="size-[18px] shrink-0 text-mint-ink" />
      <span className="min-w-0 flex-1 truncate text-[12px] text-foreground">
        {t('workbench.home.ready', { backend: name })}
      </span>
      <button
        type="button"
        onClick={onDismiss}
        aria-label={t('workbench.home.readyDismiss')}
        className="shrink-0 rounded text-muted transition-colors hover:text-foreground"
      >
        <X className="size-[15px]" />
      </button>
    </div>
  );
};
