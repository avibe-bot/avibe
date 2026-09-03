// The suite's selector vocabulary.
//
// Model Hub product code carries no `data-testid`, and this lane may not add
// one. What it does carry is user-visible copy: every control is reachable by
// its accessible name, and every name comes from the shipped i18n bundles.
// Reading those instead of pasting the strings means a copy change breaks the
// test at the key, where the diff is legible, rather than at a locator that
// silently stops matching. Specs default to English — the hermetic instance's
// language — and pass `'zh'` only when they have already put the page in
// Chinese (a config-GET intercept, never a persisted save).
//
// The bundle is not plain nesting: leaves may carry dots of their own
// (`addKey.field.apiKey` is a string AND `addKey.field.apiKey.reveal` exists
// beside it), which is what i18next's progressive lookup allows. `resolve`
// reproduces that rule — longest literal segment first, then descend — so a key
// resolves here exactly as it does in the browser.
import { readFileSync } from 'node:fs';

type Bundle = { [key: string]: string | Bundle };
type Vars = Record<string, string | number>;
export type CopyLocale = 'en' | 'zh';

const bundles: Record<CopyLocale, Bundle> = {
  en: JSON.parse(readFileSync(new URL('../../src/i18n/en.json', import.meta.url), 'utf8')) as Bundle,
  zh: JSON.parse(readFileSync(new URL('../../src/i18n/zh.json', import.meta.url), 'utf8')) as Bundle,
};

const resolve = (node: string | Bundle | undefined, segments: string[]): string | undefined => {
  if (node === undefined) return undefined;
  if (segments.length === 0) return typeof node === 'string' ? node : undefined;
  if (typeof node === 'string') return undefined;
  for (let take = segments.length; take >= 1; take -= 1) {
    const found = resolve(node[segments.slice(0, take).join('.')], segments.slice(take));
    if (found !== undefined) return found;
  }
  return undefined;
};

const interpolate = (template: string, vars?: Vars): string =>
  template.replace(/\{\{\s*([\w.]+)\s*\}\}/g, (whole, name: string) =>
    vars && name in vars ? String(vars[name]) : whole);

/** The rendered string for `key`, or `null` when the bundle has no such key. */
export const copyOrNull = (key: string, vars?: Vars, locale: CopyLocale = 'en'): string | null => {
  const count = vars?.count;
  const candidates = typeof count === 'number'
    ? [`${key}_${new Intl.PluralRules(locale === 'zh' ? 'zh-CN' : 'en-US').select(count)}`, key]
    : [key];
  for (const candidate of candidates) {
    const template = resolve(bundles[locale], candidate.split('.'));
    if (template !== undefined) return interpolate(template, vars);
  }
  return null;
};

/**
 * The rendered string for `key`. Throws when the key is missing, because a
 * locator built from `undefined` matches nothing and reports "element not
 * found" — an absent key has to fail as an absent key.
 */
export const copy = (key: string, vars?: Vars, locale: CopyLocale = 'en'): string => {
  const text = copyOrNull(key, vars, locale);
  if (text === null) throw new Error(`i18n key missing from src/i18n/${locale}.json: ${key}`);
  return text;
};

/** Whether the shipped bundle defines `key`. Used by the copy-hygiene checks. */
export const hasCopy = (key: string): boolean => copyOrNull(key) !== null;

/** `settings.models.` prefix, since every key this suite reads lives under it. */
export const hub = (key: string, vars?: Vars, locale: CopyLocale = 'en'): string =>
  copy(`settings.models.${key}`, vars, locale);

/** Nullable form of {@link hub}. */
export const hubOrNull = (key: string, vars?: Vars, locale: CopyLocale = 'en'): string | null =>
  copyOrNull(`settings.models.${key}`, vars, locale);
