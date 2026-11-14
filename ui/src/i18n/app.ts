import i18n, { type BackendModule } from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

import { forDevicePlatform } from './device';
import { APP_LANGUAGES, I18N_OPTIONS, type AppLanguage } from './options';

// The Web UI's copy, one language at a time (docs/plans/web-ui-first-load.md):
// each locale is its own chunk, and the first screen downloads only the one it
// shows. index.html starts that download alongside the entry
// (scripts/firstScreenPreload.ts repeats the detection), so waiting for it
// before the first render costs no extra round trip. A later switch (the
// language picker, or the instance's saved language) keeps the current
// language on screen until the other one's copy has arrived.
const BUNDLES: Record<AppLanguage, () => Promise<{ default: object }>> = {
  en: () => import('./en.json'),
  zh: () => import('./zh.json'),
};

const isAppLanguage = (code: string): code is AppLanguage => (APP_LANGUAGES as readonly string[]).includes(code);

const lazyBundles: BackendModule = {
  type: 'backend',
  init() {},
  read(language, _namespace, callback) {
    // A locale's copy lives under its base code. A regional or unsupported code
    // ('zh-CN', 'fr') resolves through that base, or through English, so it
    // reads nothing of its own.
    if (!isAppLanguage(language)) {
      callback(null, {});
      return;
    }
    BUNDLES[language]().then(
      (bundle) => callback(null, forDevicePlatform(bundle.default)),
      (error: unknown) => callback(error instanceof Error ? error : String(error), false),
    );
  },
};

/** Settles when the detected language's copy is in place (or has failed to load): render after it. */
export const i18nReady: Promise<void> = i18n
  .use(lazyBundles)
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    ...I18N_OPTIONS,
    // Chinese reads no English fallback, so a Chinese page never downloads the
    // English copy: both locales carry exactly the same keys
    // (src/i18n/keyCoverage.test.ts resolves every key the app asks for in each
    // locale with `fallbackLng: false`).
    fallbackLng: { zh: [], default: ['en'] },
    // Start the download now rather than on the next tick.
    initAsync: false,
  })
  .then(
    () => undefined,
    () => undefined,
  );

export default i18n;
