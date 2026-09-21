/**
 * C2's action label: compiler assertions, plus the resolution they are about.
 *
 * `labelContract` is compiled and never executed — each `@ts-expect-error` IS the
 * assertion, and an unused directive fails the compile, so this is the only
 * construction in which "the type still refuses that" stays true as the type changes.
 * Same shape as `src/i18n/translationContract.test.ts`, for the same reason.
 *
 * What is pinned: a counted label names a family C1 ships as `_one`/`_other` and carries
 * the count that resolves it, a plain label carries nothing, and neither may name a string
 * the bundles do not have. The executed cases below then show the shell resolving both
 * forms through one `t` call, in both locales — the claim and its consequence in one file.
 */
import { createInstance } from 'i18next';
import type { TFunction } from 'i18next';
import { describe, expect, it } from 'vitest';
import type { SetupAction } from './setupFlow';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';

const CHROME = { disabled: false, busy: false, icon: 'arrow-right' } as const;

/** The counted claim: the family, and the count that makes it resolve. */
const countedAction: SetupAction = {
  ...CHROME,
  labelKey: 'onboarding.providers.actionImport',
  labelArgs: { count: 2 },
};

/** The plain claim: a leaf that resolves on its own needs no args at all. */
const plainAction: SetupAction = {
  ...CHROME,
  labelKey: 'onboarding.providers.actionContinue',
};

function labelContract() {
  /** Other interpolation rides along with the count, as `labelArgs` always allowed. */
  const countedWithMore: SetupAction = {
    ...CHROME,
    labelKey: 'onboarding.import.remaining',
    labelArgs: { count: 1, name: 'Codex' },
  };
  // @ts-expect-error a plural family resolves only with a count, so C2 will not carry it alone
  const countedWithoutCount: SetupAction = {
    ...CHROME,
    labelKey: 'onboarding.providers.actionImport',
  };
  // @ts-expect-error interpolation that is not a count cannot stand in for one
  const countedWithWrongArgs: SetupAction = {
    ...CHROME,
    labelKey: 'onboarding.providers.actionRetryImport',
    labelArgs: { name: 'Codex' },
  };
  // @ts-expect-error a count is a number; a rendered one is the bundle's business
  const countedWithText: SetupAction = {
    ...CHROME,
    labelKey: 'onboarding.providers.actionImport',
    labelArgs: { count: 'two' },
  };
  const absentKey: SetupAction = {
    ...CHROME,
    // @ts-expect-error a key that has not shipped in the bundles cannot be named either way
    labelKey: 'onboarding.providers.actionImportt',
  };
  const objectPrefix: SetupAction = {
    ...CHROME,
    // @ts-expect-error an object prefix is not a label, counted or otherwise
    labelKey: 'onboarding.providers',
  };
  /**
   * A family's own `_other` form stays a plain label, and that is correct: it is a text
   * leaf that resolves on its own. The counted member is about the base `t` needs a count
   * for; it does not take the suffixed leaves away from anyone.
   */
  const suffixedLeaf: SetupAction = {
    ...CHROME,
    labelKey: 'onboarding.providers.actionImport_other',
  };
  return [countedWithMore, countedWithoutCount, countedWithWrongArgs, countedWithText,
    absentKey, objectPrefix, suffixedLeaf];
}
void labelContract;

/**
 * The shell's side of the same claim: one `t` call, no narrowing.
 *
 * This is the whole reason the two members keep identical field names — a shell that had
 * to discriminate before rendering would be a protocol change, not a wider label type.
 */
export function renderSetupAction(t: TFunction, action: SetupAction): string {
  return t(action.labelKey, action.labelArgs);
}

const translator = (lng: 'en' | 'zh') => {
  const instance = createInstance();
  void instance.init({ lng, fallbackLng: false, resources: { en: { translation: en }, zh: { translation: zh } } });
  return instance;
};

describe('C2 action label resolution', () => {
  it.each(['en', 'zh'] as const)('resolves both label forms through one t call in %s', (lng) => {
    const bundle = lng === 'en' ? en : zh;
    const t = translator(lng).t;

    // The count is not decoration: it is what turns a family into a string. Expected text
    // comes from the bundle rather than from a copy pasted here, because which words those
    // are is the contract's business, not this file's.
    expect(renderSetupAction(t, countedAction))
      .toBe(bundle.onboarding.providers.actionImport_other.replace('{{count}}', '2'));
    expect(renderSetupAction(t, plainAction)).toBe(bundle.onboarding.providers.actionContinue);
  });
});
