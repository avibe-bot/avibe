import { useSyncExternalStore } from 'react';

import { isOwnerOnlyPath, SETTINGS_LANDING_PATH } from './adminNavigation';
import { isApplicationRouteHref } from './applicationRoutes';

/**
 * Which Settings section this device opened last.
 *
 * Settings is a place people return to, not a page they read once: whoever
 * spent the last visit on Backends is far likelier to want Backends again than
 * General, and re-selecting it on every entry is work the surface can do for
 * them. Only the *section* is remembered — the rail row that was selected — so
 * a detail page inside one (a backend's provider page, the log viewer) resumes
 * at the row the rail can actually show as current.
 *
 * A per-person, per-device view preference of the same tier as the theme and
 * the menu placement, so it lives in localStorage rather than in instance
 * config, where one member's last visit would move every other member's
 * landing. Storage conventions mirror settingsMenuPlacement: versioned
 * `avibe.*` key, injectable storage for tests, best-effort try/catch so blocked
 * storage degrades to the ordinary landing instead of throwing, and a change
 * event so a link rendered elsewhere can follow the rail.
 *
 * What may be resumed is a question about the route, not about the feature
 * flags: the value has to name a route this release still declares, and a
 * section this visitor's capabilities allow. A feature flag decides which rows
 * the rail advertises, not which pages a person may stand on. Capability-gated
 * pages handle their own redirects after navigation.
 * `pwaRouteMemory` restores the very same paths on the very same terms.
 */
export const SETTINGS_LAST_SECTION_STORAGE_KEY = 'avibe.settings.last-section.v1';
export const SETTINGS_LAST_SECTION_CHANGED_EVENT = 'avibe:settings-last-section-changed';

type SectionStorage = Pick<Storage, 'getItem' | 'setItem'>;

function browserStorage(storage?: SectionStorage): SectionStorage | undefined {
  return storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined);
}

function announceSectionChange(): void {
  // `storage` events only reach *other* tabs; this covers the entry controls
  // rendered beside the rail in this one.
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(SETTINGS_LAST_SECTION_CHANGED_EVENT));
  }
}

export function readLastSettingsSection(storage?: SectionStorage): string | null {
  try {
    return browserStorage(storage)?.getItem(SETTINGS_LAST_SECTION_STORAGE_KEY) ?? null;
  } catch {
    return null;
  }
}

export function writeLastSettingsSection(path: string, storage?: SectionStorage): void {
  try {
    const target = browserStorage(storage);
    // Selecting the row you are already on is not a change, and announcing one
    // would re-render every subscriber on each navigation inside a section.
    if (!target || target.getItem(SETTINGS_LAST_SECTION_STORAGE_KEY) === path) return;
    target.setItem(SETTINGS_LAST_SECTION_STORAGE_KEY, path);
  } catch {
    // View memory is best-effort in private browsing and restricted storage contexts.
    return;
  }
  announceSectionChange();
}

/**
 * Where an ordinary Settings entry opens: the section this device was left on,
 * or the landing page when there is nothing to resume.
 */
export function settingsResumePath(
  canManageInstance: boolean,
  storage?: SectionStorage,
): string {
  const remembered = readLastSettingsSection(storage);
  if (!remembered || !remembered.startsWith('/settings/')) return SETTINGS_LANDING_PATH;
  // Only a bare section path is ever recorded, so a query or fragment means a
  // value this surface did not write. Rejecting it keeps both checks below
  // reading the same string: a suffix that route matching strips but the
  // capability check does not would otherwise walk an owner-only section past
  // the guard and hand a member a destination their route guard then bounces.
  if (/[?#]/.test(remembered)) return SETTINGS_LANDING_PATH;
  // A path an older release wrote, or hand-edited storage: resume only what this
  // release still routes, rather than handing the surface its not-found page.
  if (!isApplicationRouteHref(remembered)) return SETTINGS_LANDING_PATH;
  // A remembered page can outlive the capability that made it readable.
  if (!canManageInstance && isOwnerOnlyPath(remembered)) return SETTINGS_LANDING_PATH;
  return remembered;
}

function subscribeToLastSettingsSection(listener: () => void): () => void {
  if (typeof window === 'undefined') return () => undefined;
  const onStorage = (event: StorageEvent) => {
    if (event.key === SETTINGS_LAST_SECTION_STORAGE_KEY) listener();
  };
  window.addEventListener('storage', onStorage);
  window.addEventListener(SETTINGS_LAST_SECTION_CHANGED_EVENT, listener);
  return () => {
    window.removeEventListener('storage', onStorage);
    window.removeEventListener(SETTINGS_LAST_SECTION_CHANGED_EVENT, listener);
  };
}

/**
 * The same answer as `settingsResumePath`, kept current for a control that
 * displays it. An entry link renders long before it is clicked, and the section
 * can move under it meanwhile: the same person can have this origin open in two
 * tabs, and the one that is not in Settings has to follow the one that is, or
 * it keeps offering — and showing in its href — a section the rail left.
 */
export function useSettingsResumePath(canManageInstance: boolean): string {
  // The snapshot is a plain string, so identity comparison is value comparison
  // and no caching layer is needed between reads.
  return useSyncExternalStore(
    subscribeToLastSettingsSection,
    () => settingsResumePath(canManageInstance),
    () => SETTINGS_LANDING_PATH,
  );
}
