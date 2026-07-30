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
//
// Which of those two answers can be given is `adoptionVerdict`'s call, not this
// component's: 「who did not」 is only answerable from the server's own
// eligible-but-skipped complement, and a response that omits it still gets the
// sentence the note can prove rather than a guess at the missing half.
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { adoptionVerdict } from './sufficiency';
import type { AdoptedBy, SkippedBy } from './types';

export const AdoptionNote: React.FC<{ adoptedBy: AdoptedBy[] | null; skippedBy?: SkippedBy[] | null }> = ({
  adoptedBy,
  skippedBy,
}) => {
  const { t } = useTranslation();
  // No result at all — a creation that reported nothing about adoption. Not the same
  // as an empty result, and the only case with nothing true to say.
  if (!adoptedBy) return null;
  const verdict = adoptionVerdict(adoptedBy, skippedBy);
  const backendName = (backend: string) =>
    t(`settings.models.backends.${backend}`, { defaultValue: backend }) as string;

  if (verdict.kind === 'adopted_none' || verdict.kind === 'skipped_all') {
    // Not an error — the source exists and is healthy. It is a pointer to the one
    // action that makes it serve traffic, on the page the user is already on. And
    // it points AT the orders whenever the server named them: 「nobody took it」 and
    // 「these hand-picked orders left it out」 want the same edit in two very
    // different amounts of hunting.
    return (
      <p className="text-[12px] leading-relaxed text-muted">
        {verdict.kind === 'skipped_all'
          ? t('settings.models.adoption.noneSkipped', {
              skipped: verdict.backends.map(backendName).join(' · '),
            })
          : t('settings.models.adoption.none')}
      </p>
    );
  }

  // Position order, not response order: 「第 1 位」 before 「第 3 位」 reads as a
  // ranking, which is what it is.
  const list = [...adoptedBy]
    .sort((a, b) => a.position - b.position)
    .map((a) =>
      t('settings.models.adoption.entry', {
        name: backendName(a.backend),
        position: a.position,
      }),
    )
    // ' · ' is this page's separator everywhere else (source sub-lines, the
    // supply tooltip, the agent card's amber line) — locale-neutral on purpose.
    .join(' · ');

  return (
    <p className="text-[12px] leading-relaxed text-muted">
      {verdict.kind === 'partly_skipped'
        ? t('settings.models.adoption.partlySkipped', {
            list,
            skipped: verdict.backends.map(backendName).join(' · '),
          })
        : // `covered` and `indeterminate` render the same sentence, and it is the
          // sentence that is TRUE of both: these backends took it. Only the omission
          // needs the server half, so only the omission waits for it.
          t('settings.models.adoption.enabled', { list })}
    </p>
  );
};

export default AdoptionNote;
