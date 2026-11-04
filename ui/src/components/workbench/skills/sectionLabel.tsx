import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

/** The section header both project views share: icon · LABEL · hint. */
export function SectionLabel({ icon, label, hint }: { icon: ReactNode; label: string; hint?: string }) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-2 px-1">
      {icon}
      <span className="font-mono text-[10.5px] font-bold uppercase tracking-[0.1em] text-muted">{label}</span>
      {hint ? <span className="font-mono text-[10px] text-muted">{t('skills.sectionHint', { hint })}</span> : null}
    </div>
  );
}
