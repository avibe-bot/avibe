import type { InitOptions } from 'i18next';

/** The languages the app ships copy for (`en.json`, `zh.json`), in the order its pickers list them. */
export const APP_LANGUAGES = ['en', 'zh'] as const;
export type AppLanguage = (typeof APP_LANGUAGES)[number];

/** What every i18next instance of the app is initialized with, wherever its copy comes from. */
export const I18N_OPTIONS = {
  fallbackLng: 'en',
  interpolation: {
    escapeValue: false, // React already escapes
  },
  detection: {
    order: ['localStorage', 'navigator'],
    caches: ['localStorage'],
  },
} satisfies InitOptions;
