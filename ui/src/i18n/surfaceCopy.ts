import i18n from 'i18next';

const pending = new Map<string, Promise<void>>();

/**
 * Loads the copy one surface draws: the part of the catalog only its lazy pages
 * draw (the Settings pages with the Model Hub, Agents, Skills, Harness, the
 * setup wizard), one chunk per language, off the first screen
 * (scripts/localeSurfaces.ts). Each of those pages loads with it, through
 * `withCopy` in src/routePages.ts and src/App.tsx, so opening one, or the idle
 * prefetch that fetches it, fetches its copy too, and the page draws only once
 * both are here. A later language switch fetches the other language's copy
 * before it changes (i18next loads every namespace in use).
 *
 * Copy that fails to load rejects with the fetch's own error, so the page shows
 * the error card whose reload recovers it, as a page whose code failed to
 * download does; like that failure, it stands until the page reloads. The
 * two-language instance (./index.ts: the design canvas, the tests) holds every
 * key in `translation` and loads nothing here; neither does a surface whose copy
 * all stays with the first screen.
 */
export function loadSurfaceCopy(surface: string): Promise<void> {
  let loading = pending.get(surface);
  if (!loading) {
    loading = (async () => {
      // A language switch already under way loads only the namespaces in use
      // when it began, so the language it lands on gets this copy here too.
      // (i18next keeps the target in `isLanguageChangingTo`, untyped, and hands
      // either callback the load's errors; src/i18n/app.test.ts covers the switch.)
      const switching = (i18n as typeof i18n & { isLanguageChangingTo?: string }).isLanguageChangingTo;
      await Promise.all([
        settle((done) => i18n.loadNamespaces(surface, done)),
        switching ? settle((done) => i18n.reloadResources(switching, surface, done as () => void)) : undefined,
      ]);
    })();
    pending.set(surface, loading);
  }
  return loading;
}

/** Loads a lazy page together with its surface's copy (scripts/localeSurfaces.ts checks every loader). */
export function withCopy<T>(surface: string, load: () => Promise<T>): Promise<T> {
  return Promise.all([load(), loadSurfaceCopy(surface)]).then(([module]) => module);
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
