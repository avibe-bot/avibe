import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

import en from './en.json';
import zh from './zh.json';
import { desktopAccountCopy } from './desktop';
import { forDevicePlatform } from './device';
import { I18N_OPTIONS } from './options';

// Both languages, ready on import: the design canvas, the bundled mobile
// onboarding, the Desktop account document and the tests, with the account
// document's own copy (./desktop). The Web UI entry loads one language on
// demand instead, without that copy (./app.ts).
const resources = {
  en: { translation: forDevicePlatform({ ...en, ...desktopAccountCopy.en }) },
  zh: { translation: forDevicePlatform({ ...zh, ...desktopAccountCopy.zh }) },
};

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({ ...I18N_OPTIONS, resources });

export default i18n;
