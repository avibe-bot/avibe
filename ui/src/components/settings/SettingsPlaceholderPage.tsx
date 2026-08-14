import React from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, Sparkles } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { SettingsPageShell } from './SettingsPageShell';

export const SettingsPlaceholderPage: React.FC<{
  tab: 'platforms' | 'backends' | 'messaging';
  titleKey: string;
  subtitleKey: string;
}> = ({ tab, titleKey, subtitleKey }) => {
  const { t } = useTranslation();

  return (
    <SettingsPageShell activeTab={tab} title={t(titleKey)} subtitle={t(subtitleKey)}>
      <div className="flex flex-col gap-3 rounded-xl border border-border bg-background px-6 py-5">
        <div className="flex items-center gap-2 text-[14px] font-semibold text-foreground">
          <Sparkles className="size-4 text-cyan-ink" />
          {t('settings.inProgressTitle')}
        </div>
        <p className="text-[12px] leading-relaxed text-muted">{t('settings.inProgressBody')}</p>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <span className="text-[12px] text-muted">{t('settings.inProgressHint')}</span>
          <Link
            to="/setup"
            className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-foreground/[0.04] px-3 py-2 text-[12px] font-medium text-foreground transition hover:border-border-strong"
          >
            {t('settings.openSetup')}
            <ArrowRight className="size-3.5" strokeWidth={2.25} />
          </Link>
        </div>
      </div>
    </SettingsPageShell>
  );
};
