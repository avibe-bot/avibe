import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '@/context/ToastContext';
import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { SourceRow } from './SourceRow';
import type { Source } from './types';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'zh',
  fallbackLng: 'en',
  resources: { en: { translation: en }, zh: { translation: zh } },
  interpolation: { escapeValue: false },
});

const source = (kind: Source['kind']): Source => ({
  id: `src_${kind}`,
  kind,
  vendor: 'anthropic',
  display_name: kind,
  protocol: 'anthropic',
  supply_channel: kind === 'subscription' ? 'native_cli' : 'hub',
  billing: kind === 'subscription' ? 'monthly' : 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [],
});

const render = (row: Source) => renderToStaticMarkup(
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <SourceRow source={row} onChanged={vi.fn()} onRepair={vi.fn()} onAddModel={vi.fn()} />
    </ToastProvider>
  </I18nextProvider>,
);

describe('SourceRow empty inventory', () => {
  it('sends subscriptions to sign-in instead of an unavailable manual-model action', () => {
    const html = render(source('subscription'));
    expect(html).toContain(zh.settings.models.sources.modelsEmptySubscription);
    expect(html).toContain(zh.settings.models.sourceActions.reauth);
    expect(html).not.toContain(zh.settings.models.sources.modelsEmpty);
    expect(html).not.toContain(zh.settings.models.sources.addModel);
  });

  it('keeps manual model creation available for API-key sources', () => {
    const html = render(source('api_key'));
    expect(html).toContain(zh.settings.models.sources.modelsEmpty);
    expect(html).toContain(zh.settings.models.sources.addModel);
  });
});
