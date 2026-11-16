import type { TranslationPrefix } from '@/i18n/types';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

interface WorkbenchModulePlaceholderProps {
  icon: ReactNode;
  /** i18n key prefix (e.g. `workbench.modules.agents`). The component reads
   *  `${prefix}.title` and `${prefix}.description`. */
  i18nPrefix: Extract<TranslationPrefix<'title'> & TranslationPrefix<'description'>, `workbench.modules.${string}`>;
}

// Not mounted by any route; kept as the consuming type for the translation
// prefix contract in `i18n/translationContract.test.ts`.
export const WorkbenchModulePlaceholder: React.FC<WorkbenchModulePlaceholderProps> = ({
  icon,
  i18nPrefix,
}) => {
  const { t } = useTranslation();

  return (
    <div className="mx-auto flex max-w-2xl flex-col items-center gap-5 py-16 text-center">
      <div className="flex size-14 items-center justify-center rounded-2xl border border-mint/30 bg-mint/[0.08] text-mint-ink shadow-decor-md-mint">
        {icon}
      </div>
      <h1 className="text-2xl font-bold text-foreground">{t(`${i18nPrefix}.title`)}</h1>
      <p className="text-sm leading-relaxed text-muted">{t(`${i18nPrefix}.description`)}</p>
    </div>
  );
};
