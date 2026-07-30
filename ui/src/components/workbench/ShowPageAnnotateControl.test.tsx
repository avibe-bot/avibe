import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { ShowPageAnnotateControl } from './ShowPageAnnotateControl';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const renderCompact = (enabled: boolean) =>
  renderToStaticMarkup(
    <I18nextProvider i18n={i18n}>
      <ShowPageAnnotateControl
        compact
        state={{ enabled, mode: 'smart', available: true }}
        onEnable={vi.fn()}
        onDisable={vi.fn()}
        onSetMode={vi.fn()}
      />
    </I18nextProvider>,
  );

describe('ShowPageAnnotateControl compact presentation', () => {
  it.each([false, true])('keeps one title-bar button when enabled=%s', (enabled) => {
    const html = renderCompact(enabled);

    expect(html.match(/<button/g)).toHaveLength(1);
    expect(html).toContain('aria-label="Annotate"');
  });
});
