// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from './en.json';
import zh from './zh.json';

// The Web UI downloads only the language it opens in (./app.ts). What that
// must not cost: the first screen drawing before its copy is there, or a later
// switch to the other language (the picker, or the instance's saved language)
// showing raw keys, or staying in the old language, because that copy was not
// fetched yet.

afterEach(() => {
  window.localStorage.clear();
  vi.resetModules();
});

const open = async (stored: string) => {
  window.localStorage.setItem('i18nextLng', stored);
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
