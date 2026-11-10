import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { cn } from '@/lib/utils';
import { BrandLogo } from './BrandLogo';
import { ProgressBar } from './ProgressBar';
import { LanguageSwitcher } from '@/components/LanguageSwitcher';

interface WizardChromeProps {
  current: number;
  total: number;
  /** Show the segmented progress rail. Hidden on the welcome step. */
  showProgress?: boolean;
  /** Allow skipping to summary; hidden on the first / last steps. */
  onSkip?: () => void;
  /** Step counter label. Defaults to "Step X of Y". */
  counterLabel?: React.ReactNode;
  className?: string;
}

// Top bar + progress rail of the earlier multi-step wizard.
export const WizardChrome: React.FC<WizardChromeProps> = ({
  current,
  total,
  showProgress = true,
  onSkip,
  counterLabel,
  className,
}) => {
  const { t } = useTranslation();
  const safeTotal = Math.max(1, total);
  return (
    <div className={cn('mx-auto w-full max-w-[1280px]', className)}>
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3">
          <BrandLogo size={40} />
          <div className="flex flex-col">
            <span className="text-[14px] font-semibold leading-tight text-foreground">
              {t('wizard.title')}
            </span>
            {showProgress && (
              <span className="font-mono text-[11px] uppercase tracking-[0.16em] text-muted">
                {counterLabel ?? `${t('common.step')} ${Math.min(current + 1, safeTotal)} / ${safeTotal}`}
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <LanguageSwitcher />
          {onSkip && (
            <button
              type="button"
              onClick={onSkip}
              className="rounded-md border border-edge px-3 py-1.5 text-xs font-medium text-muted transition-colors hover:text-foreground bg-field hover:bg-field-hover active:bg-field-hover"
            >
              {t('common.skip')}
            </button>
          )}
        </div>
      </div>
      {showProgress && (
        <ProgressBar
          segments={safeTotal}
          current={current}
          className="mt-6"
        />
      )}
    </div>
  );
};
