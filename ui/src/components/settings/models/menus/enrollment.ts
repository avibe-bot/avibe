// What accepting a menu edit ALSO did to the backend's source order.
//
// api.md → "Mapping and menu enrollment": a mapping or open-menu target is
// accepted only when its selected source is enrolled in the post-mutation
// effective order, and acceptance auto-appends that source — 「the confirm step
// must surface the append」. The user ticked a model; the server also changed
// which accounts that backend will spend, and nothing on screen said so.
//
// Read from the echo, never predicted. The server picks the first supplier in ITS
// recommended order (`_enroll_target_sources`), so a client that computed the
// append before committing would be re-deriving a server rule from a snapshot
// that may already be stale — and would name the wrong source the first time the
// two disagree. Both orders below are `effective_source_order` as the payload
// reports it, for either policy, so the diff is one subtraction and no rule.
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '@/context/ToastContext';
import type { AgentBackend, AgentSupply, Source } from '../types';
import { useCompactSourceLabel } from './sourceLabel';

/**
 * The ids the commit added to the order, in the order the server put them.
 *
 * `[]` when nothing was appended AND when either side is missing, because the two
 * are the same statement here: with no baseline every id looks new, and 「保存后
 * 自动加入了全部来源」 is the one sentence this must never produce. Both callers
 * only speak when the list is non-empty, so silence needs no second member.
 *
 * WHAT `before` HAS TO BE: the order the server held when this write began. It is
 * the page's own Agent list, so a `sources.order` PUT that lands in the window
 * between the page reading it and this write returning would show up in the diff
 * and be reported as this write's doing. The one path that could reach that window
 * is closed where it opens rather than compensated for here: 来源顺序's hand-off to
 * these drawers is disabled until its own write has been read back
 * (`SourceOrderDrawer.persist`), so the baseline handed over is never mid-write.
 * The general case — any order write from outside this page's read cycle — is not
 * client-answerable, and its fix is the mutation response naming its own appends
 * (`enrolled_source_ids`), server-side and escalated rather than approximated here.
 */
export function enrolledByCommit(
  before: Pick<AgentSupply, 'sources'>,
  after: Pick<AgentSupply, 'sources'>,
): string[] {
  const was = before.sources?.order;
  const now = after.sources?.order;
  if (!was || !now) return [];
  const enrolled = new Set(was);
  return now.filter((id) => !enrolled.has(id));
}

/**
 * Both drawers' 完成, in one place: diff the echo against what was on screen and
 * say what the save also enrolled.
 *
 * Shared rather than written twice for the reason `AdoptionNote` was: two dialogs
 * reporting one server behaviour in their own words is how they end up saying
 * different things about the same event. The append is per-commit and identical
 * for a fixed menu and an open one — only the backend's name differs.
 *
 * Silent when nothing was appended, which is the ordinary case: a ticked model
 * whose supplier is already in the order changes only the menu, and a toast that
 * fires every time teaches the user to dismiss the one that matters. It says
 * nothing about the policy either — under `follow` the effective order is
 * exhaustive over eligible sources, so an append means the order was already
 * `custom` and no recommendation was lost.
 */
export function useAnnounceEnrollment(
  backend: AgentBackend,
  sources: readonly Source[],
): (before: Pick<AgentSupply, 'sources'>, after: Pick<AgentSupply, 'sources'>) => void {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const compactLabel = useCompactSourceLabel();
  return React.useCallback(
    (before, after) => {
      const enrolled = enrolledByCommit(before, after);
      if (enrolled.length === 0) return;
      const byId = new Map(sources.map((s) => [s.id, s]));
      const names = enrolled.map((id) => {
        const source = byId.get(id);
        // An id the inventory on screen does not carry is still an enrollment
        // that happened. Naming it by id is worse copy and a true sentence;
        // dropping it would silently under-report what the save did.
        return source ? compactLabel(source) : id;
      });
      showToast(
        t('settings.models.menus.enrolled', {
          sources: names.join(' · '),
          backend: t(`settings.models.backends.${backend}`, { defaultValue: backend }) as string,
        }) as string,
        'success',
      );
    },
    [backend, compactLabel, showToast, sources, t],
  );
}
