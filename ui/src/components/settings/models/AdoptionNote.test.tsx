// The empty array is the case worth a test: `adopted_by: []` means the source is
// live and serving nothing, and a note that renders blank there is the same defect
// the note was added to fix. It cannot be reached from the mock store (every
// `follow` backend adopts an eligible source), so it is pinned here instead.
import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { AdoptionNote } from './AdoptionNote';
import type { AdoptedBy } from './types';

const instance = (lng: 'en' | 'zh') => {
  const i18n = createInstance();
  void i18n.use(initReactI18next).init({
    lng,
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
  return i18n;
};

const render = (adoptedBy: AdoptedBy[], lng: 'en' | 'zh' = 'zh') =>
  renderToStaticMarkup(
    <I18nextProvider i18n={instance(lng)}>
      <AdoptionNote adoptedBy={adoptedBy} />
    </I18nextProvider>,
  );

const entry = (backend: string, position: number): AdoptedBy => ({
  backend,
  policy: 'follow',
  position,
});

describe('AdoptionNote', () => {
  it.each(['zh', 'en'] as const)('says nobody enabled it yet in %s', (lng) => {
    const html = render([], lng);
    // Not blank, and not a raw key — an unresolved i18n key would still render
    // non-empty text, so assert the sentence is real.
    expect(html).not.toContain('adoption.none');
    expect(html.replace(/<[^>]+>/g, '').trim().length).toBeGreaterThan(8);
  });

  it('lists adopters by position, not by response order', () => {
    const html = render([entry('codex', 3), entry('claude', 1)]);
    const text = html.replace(/<[^>]+>/g, '');
    expect(text.indexOf('Claude Code')).toBeLessThan(text.indexOf('Codex'));
    expect(text).toContain('第 1 位');
    expect(text).toContain('第 3 位');
    // The page separator, so a joined list reads like the rest of the page.
    expect(text).toContain(' · ');
  });

  it('falls back to the raw backend id for an unknown backend', () => {
    // A backend shipped by the server before the UI has a label for it must still
    // be nameable — dropping it would understate who took the source.
    expect(render([entry('futurecli', 2)])).toContain('futurecli');
  });
});
