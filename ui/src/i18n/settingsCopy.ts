import i18n from 'i18next';

import { SETTINGS_COPY_NAMESPACE } from './options';

let pending: Promise<void> | null = null;

/**
 * Loads the copy only the Settings pages (with the Model Hub) draw: one chunk
 * per language, off the first screen
 * (scripts/localeSurfaces.ts). Each of those pages loads with it
 * (src/routePages.ts, src/App.tsx), so opening one, or the idle prefetch that
 * fetches it, fetches its copy too, and the page draws only once both are here.
 * A later language switch fetches the other language's copy before it changes
 * (i18next loads every namespace in use).
 *
 * Copy that fails to load rejects with the fetch's own error, so the page shows
 * the error card whose reload recovers it, as a page whose code failed to
 * download does; like that failure, it stands until the page reloads. The
 * two-language instance (./index.ts: the design canvas, the tests) holds every
 * key in `translation` and loads nothing here.
 */
export function loadSettingsCopy(): Promise<void> {
  pending ??= (async () => {
    // A language switch already under way loads only the namespaces in use when
    // it began, so the language it lands on gets this copy here too.
    // (i18next keeps the target in `isLanguageChangingTo`, untyped, and hands
    // either callback the load's errors; src/i18n/app.test.ts covers the switch.)
    const switching = (i18n as typeof i18n & { isLanguageChangingTo?: string }).isLanguageChangingTo;
    await Promise.all([
      settle((done) => i18n.loadNamespaces(SETTINGS_COPY_NAMESPACE, done)),
      switching ? settle((done) => i18n.reloadResources(switching, SETTINGS_COPY_NAMESPACE, done as () => void)) : undefined,
    ]);
  })();
  return pending;
}

/** i18next reports a failed load as a list of errors: reject with the first, the fetch's own. */
function settle(load: (done: (errors: unknown) => void) => unknown): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    void load((errors) => {
      const error = Array.isArray(errors) ? errors[0] : errors;
      if (error) reject(error instanceof Error ? error : new Error(String(error)));
      else resolve();
    });
  });
}
