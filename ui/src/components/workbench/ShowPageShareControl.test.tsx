import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { ShowPageShareControl } from './ShowPageShareControl';

vi.mock('../../context/ApiContext', () => ({
  useApi: () => ({
    ensureShowPage: vi.fn(),
    getShowPageAccess: vi.fn(),
    setShowPageVisibility: vi.fn(),
    rotateShowPageShare: vi.fn(),
  }),
}));

vi.mock('../../context/DockContext', () => ({
  useDock: () => ({
    isDocked: vi.fn(() => false),
    isPinned: vi.fn(() => false),
    dock: vi.fn(),
    pin: vi.fn(),
    undock: vi.fn(),
  }),
}));

vi.mock('../useShowPages', () => ({
  useShowPageInventory: () => ({
    pages: [],
    mergePage: vi.fn(),
    removePage: vi.fn(),
    reload: vi.fn(),
  }),
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const renderControl = (compact: boolean) =>
  renderToStaticMarkup(
    <I18nextProvider i18n={i18n}>
      <ShowPageShareControl sessionId="ses-1" compact={compact} />
    </I18nextProvider>,
  );

describe('ShowPageShareControl trigger presentation', () => {
  it('renders the header-sized trigger by default', () => {
    const html = renderControl(false);

    expect(html.match(/<button/g)).toHaveLength(1);
    expect(html).toContain('aria-label="Share"');
    expect(html).toContain('size-7');
    expect(html).not.toContain('size-6');
  });

  it('renders the window-chrome trigger when compact', () => {
    const html = renderControl(true);

    expect(html.match(/<button/g)).toHaveLength(1);
    expect(html).toContain('aria-label="Share"');
    // Mirrors the compact annotate control's title-bar styling (§ design q4E5l chrome).
    expect(html).toContain('size-6');
    expect(html).toContain('rounded-md');
    expect(html).toContain('text-muted');
    expect(html).toContain('hover:text-foreground');
    expect(html).not.toContain('size-7');
  });
});
