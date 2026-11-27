import type { InitOptions } from 'i18next';

/** The languages the app ships copy for (`en.json`, `zh.json`), in the order its pickers list them. */
export const APP_LANGUAGES = ['en', 'zh'] as const;
export type AppLanguage = (typeof APP_LANGUAGES)[number];

/**
 * The app language a browser or system language reads as: every Chinese variant
 * (`zh`, `zh-CN`, `zh-TW`, `zh-Hant-HK` …) is Chinese, everything else English.
 */
export const appLanguageFor = (code: string): AppLanguage =>
  code.toLowerCase().split(/[-_]/)[0] === 'zh' ? 'zh' : 'en';

/** Where this browser keeps a language the reader picked (the detector's `localStorage` key). */
export const LANGUAGE_CHOICE_KEY = 'i18nextLng';

/**
 * Keeps a language the reader picked for this browser. Only a pick is kept: a
 * reader who never picked one is not written down as English or Chinese, so the
 * page keeps following the browser's preferred language.
 */
export const rememberLanguageChoice = (code: string): void => {
  try {
    window.localStorage.setItem(LANGUAGE_CHOICE_KEY, code);
  } catch {
    // Storage refused (private mode, quota): the pick still stands for this page.
  }
};

/** What every i18next instance of the app is initialized with, wherever its copy comes from. */
export const I18N_OPTIONS = {
  fallbackLng: 'en',
  interpolation: {
    escapeValue: false, // React already escapes
  },
  detection: {
    // A kept pick first, then the environment's preferred language.
    order: ['localStorage', 'navigator'],
    lookupLocalStorage: LANGUAGE_CHOICE_KEY,
    // Nothing is cached by the detector: i18next would otherwise store the
    // detected (or any later) language as if the reader had chosen it.
    // `rememberLanguageChoice` stores a real pick.
    caches: [],
    convertDetectedLanguage: appLanguageFor,
  },
} satisfies InitOptions;
