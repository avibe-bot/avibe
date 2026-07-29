// The 「so what now?」 line of both creation dialogs (api.md → `adopted_by`).
//
// A new credential is not automatically usable: only `follow` backends take it in,
// and only where their eligibility admits it. So the two answers a user needs
// before the dialog closes are 「哪些 Agent 已经用上了，排第几」 and 「谁没有」 —
// and the second one is the whole reason this exists, because a `custom` backend is
// simply ABSENT from the array. Reading nothing there as 「fine, nothing to say」 is
// how a connected key silently never serves a turn.
//
// Shared rather than written twice: an API key and a subscription differ in how they
// are added and in nothing at all afterwards, and the previous per-dialog success
// copy is exactly where the two drifted into saying different things.
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import type { AdoptedBy } from './types';

export const AdoptionNote: React.FC<{ adoptedBy: AdoptedBy[] }> = ({ adoptedBy }) => {
  const { t } = useTranslation();

  if (adoptedBy.length === 0) {
    // Not an error — the source exists and is healthy. It is a pointer to the one
    // action that makes it serve traffic, on the page the user is already on.
    return <p className="text-[12px] leading-relaxed text-muted">{t('settings.models.adoption.none')}</p>;
  }

  // Position order, not response order: 「第 1 位」 before 「第 3 位」 reads as a
  // ranking, which is what it is.
  const list = [...adoptedBy]
    .sort((a, b) => a.position - b.position)
    .map((a) =>
      t('settings.models.adoption.entry', {
        name: t(`settings.models.backends.${a.backend}`, { defaultValue: a.backend }),
        position: a.position,
      }),
    )
    // ' · ' is this page's separator everywhere else (source sub-lines, the
    // supply tooltip, the agent card's amber line) — locale-neutral on purpose.
    .join(' · ');

  return (
    <p className="text-[12px] leading-relaxed text-muted">
      {t('settings.models.adoption.enabled', { list })}
    </p>
  );
};

export default AdoptionNote;
