import { createInstance } from 'i18next';
import { describe, expect, it } from 'vitest';

import en from './en.json';
import zh from './zh.json';

const navigationLabels = {
  en: en.nav,
  zh: zh.nav,
};

describe('navigation translations', () => {
  it.each(Object.entries(navigationLabels))(
    'resolves every %s navigation key to visible copy',
    (language, labels) => {
      const i18n = createInstance();
      void i18n.init({
        lng: language,
        resources: { [language]: { translation: { nav: labels } } },
      });

      for (const key of Object.keys(labels)) {
        expect(i18n.t(`nav.${key}`)).not.toBe(`nav.${key}`);
      }
    },
  );

  it('keeps navigation keys available in both locales', () => {
    expect(Object.keys(zh.nav).sort()).toEqual(Object.keys(en.nav).sort());
  });
});
