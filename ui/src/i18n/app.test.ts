// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from './en.json';
import zh from './zh.json';
import surfaces from './surfaces.json';

// The Web UI downloads only the language it opens in (./app.ts). What that
// must not cost: the first screen drawing before its copy is there, or a later
// switch to the other language (the picker, or the instance's saved language)
// showing raw keys, or staying in the old language, because that copy was not
// fetched yet.

afterEach(() => {
  window.localStorage.clear();
  vi.unstubAllGlobals();
  vi.resetModules();
});

/** The browser's preferred languages, as `navigator.languages` reports them. */
const environment = (...languages: string[]) => {
  vi.stubGlobal('navigator', { ...window.navigator, languages, language: languages[0] });
};

const open = async (stored?: string) => {
  if (stored !== undefined) window.localStorage.setItem('i18nextLng', stored);
  const { default: i18n, i18nReady } = await import('./app');
  await i18nReady;
  return i18n;
};

describe('the Web UI language', () => {
  it('has the opening language ready before the first render, and only that one', async () => {
    const i18n = await open('zh-CN');

    expect(i18n.t('common.loading')).toBe(zh.common.loading);
    expect(i18n.hasResourceBundle('en', 'translation')).toBe(false);
  });

  it('opens an unsupported language in English', async () => {
    const i18n = await open('fr-FR');

    expect(i18n.t('common.loading')).toBe(en.common.loading);
    expect(i18n.hasResourceBundle('zh', 'translation')).toBe(false);
  });

  it('keeps the current language on screen until the other one has loaded', async () => {
    const i18n = await open('zh');

    const switching = i18n.changeLanguage('en');
    expect(i18n.t('common.loading')).toBe(zh.common.loading);
    await switching;

    expect(i18n.language).toBe('en');
    expect(i18n.t('common.loading')).toBe(en.common.loading);
  });
});

// Until the reader picks a language, the page follows the browser's preferred
// one, and following it is never written down as a choice.
describe('the language before the reader picks one', () => {
  it.each(['zh', 'zh-CN', 'zh-TW', 'zh-HK', 'zh-Hant-HK'])('opens in Chinese for a %s browser, storing nothing', async (code) => {
    environment(code, 'en-US');
    const i18n = await open();

    expect(i18n.language).toBe('zh');
    expect(i18n.t('common.loading')).toBe(zh.common.loading);
    expect(window.localStorage.getItem('i18nextLng')).toBeNull();
  });

  it.each(['en-US', 'en', 'fr-FR'])('opens in English for a %s browser, storing nothing', async (code) => {
    environment(code);
    const i18n = await open();

    expect(i18n.language).toBe('en');
    expect(i18n.t('common.loading')).toBe(en.common.loading);
    expect(window.localStorage.getItem('i18nextLng')).toBeNull();
  });

  it('keeps following the browser after a language change it did not pick', async () => {
    environment('zh-CN');
    const i18n = await open();
    await i18n.changeLanguage('en');

    expect(window.localStorage.getItem('i18nextLng')).toBeNull();
  });

  it('lets a kept pick win over the browser', async () => {
    environment('zh-CN');
    const i18n = await open('en');

    expect(i18n.language).toBe('en');
    expect(i18n.t('common.loading')).toBe(en.common.loading);
  });

  it('leaves a language an existing install already stored as it is', async () => {
    environment('en-US');
    const i18n = await open('zh-CN');

    expect(i18n.language).toBe('zh');
    expect(window.localStorage.getItem('i18nextLng')).toBe('zh-CN');
  });

  it('keeps a pick from the language switch', async () => {
    environment('zh-CN');
    const i18n = await open();
    const { selectLanguage } = await import('../lib/useLanguageSelection');
    await selectLanguage(i18n, 'en');

    await vi.waitFor(() => expect(i18n.language).toBe('en'));
    expect(window.localStorage.getItem('i18nextLng')).toBe('en');
  });
});

describe('the instance language an owner\'s browser adopts', () => {
  it('is kept, so the next load starts in it', async () => {
    environment('en-US');
    const i18n = await open();
    const { adoptPersistedLanguage } = await import('../lib/useLanguageSelection');
    adoptPersistedLanguage(i18n, 'zh');

    await vi.waitFor(() => expect(i18n.language).toBe('zh'));
    expect(window.localStorage.getItem('i18nextLng')).toBe('zh');
  });

  it('stores nothing when it is the language the browser already shows', async () => {
    environment('zh-CN');
    const i18n = await open();
    const { adoptPersistedLanguage } = await import('../lib/useLanguageSelection');
    adoptPersistedLanguage(i18n, 'zh');

    expect(window.localStorage.getItem('i18nextLng')).toBeNull();
  });
});

// Copy only Settings draws is its own chunk per language (scripts/localeSurfaces.ts),
// loaded with the Settings pages (./settingsCopy.ts).
type Tree = { [key: string]: unknown };
const leaves = (tree: Tree, prefix = ''): Map<string, unknown> => new Map(Object.entries(tree).flatMap(([key, value]) => {
  const path = prefix ? `${prefix}.${key}` : key;
  return value && typeof value === 'object' && !Array.isArray(value) ? [...leaves(value as Tree, path)] : [[path, value] as const];
}));
const lookup = (tree: Tree, path: string): unknown => path.split('.').reduce<unknown>((node, key) => (node as Tree | undefined)?.[key], tree);
// A sentence the Settings copy holds.
const settingsKey = [...leaves(zh)].find(([path, value]) => typeof value === 'string'
  && surfaces.settings.some((listed) => path === listed || path.startsWith(`${listed}.`)))![0];

describe('the Settings copy', () => {
  it.each([['en', en], ['zh', zh]] as const)('keeps every %s string, in exactly one surface', async (language, catalog) => {
    const first = leaves((await import(`virtual:avibe-copy/${language}/first`)).default as Tree);
    const settings = leaves((await import(`virtual:avibe-copy/${language}/settings`)).default as Tree);

    expect(settings.size).toBeGreaterThan(0);
    expect([...settings.keys()].filter((path) => first.has(path))).toEqual([]);
    expect(new Map([...first, ...settings])).toEqual(leaves(catalog as Tree));
  });

  it('is not part of the first screen\'s download, and is in place once Settings loads', async () => {
    const i18n = await open('zh');
    expect(i18n.exists(settingsKey)).toBe(false);

    const { loadSettingsCopy } = await import('./settingsCopy');
    await loadSettingsCopy();

    expect(i18n.t(settingsKey as never)).toBe(lookup(zh, settingsKey));
  });

  it('arrives in the other language before a switch shows it', async () => {
    const i18n = await open('zh');
    const { loadSettingsCopy } = await import('./settingsCopy');
    await loadSettingsCopy();

    await i18n.changeLanguage('en');

    expect(i18n.t(settingsKey as never)).toBe(lookup(en, settingsKey));
  });
});
