// The open-vocabulary half of AC-19.
//
// contractLocaleKeys.test.ts can only cover keys a schema CLOSES (`enum` /
// `const`). The oauth flow's `instructions_key` / `error_key` and a migration
// item's `notes_key` are declared as free-form runtime keys, so no build-time
// check can know their membership and a new adapter rev is enough to send one no
// bundle carries. i18next then returns the key itself, which renders a machine
// string to a user as if it were a sentence — that is what this guards.
import { createInstance, type TFunction } from 'i18next';
import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { serverText } from './serverCopy';

const t = (lng: 'en' | 'zh'): TFunction => {
  const i18n = createInstance();
  void i18n.init({
    lng,
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
  return i18n.t;
};

describe('serverText', () => {
  it.each(['zh', 'en'] as const)('renders a key the bundle carries in %s', (lng) => {
    // The keys the backend actually emits today (native_oauth._INSTRUCTIONS_KEYS,
    // migration._CUSTOM_ENDPOINT_NOTE) must come through unchanged.
    expect(serverText(t(lng), 'settings.models.oauth.pasteCode.hint', 'settings.models.oauth.error.generic')).toBe(
      t(lng)('settings.models.oauth.pasteCode.hint'),
    );
    expect(serverText(t(lng), 'settings.models.source.customEndpoint')).toBe(
      t(lng)('settings.models.source.customEndpoint'),
    );
  });

  it.each(['zh', 'en'] as const)('falls back to the generic copy for an unknown key in %s', (lng) => {
    const text = serverText(t(lng), 'models.oauth.some.future.adapter.key', 'settings.models.oauth.error.generic');
    expect(text).toBe(t(lng)('settings.models.oauth.error.generic'));
    // The point of the whole helper: never the key.
    expect(text).not.toContain('models.oauth.some.future');
  });

  it('renders nothing for optional copy whose key is unknown', () => {
    // A migration row's secondary line has no generic substitute — dropping the
    // line is honest, printing the key is not.
    expect(serverText(t('zh'), 'models.migration.some.future.note')).toBeNull();
  });

  it('renders nothing when there is no key at all', () => {
    expect(serverText(t('zh'), null)).toBeNull();
    expect(serverText(t('zh'), undefined)).toBeNull();
  });

  it('still falls back when the key is absent but a fallback is given', () => {
    expect(serverText(t('en'), null, 'settings.models.oauth.error.generic')).toBe(
      t('en')('settings.models.oauth.error.generic'),
    );
  });
});
