// The Model Hub draws its copy through `t('settings.models…')` literals, and a
// key that never reached the bundles renders as the raw key: removing a provider
// showed `settings.models.sourceDetail.gone` where a sentence belonged.
//
// So the guard states the property — every literal key those components ask for
// has copy in BOTH locales — and EXTRACTS the key list from the components
// themselves. A key or a whole new component added later is covered without
// editing this file, which a hand-written list could never promise.
//
// Resolution runs through a real i18next instance instead of walking the JSON,
// because the bundles use two shapes a plain path walk reports as missing and
// the page depends on: plural families (`usageSummary_one` with no plain key)
// and flat dotted keys (`"vendor.hint"` sitting inside `field`, found by
// `ignoreJSONStructure`). The audit that opened this lane walked paths, and
// called 12 keys missing that the runtime resolves.
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { createInstance, type i18n as I18n } from 'i18next';
import { describe, expect, it } from 'vitest';

import en from './en.json';
import zh from './zh.json';

const MODEL_HUB = resolve(dirname(fileURLToPath(import.meta.url)), '../components/settings/models');

/**
 * Every key a source file names as a literal first argument to `t()`.
 *
 * Anchored on a word boundary so a call whose name merely ends in `t` — the
 * page's own `jsonInit('POST')` — cannot be read as a translation. A key built
 * from a variable or a template is invisible here by construction; the ones a
 * frozen contract declares are covered by the models page's own
 * `contractLocaleKeys.test.ts`.
 */
export const collectKeys = (source: string): string[] => {
  const keys = new Set<string>();
  for (const match of source.matchAll(/(?<![\w$.])t\(\s*(['"])((?:\\.|(?!\1)[^\\])*)\1/g)) {
    keys.add(match[2]);
  }
  return [...keys].sort();
};

const sourceFiles = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    if (!/\.tsx?$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name)) return [];
    return [path];
  });

const referencedKeys = (): string[] => {
  const keys = new Set<string>();
  for (const file of sourceFiles(MODEL_HUB)) {
    for (const key of collectKeys(readFileSync(file, 'utf8'))) keys.add(key);
  }
  return [...keys].sort();
};

/** One locale, resolved with no fallback: a zh gap must not be filled by en. */
const localeInstance = (lng: 'en' | 'zh'): I18n => {
  const instance = createInstance();
  void instance.init({
    lng,
    fallbackLng: false,
    resources: { [lng]: { translation: lng === 'en' ? en : zh } },
  });
  return instance;
};

/**
 * Probed with and without `count`, because `count` is what selects a plural
 * family: a key that exists only as `_one`/`_other` resolves to nothing until it
 * is asked with one.
 */
const PROBES = [{}, { count: 1 }, { count: 2 }] as const;

/** The symptom this guard exists for, stated exactly: no copy renders as the key. */
const hasCopy = (instance: I18n, key: string): boolean => PROBES.some((options) => {
  const rendered: unknown = instance.t(key, options);
  return typeof rendered === 'string' && rendered !== '' && rendered !== key;
});

describe('Model Hub i18n key coverage', () => {
  const keys = referencedKeys();

  it('collects the literal keys, and only those, from a source file', () => {
    // The forward guard on the collector itself: a coverage test whose collector
    // silently stops matching reports coverage it never had.
    const fixture = `
      const label = t('settings.models.fixture.literal');
      const quoted = t("settings.models.fixture.double");
      const wrapped = t(
        'settings.models.fixture.wrapped',
        { count: 2 },
      );
      void jsonInit('POST');
      void request.t('settings.models.fixture.member');
      void t(\`settings.models.fixture.\${dynamic}\`);
    `;
    expect(collectKeys(fixture)).toEqual([
      'settings.models.fixture.double',
      'settings.models.fixture.literal',
      'settings.models.fixture.wrapped',
    ]);
  });

  it('reads its key list out of the live components', () => {
    // Not a floor on the count, which would only say the scan found something:
    // the scan must have walked the tree, and produced the key whose absence
    // put a raw `settings.models.sourceDetail.gone` on the remove flow.
    expect(sourceFiles(MODEL_HUB).length).toBeGreaterThan(1);
    expect(keys).toContain('settings.models.sourceDetail.gone');
  });

  it.each(['en', 'zh'] as const)('translates every referenced key in %s', (lng) => {
    const instance = localeInstance(lng);
    expect(keys.filter((key) => !hasCopy(instance, key))).toEqual([]);
  });
});
