import * as React from 'react';
import { useTranslation } from 'react-i18next';

import type { AddedTo, AdoptedBy } from './types';

export const AdoptionNote: React.FC<{
  addedTo: AddedTo[] | null;
  adoptedBy: AdoptedBy[] | null;
}> = ({ addedTo, adoptedBy }) => {
  const { t } = useTranslation();
  if (!addedTo || !adoptedBy) return null;
  if (addedTo.length === 0) {
    return <p className="text-[12px] leading-relaxed text-muted">{t('settings.models.adoption.none')}</p>;
  }
  const backendName = (backend: string) =>
    t(`settings.models.backends.${backend}`, { defaultValue: backend }) as string;
  const locations = addedTo.map((entry) =>
    t('settings.models.adoption.route', {
      backend: backendName(entry.backend),
      model: entry.menu_model,
      position: entry.position,
    }),
  );
  return (
    <p className="text-[12px] leading-relaxed text-muted">
      {t('settings.models.adoption.enabled', { list: locations.join(' · ') })}
    </p>
  );
};

export default AdoptionNote;
