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
import {
  catalogSaveFailureKey,
  modelsDevFillFailureKey,
  NATIVE_LOGIN_IN_PROGRESS_FAILURE,
  NATIVE_SUBSCRIPTION_EXISTS_FAILURE,
  oauthFailureKey,
  oauthStartFailureKey,
  serverText,
  TIER_EDIT_MANAGED_FAILURE,
  tierEditRefusedAsManaged,
} from './serverCopy';

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

describe('oauthStartFailureKey', () => {
  it('surfaces the native singleton race with the dedicated already-bound copy', () => {
    expect(oauthStartFailureKey(NATIVE_SUBSCRIPTION_EXISTS_FAILURE)).toBe(
      'settings.models.addSub.error.alreadyBound',
    );
  });

  it('keeps a transient native login conflict retryable on its current channel', () => {
    const key = oauthStartFailureKey(NATIVE_LOGIN_IN_PROGRESS_FAILURE);
    expect(key).toBe('settings.models.addSub.error.loginInProgress');
    expect(t('en')(key)).toMatch(/already in progress/i);
    expect(t('zh')(key)).toContain('正在进行中');
  });

  it('keeps unknown start failures retryable without inventing a cause', () => {
    expect(oauthStartFailureKey(undefined)).toBe('settings.models.addSub.error.startFailed');
  });

  it('uses the dedicated gateway outage copy for engine-down starts', () => {
    expect(oauthStartFailureKey('engine_down')).toBe('settings.models.addSub.error.engineDown');
    expect(oauthStartFailureKey('modelHub.errors.engine_down')).toBe('settings.models.addSub.error.engineDown');
  });
});

describe('catalogSaveFailureKey', () => {
  // Every refusal `PUT /api/models/agents/<backend>/models` can answer with, and
  // the sentence that tells the user what to do about it. Spelled out here rather
  // than imported from the module, so this fails when the UI's table and the
  // service's taxonomy drift apart.
  const REFUSALS = [
    ['backend_model_in_route', 'saveRouted'],
    ['candidate_suppliers_changed', 'saveSuppliersChanged'],
    ['backend_model_conflict', 'saveConflict'],
    ['backend_model_id_prefix', 'saveIdPrefix'],
    ['backend_model_id_invalid', 'saveIdInvalid'],
    ['backend_model_duplicate', 'saveDuplicate'],
    ['backend_model_locked', 'saveLocked'],
    ['backend_model_catalog_invalid', 'saveInvalid'],
  ] as const;

  it.each(REFUSALS)('renders %s as copy, not as a key, in both locales', (code, key) => {
    const resolved = catalogSaveFailureKey(`modelHub.errors.${code}`);
    expect(resolved).toBe(`settings.models.gateway.catalog.${key}`);
    for (const lng of ['en', 'zh'] as const) {
      // Ours, so it must translate outright — a missing entry would reach the
      // user as the key itself.
      const text = t(lng)(resolved, { defaultValue: '' }) as string;
      expect(text, `${lng}: ${resolved}`).not.toBe('');
      expect(text).not.toContain('settings.models');
    }
  });

  it('gives each refusal a next step of its own', () => {
    // Seven codes pointing at one sentence would be the generic fallback wearing
    // seven names; the taxonomy exists because the actions differ.
    const texts = REFUSALS.map(([code]) => t('en')(catalogSaveFailureKey(`modelHub.errors.${code}`)) as string);
    expect(new Set(texts).size).toBe(REFUSALS.length);
    expect(texts).not.toContain(t('en')('settings.models.gateway.catalog.saveFailed'));
  });

  it('asks for a re-add where the ID rule is broken, because the field is read-only', () => {
    // Edit mode keeps the backend model id read-only, so 「fix the ID」 would be
    // advice this dialog refuses to take.
    const prefix = catalogSaveFailureKey('modelHub.errors.backend_model_id_prefix');
    expect(t('en')(prefix)).toMatch(/add it again/i);
    expect(t('zh')(prefix)).toContain('重新添加');
    const invalid = catalogSaveFailureKey('modelHub.errors.backend_model_id_invalid');
    expect(t('en')(invalid)).toMatch(/add it again/i);
    expect(t('zh')(invalid)).toContain('重新添加');
  });

  it('never passes an unrecognized server detail through as copy', () => {
    // `modelHub.errors.*` keys live in the backend bundle, so a code this UI does
    // not know would render as a machine string if it reached i18next.
    for (const detail of [undefined, 'modelHub.errors.some_future_code', 'engine_down']) {
      expect(catalogSaveFailureKey(detail)).toBe('settings.models.gateway.catalog.saveFailed');
    }
  });
});

describe('tierEditRefusedAsManaged', () => {
  const MANAGED_COPY = 'settings.models.sourceDetail.fail.tierManaged';

  it.each([
    ['code', { code: TIER_EDIT_MANAGED_FAILURE }],
    ['prefixed code', { code: `modelHub.errors.${TIER_EDIT_MANAGED_FAILURE}` }],
    ['detail', { code: 'bad_request', detail: TIER_EDIT_MANAGED_FAILURE }],
    ['prefixed detail', { code: 'bad_request', detail: `modelHub.errors.${TIER_EDIT_MANAGED_FAILURE}` }],
  ])('recognizes the refusal carried in the %s field', (_where, failure) => {
    // Both response shapes on this API are in play — `error` for
    // `source_not_found`, `detail` for the `modelHub.errors.*` refusals — and
    // which one the new guard picks is the backend's choice, not a fact this
    // client should encode.
    expect(tierEditRefusedAsManaged(failure)).toBe(true);
  });

  it('leaves every other failure on the retryable path', () => {
    for (const failure of [
      null,
      undefined,
      { code: 'source_not_found' },
      { code: 'engine_down', detail: 'modelHub.errors.engine_down' },
      // The neighbouring guard on `POST .../models`: a different write, refused
      // for a different reason, and one the user CAN act on by picking another
      // model id. Reading it as a locked tier list would replace that with a
      // sentence about a list they never edited.
      { code: 'source_model_managed_upstream' },
      { code: 'bad_request', detail: 'source_model_tiers_managed_by_someone_else' },
    ]) {
      expect(tierEditRefusedAsManaged(failure), JSON.stringify(failure)).toBe(false);
    }
  });

  it.each(['zh', 'en'] as const)('renders the refusal as a sentence, not a key, in %s', (lng) => {
    const text = t(lng)(MANAGED_COPY, { defaultValue: '' }) as string;
    expect(text, lng).not.toBe('');
    expect(text).not.toContain('settings.models');
    expect(text).not.toContain(TIER_EDIT_MANAGED_FAILURE);
    // Same sentence under the server-emitted key: a missing-key defect
    // (B1/D-3) would otherwise print `modelHub.errors.source_model_tiers_managed`
    // the moment a future path looks the code up instead of our fail.* alias.
    expect(t(lng)(`modelHub.errors.${TIER_EDIT_MANAGED_FAILURE}`, { defaultValue: '' })).toBe(text);
  });

  it('never invites a retry of a decision the server has already made', () => {
    // The generic tier-save line offers exactly that, which is why this refusal
    // needs copy of its own: retrying cannot succeed while the rung holds.
    expect(t('en')(MANAGED_COPY) as string).not.toMatch(/retry|try again/i);
    expect(t('zh')(MANAGED_COPY) as string).not.toContain('重试');
    expect(t('zh')(MANAGED_COPY) as string).not.toBe(t('zh')('settings.models.sourceDetail.fail.tier') as string);
  });
});

describe('modelsDevFillFailureKey', () => {
  it('separates an upstream catalog outage from a lookup that simply failed', () => {
    // Two different states of the same typeahead row, so the two keys must be
    // two sentences in every bundle: 「the catalog is down」 is not 「this lookup
    // failed」, and only the first one means retrying is the wrong advice. The
    // property is that they stay distinguishable and neither renders as a key —
    // the wording itself belongs to the copy table, not to this test.
    const down = modelsDevFillFailureKey('modelHub.errors.models_dev_unavailable');
    const failed = modelsDevFillFailureKey(undefined);
    expect(down).toBe('settings.models.gateway.modelEditor.fillUnavailable');
    for (const lng of ['en', 'zh'] as const) {
      const outage = t(lng)(down) as string;
      expect(outage).not.toBe('');
      expect(outage).not.toBe(down);
      expect(outage).not.toBe(t(lng)(failed) as string);
    }
  });

  it('keeps every other cause on the plain unreachable line', () => {
    for (const detail of [undefined, 'engine_down', 'modelHub.errors.some_future_code']) {
      expect(modelsDevFillFailureKey(detail)).toBe('settings.models.gateway.modelEditor.fillFailed');
    }
  });
});
