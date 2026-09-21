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
 * storage degrades to the ordinary landing instead of throwing.
 *
 * A remembered section is checked against the routes this release actually
 * declares and against what this visitor may open, so neither a section retired
 * by a later release nor an owner's page an ordinary member inherited can land
 * anyone somewhere the rail would not offer them today.
 */
export const SETTINGS_LAST_SECTION_STORAGE_KEY = 'avibe.settings.last-section.v1';

type SectionStorage = Pick<Storage, 'getItem' | 'setItem'>;

function browserStorage(storage?: SectionStorage): SectionStorage | undefined {
  return storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined);
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
    browserStorage(storage)?.setItem(SETTINGS_LAST_SECTION_STORAGE_KEY, path);
  } catch {
    // View memory is best-effort in private browsing and restricted storage contexts.
  }
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
  // A path an older release wrote, or hand-edited storage: resume only what this
  // release still routes, rather than handing the surface its not-found page.
  if (!isApplicationRouteHref(remembered)) return SETTINGS_LANDING_PATH;
  // A remembered page can outlive the capability that made it readable.
  if (!canManageInstance && isOwnerOnlyPath(remembered)) return SETTINGS_LANDING_PATH;
  return remembered;
}
