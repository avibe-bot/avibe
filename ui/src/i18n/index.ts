import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

import en from './en.json';
import zh from './zh.json';
import { forDevicePlatform } from './device';
import { I18N_OPTIONS } from './options';

// Both languages, ready on import: the design canvas, the bundled mobile
// onboarding and the tests. The Web UI entry loads one language on demand
// instead (./app.ts).
const resources = {
  en: { translation: forDevicePlatform(en) },
  zh: { translation: forDevicePlatform(zh) },
};

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({ ...I18N_OPTIONS, resources });

export default i18n;
