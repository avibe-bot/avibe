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
import type { AdoptedBy, SkippedBy } from './types';

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

const render = (adoptedBy: AdoptedBy[] | null, skippedBy?: SkippedBy[], lng: 'en' | 'zh' = 'zh') =>
  renderToStaticMarkup(
    <I18nextProvider i18n={instance(lng)}>
      <AdoptionNote adoptedBy={adoptedBy} skippedBy={skippedBy} />
    </I18nextProvider>,
  );

const text = (html: string) => html.replace(/<[^>]+>/g, '');

const entry = (backend: string, position: number): AdoptedBy => ({
  backend,
  policy: 'follow',
  position,
});

describe('AdoptionNote', () => {
  it.each(['zh', 'en'] as const)('says nobody enabled it yet in %s', (lng) => {
    const html = render([], undefined, lng);
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

  // 「谁没有」 is the answer this note exists for, and `adopted_by` alone cannot give
  // it: `_adopted_by` filters `policy == "follow"`, so a `custom` backend that
  // skipped the source is absent for the same reason an ineligible one is. That is
  // what `skipped_by` is for — and a response that omits it is still a response that
  // did not answer, so the note keeps saying only what it can prove.
  it('claims nothing about who skipped it while the server does not say', () => {
    const html = render([entry('claude', 1)]);
    expect(text(html)).toContain('Claude Code');
    expect(html).not.toContain('adoption.');
    expect(text(html)).not.toContain('自定义顺序');
  });

  it('names the skipped backends once the server sends them', () => {
    const html = render([entry('claude', 1)], [{ backend: 'codex', reason: 'custom_order' }]);
    const body = text(html);
    expect(body).toContain('Claude Code');
    expect(body).toContain('Codex');
    expect(body).toContain('自定义顺序');
  });

  it('names them even when nobody adopted it at all', () => {
    // `adopted_by: []` with a non-empty complement — every eligible backend keeps a
    // hand-picked order. The generic 「还没有 Agent 启用它」 is true here but throws
    // away the one thing the payload knows: which orders to go edit. And the adopted
    // half must NOT appear: there is no list to name.
    const body = text(render([], [{ backend: 'codex', reason: 'custom_order' }]));
    expect(body).toContain('Codex');
    expect(body).toContain('自定义顺序');
    expect(body).not.toContain('已自动加入');
  });

  it('says nothing at all when the creation reported no adoption result', () => {
    // OAuthConnectDialog's unreported-creation path. An absent result is not an empty
    // one: 「没有 Agent 启用它」 would be a claim, and this is ignorance. The component
    // owns that distinction so its callers stop guarding it on the way in.
    expect(render(null)).toBe('');
  });
});
