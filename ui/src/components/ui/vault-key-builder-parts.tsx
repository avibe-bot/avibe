import { Download, Sparkles } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { InfoHint } from './info-hint';
import { SegmentedRadio } from './segmented';

export type KeySource = 'generate' | 'import';

/** The Generate / Import choice of a signing-key builder, drawn as the segmented tabs every other
 *  choice in the Add dialog uses, so it reads as tabs (owner, 2026-11-12). */
export const KeySourceTabs: React.FC<{
  value: KeySource;
  onChange: (next: KeySource) => void;
  disabled?: boolean;
}> = ({ value, onChange, disabled }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[13px] font-medium text-foreground">{t('vaults.dialog.keySource')}</span>
      <SegmentedRadio<KeySource>
        value={value}
        onChange={onChange}
        disabled={disabled}
        ariaLabel={t('vaults.dialog.keySource')}
        tone="muted"
        options={[
          { id: 'generate', label: t('vaults.dialog.signingGenerate'), icon: Sparkles },
          { id: 'import', label: t('vaults.dialog.signingImport'), icon: Download },
        ]}
      />
    </div>
  );
};

/** One short line a reader needs, with the longer explanation behind an ⓘ beside it. */
export const HintLine: React.FC<{
  text: React.ReactNode;
  info?: React.ReactNode;
  className?: string;
}> = ({ text, info, className }) => {
  const { t } = useTranslation();
  return (
    <span className={cn('flex items-start gap-1.5 text-xs leading-snug text-muted', className)}>
      <span className="min-w-0">{text}</span>
      {info ? <InfoHint content={info} label={t('vaults.dialog.moreInfo')} hover className="mt-px" /> : null}
    </span>
  );
};
