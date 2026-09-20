import { createInstance, type TFunction } from 'i18next';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import type { BackendNotice } from '@/context/ApiContext';

import { surfaceBackendNotices } from './surfaceBackendNotices';

const resources = {
  en: { translation: en },
  zh: { translation: zh },
};

const translate = new Map<'en' | 'zh', TFunction>();

beforeAll(async () => {
  for (const language of ['en', 'zh'] as const) {
    const instance = createInstance();
    await instance.init({
      lng: language,
      fallbackLng: 'en',
      resources,
      interpolation: { escapeValue: false },
    });
    translate.set(language, instance.t);
  }
});

describe('surfaceBackendNotices', () => {
  it.each(['en', 'zh'] as const)(
    'explains that the key was removed while settings may be stale in %s',
    (language) => {
      const showToast = vi.fn();
      const notice: BackendNotice = {
        code: 'v2_clear_failed',
        detail: 'disk full',
      };

      surfaceBackendNotices([notice], showToast, translate.get(language)!);

      expect(showToast).toHaveBeenCalledWith(
        expect.stringContaining('disk full'),
        'warning',
      );
      const message = showToast.mock.calls[0]?.[0] as string;
      expect(message).not.toContain('v2_clear_failed');
      expect(message).toMatch(language === 'en' ? /API key removed/i : /API Key 已删除/);
      expect(message).toMatch(language === 'en' ? /settings.*stale/i : /设置.*未更新/);
    },
  );

  it('keeps existing relay and unknown notice rendering intact', () => {
    const showToast = vi.fn();
    surfaceBackendNotices(
      [
        {
          code: 'cleared_custom_relay_pointer',
          provider_id: 'Relay',
          base_url: 'https://relay.example/v1',
        },
        { code: 'future_notice' },
      ],
      showToast,
      translate.get('en')!,
    );

    expect(showToast).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining('https://relay.example/v1'),
      'warning',
    );
    expect(showToast).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining('future_notice'),
      'warning',
    );
  });
});
