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
import { oauthFailureKey, serverText } from './serverCopy';

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

describe('oauthFailureKey', () => {
  // The codes raised while MATERIALIZING the terminal flow. Listed here, not
  // imported: the test's job is to fail when the module's list and the contract's
  // POST /sources errors drift apart, which a shared constant cannot do.
  const MATERIALIZE = ['discovery_failed', 'migration_item_conflict'] as const;

  it.each(MATERIALIZE)('names the right object for %s', (code) => {
    // The whole reason the journey is a required argument: a connect has no
    // Source yet, a reauth has one the server just cleared and marked unavailable.
    expect(oauthFailureKey(code, 'connect')).toBe('settings.models.oauth.error.finalize');
    expect(oauthFailureKey(code, 'reauth')).toBe('settings.models.oauth.error.finalizeReauth');
  });

  it.each(['connect', 'reauth'] as const)(
    'refuses to read an engine outage as a finished sign-in in a %s',
    (journey) => {
      // `engine_down` is the engine-outage catch-all, not a phase: every
      // `_oauth_call` maps EngineUnavailableError (and any unexpected exception)
      // onto it, and `_oauth_status` is one of its callers — so a poll can carry
      // it while the flow is still pending, with nothing authorized and nothing
      // materialized. Claiming 「已重新登录，但…」 there is unverifiable by the user.
      expect(oauthFailureKey('engine_down', journey)).toBe('settings.models.oauth.error.generic');
      // And the generic line rather than one naming the engine, because the same
      // code covers NativeOAuthUnavailableError, where 「中枢没有响应」 is false.
      expect(oauthFailureKey('engine_down', journey)).not.toBe(
        'settings.models.oauth.error.finalizeReauth',
      );
    },
  );

  it.each(['connect', 'reauth'] as const)('degrades an unrecognized code to the generic line in a %s', (journey) => {
    expect(oauthFailureKey('some_future_code', journey)).toBe('settings.models.oauth.error.generic');
    expect(oauthFailureKey(undefined, journey)).toBe('settings.models.oauth.error.generic');
  });

  it.each(['zh', 'en'] as const)('resolves every key it can return in %s', (lng) => {
    const keys = [
      oauthFailureKey('discovery_failed', 'connect'),
      oauthFailureKey('discovery_failed', 'reauth'),
      oauthFailureKey(undefined, 'connect'),
    ];
    for (const key of keys) {
      // `serverText`'s fallback would hide a missing key behind the generic line;
      // these are OURS, so they must translate outright.
      const text = t(lng)(key, { defaultValue: '' }) as string;
      expect(text, key).not.toBe('');
      expect(text).not.toContain('settings.models');
    }
  });

  it('does not tell a reauth its source could not be created', () => {
    // The finding this branch exists for, asserted as the user-visible claim
    // rather than as a key: 「创建来源失败」 names an object a repair never had.
    const reauth = t('zh')(oauthFailureKey('discovery_failed', 'reauth')) as string;
    expect(reauth).not.toContain('创建');
    expect(t('en')(oauthFailureKey('discovery_failed', 'reauth')) as string).not.toMatch(/creat/i);
  });
});
