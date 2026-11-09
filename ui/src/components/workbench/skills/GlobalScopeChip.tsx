import { Globe } from 'lucide-react';
import { useTranslation } from 'react-i18next';

export function GlobalScopeChip() {
  const { t } = useTranslation();
  return <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-edge px-1.5 font-mono text-[9px] font-bold uppercase text-muted bg-field">
    <Globe className="size-2.5" />
    {t('skills.globalBadge')}
  </span>;
}
