import { useTranslation } from 'react-i18next';
import { AlertTriangle, RotateCcw } from 'lucide-react';
import clsx from 'clsx';

import { Button } from './button';

// Kept dependency-light on purpose: this renders precisely when something else just threw, so it must
// not itself rely on app data that might be the cause. i18n `t` returns the key if missing (no throw).
export const ErrorFallback: React.FC<{ error: unknown; reset: () => void; variant: 'page' | 'inline' }> = ({ error, reset, variant }) => {
  const { t } = useTranslation();
  const detail = error instanceof Error ? error.message : typeof error === 'string' ? error : '';
  return (
    <div className={clsx('grid h-full w-full place-items-center bg-surface p-6 text-center', variant === 'page' && 'min-h-[60vh]')}>
      <div className="flex max-w-sm flex-col items-center gap-3">
        <span className="grid size-12 shrink-0 place-items-center rounded-2xl border border-gold/40 bg-gold/[0.08]">
          <AlertTriangle className="size-6 text-gold-ink" />
        </span>
        <div className="text-[15px] font-semibold text-foreground">{t('errorBoundary.title')}</div>
        <div className="text-[12.5px] text-muted">{t('errorBoundary.body')}</div>
        {detail && (
          <div className="max-w-full truncate rounded bg-surface-3 px-2 py-1 font-mono text-[11px] text-muted" title={detail}>
            {detail}
          </div>
        )}
        <div className="mt-1 flex items-center gap-2">
          <Button type="button" size="sm" variant="brand" className="gap-1.5" onClick={reset}>
            <RotateCcw className="size-3.5" /> {t('errorBoundary.retry')}
          </Button>
          <Button type="button" size="sm" variant="outline" onClick={() => window.location.reload()}>
            {t('errorBoundary.reload')}
          </Button>
        </div>
      </div>
    </div>
  );
};
