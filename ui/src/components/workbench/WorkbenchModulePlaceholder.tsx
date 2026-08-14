import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

interface WorkbenchModulePlaceholderProps {
  icon: ReactNode;
  /** i18n key prefix (e.g. `workbench.modules.agents`). The component reads
   *  `${prefix}.title` and `${prefix}.description` plus the shared
   *  `workbench.modules.comingSoon` hint. */
  i18nPrefix: string;
}

// Shared placeholder used by all four capability modules and the inbox
// landing route while the real pages are staged in by later commits.
// Keeps the routes resolvable so the sidebar links never 404 and lets the
// shell layout get exercised end-to-end on day 1.
export const WorkbenchModulePlaceholder: React.FC<WorkbenchModulePlaceholderProps> = ({
  icon,
  i18nPrefix,
}) => {
  const { t } = useTranslation();

  return (
    <div className="mx-auto flex max-w-2xl flex-col items-center gap-5 py-16 text-center">
      <div className="flex size-14 items-center justify-center rounded-2xl border border-mint/30 bg-mint/[0.08] text-mint-ink shadow-[0_0_24px_-6px_rgba(91,255,160,0.5)]">
        {icon}
      </div>
      <div className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-mint-ink">
        {t(`${i18nPrefix}.eyebrow`, { defaultValue: t('workbench.modules.comingSoonEyebrow') })}
      </div>
      <h1 className="text-2xl font-bold text-foreground">{t(`${i18nPrefix}.title`)}</h1>
      <p className="text-sm leading-relaxed text-muted">{t(`${i18nPrefix}.description`)}</p>
      <p className="text-[12px] text-muted">{t('workbench.modules.comingSoon')}</p>
    </div>
  );
};
