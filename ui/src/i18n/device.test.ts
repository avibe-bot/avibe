import { createInstance } from 'i18next';
import { afterEach, expect, it, vi } from 'vitest';
import { desktopEn as en, desktopZh as zh } from './desktop';
import { forDevicePlatform } from './device';

afterEach(() => { vi.restoreAllMocks(); });

const leaves = (value: unknown): string[] => typeof value === 'string' ? [value]
  : value && typeof value === 'object' ? Object.values(value).flatMap(leaves) : [];

// Desktop ships on macOS and Windows; its account copy must not call a Windows PC a Mac.
it.each([
  ['MacIntel', 'This Mac · 小明的 Mac', 'Back to this Mac'],
  ['Win32', 'This PC · 小明的 Mac', 'Back to this PC'],
])('names the local machine for the %s platform in Desktop copy', async (platform, localName, backLocal) => {
  vi.spyOn(navigator, 'platform', 'get').mockReturnValue(platform);
  const i18n = createInstance();
  await i18n.init({ lng: 'en', resources: { en: { translation: forDevicePlatform(en) } }, interpolation: { escapeValue: false } });
  expect(i18n.t('desktopAccount.localName', { name: '小明的 Mac' })).toBe(localName);
  expect(i18n.t('desktopAccount.backLocal')).toBe(backLocal);
  if (platform === 'Win32') {
    for (const bundle of [en, zh]) {
      const patched = forDevicePlatform(bundle);
      expect(leaves([patched.desktopClient, patched.desktopAccount]).filter((text) => /\bMac\b/.test(text))).toEqual([]);
    }
  }
  // Only Desktop's account copy is device-specific; macOS-only surfaces keep their wording.
  const patched = forDevicePlatform(en) as Record<string, unknown>;
  for (const key of Object.keys(en).filter((name) => name !== 'desktopClient' && name !== 'desktopAccount')) expect(patched[key]).toBe((en as Record<string, unknown>)[key]);
});
