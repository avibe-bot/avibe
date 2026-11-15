import { createInstance } from 'i18next';
import { afterEach, expect, it, vi } from 'vitest';
import en from './en.json';
import zh from './zh.json';
import { forDevicePlatform } from './device';

afterEach(() => { vi.restoreAllMocks(); });

const leaves = (value: unknown): string[] => typeof value === 'string' ? [value]
  : value && typeof value === 'object' ? Object.values(value).flatMap(leaves) : [];

// Desktop ships on macOS and Windows; its account copy must not call a Windows PC a Mac.
it.each([
  ['MacIntel', 'This Mac · 小明的 Mac', 'Back to this Mac'],
  ['Win32', 'This PC · 小明的 Mac', 'Back to this PC'],
])('names the local machine for the %s platform in desktopClient copy', async (platform, localName, backLocal) => {
  vi.spyOn(navigator, 'platform', 'get').mockReturnValue(platform);
  const i18n = createInstance();
  await i18n.init({ lng: 'en', resources: { en: { translation: forDevicePlatform(en) } }, interpolation: { escapeValue: false } });
  expect(i18n.t('desktopClient.localName', { name: '小明的 Mac' })).toBe(localName);
  expect(i18n.t('desktopClient.backLocal')).toBe(backLocal);
  if (platform === 'Win32') {
    expect(leaves(forDevicePlatform(en).desktopClient).filter((text) => /\bMac\b/.test(text))).toEqual([]);
    expect(leaves(forDevicePlatform(zh).desktopClient).filter((text) => /\bMac\b/.test(text))).toEqual([]);
  }
  // Only Desktop's account copy is device-specific; macOS-only surfaces keep their wording.
  const patched = forDevicePlatform(en) as Record<string, unknown>;
  for (const key of Object.keys(en).filter((name) => name !== 'desktopClient')) expect(patched[key]).toBe((en as Record<string, unknown>)[key]);
});
