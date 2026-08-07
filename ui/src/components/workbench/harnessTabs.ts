// Which Harness tab is on screen, and what an empty one says. The tab set and
// the ``?tab=`` mapping live together so removing a tab later cannot strand its
// old links, and the copy keys are asserted without rendering the page
// (see HarnessPage.test.tsx).

export type TabKey = 'tasks' | 'watches' | 'runs';

export const TAB_ORDER: TabKey[] = ['tasks', 'watches', 'runs'];
export const DEFAULT_TAB: TabKey = 'tasks';

// Which tab a ``?tab=`` param opens. Anything that is not a tab opens the
// default rather than selecting nothing — ``?tab=webhooks`` still arrives from
// links and bookmarks made before the Webhooks tab was removed, and must land
// on Tasks, not on an empty page with no tab lit.
//
// One function rather than a guard here and a ``useState`` initializer three
// hundred lines away, so removing a tab later cannot strand its old links.
export function harnessTabFromParam(param: string | null | undefined): TabKey {
  return (TAB_ORDER as string[]).includes(param ?? '') ? (param as TabKey) : DEFAULT_TAB;
}

export function harnessEmptyStateKey(kind: TabKey, hasStoredRows: boolean): string {
  if (!hasStoredRows) {
    return kind === 'tasks' ? 'harness.emptyTasks' : kind === 'watches' ? 'harness.emptyWatches' : 'harness.emptyRuns';
  }
  return kind === 'tasks' ? 'harness.noTaskMatches' : kind === 'watches' ? 'harness.noWatchMatches' : 'harness.noRunMatches';
}
