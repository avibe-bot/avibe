// App Lock's lock screen is drawn natively, but its copy has one source: `appLock.lock.*` in
// these bundles (keys shared by every shell, `appLock.lock.iphone.*` for the iPhone only). The
// iPhone App's string catalog carries the same keys; this keeps every one of them word for word
// equal to the bundles, so a shell can never ship its own wording.
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import en from './en.json';
import zh from './zh.json';

type Tree = { [key: string]: string | Tree };
const flatten = (tree: Tree, prefix: string): Record<string, string> =>
  Object.fromEntries(Object.entries(tree).flatMap(([key, value]) =>
    typeof value === 'string' ? [[`${prefix}.${key}`, value]] : Object.entries(flatten(value, `${prefix}.${key}`))));

type Catalog = { strings: Record<string, { localizations?: Record<string, { stringUnit?: { value?: string } }> }> };
const catalog = JSON.parse(readFileSync(new URL('../../../ios/Avibe/Resources/Localizable.xcstrings', import.meta.url), 'utf8')) as Catalog;

describe('App Lock lock-screen copy', () => {
  const bundles = { en: flatten(en.appLock as Tree, 'appLock'), zh: flatten(zh.appLock as Tree, 'appLock') };

  it('has the same keys in both locales', () => {
    expect(Object.keys(bundles.zh).sort()).toEqual(Object.keys(bundles.en).sort());
  });

  it('is word for word the bundles in the iPhone catalog', () => {
    const ios = Object.entries(catalog.strings).filter(([key]) => key.startsWith('appLock.'));
    expect(ios.length).toBeGreaterThan(0);
    for (const [key, entry] of ios) {
      expect(bundles.en[key], key).toBeDefined();
      expect(entry.localizations?.en?.stringUnit?.value, `${key} en`).toBe(bundles.en[key]);
      expect(entry.localizations?.['zh-Hans']?.stringUnit?.value, `${key} zh`).toBe(bundles.zh[key]);
    }
  });
});
