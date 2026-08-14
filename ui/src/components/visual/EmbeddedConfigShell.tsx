import React from 'react';
import clsx from 'clsx';
import { Check, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '../ui/button';

interface EmbeddedConfigShellProps {
  total: number;
  completed: number;
  canApply: boolean;
  applying?: boolean;
  onApply: () => void;
  onCancel: () => void;
  children: React.ReactNode;
}

// Wraps a wizard *Config body inside the settings-page collapse card. Drops the
// wizard's WizardCard chrome and replaces Back/Continue with Cancel/Apply.
export const EmbeddedConfigShell: React.FC<EmbeddedConfigShellProps> = ({
  total,
  completed,
  canApply,
  applying,
  onApply,
  onCancel,
  children,
}) => {
  const { t } = useTranslation();
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-end gap-2">
        <span className="font-mono text-[11px] font-bold uppercase tracking-[0.16em] text-mint-ink">
          {completed} / {total}
        </span>
        <div className="flex gap-1">
          {Array.from({ length: total }, (_, i) => (
            <span
              key={i}
              className={clsx(
                'h-1 w-4 rounded-full',
                i < completed ? 'bg-mint shadow-[0_0_8px_rgba(91,255,160,0.6)]' : 'bg-foreground/[0.08]'
              )}
            />
          ))}
        </div>
      </div>
      <div className="flex flex-col gap-3">{children}</div>
      <div className="flex items-center justify-end gap-2 border-t border-border pt-3">
        <Button type="button" variant="secondary" size="xs" onClick={onCancel} disabled={applying}>
          {t('common.cancel')}
        </Button>
        <Button
          type="button"
          variant="brand"
          size="xs"
          onClick={onApply}
          disabled={!canApply || applying}
        >
          {applying ? <RefreshCw size={12} className="animate-spin" /> : <Check size={12} />}
          {t('platform.apply')}
        </Button>
      </div>
    </div>
  );
};
