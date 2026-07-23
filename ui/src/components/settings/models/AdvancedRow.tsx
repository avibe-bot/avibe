// The single 高级 placeholder row (frame 01r): cross-vendor auto-substitution
// (default-off) · request log · diagnostics. The detail surfaces are future
// work; the row explains itself until they land.
import * as React from 'react';
import { ChevronRight, SlidersHorizontal } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useToast } from '@/context/ToastContext';

export const AdvancedRow: React.FC = () => {
  const { t } = useTranslation();
  const { showToast } = useToast();

  return (
    <button
      type="button"
      onClick={() => showToast(t('settings.models.advanced.comingSoon') as string, 'warning')}
      className="flex w-full items-center gap-3 rounded-xl border border-border bg-background px-5 py-4 text-left transition-colors hover:border-border-strong"
    >
      <SlidersHorizontal className="size-4 shrink-0 text-muted" />
      <span className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="text-[14px] font-medium text-foreground">{t('settings.models.advanced.title')}</span>
        <span className="text-[12px] text-muted">{t('settings.models.advanced.detail')}</span>
      </span>
      <ChevronRight className="size-4 shrink-0 text-muted" />
    </button>
  );
};
