import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import type { AnnotationView } from '../../lib/annotationView';
import { AnnotationMessage } from './AnnotationMessage';
import { AGENT_BUBBLE, USER_BUBBLE } from './chatBubble';

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

const render = (view: AnnotationView, lng: 'en' | 'zh' = 'en') =>
  renderToStaticMarkup(
    <I18nextProvider i18n={instance(lng)}>
      <AnnotationMessage
        messageId="msg_01J8XK2Q4W"
        view={view}
        body={<p>Body</p>}
        attachments={<img alt="" src="/api/media/med_9a71c33f8b2e" />}
        time={<span>12:04</span>}
        rowClass={(extra) => `flex w-full ${extra}`}
      />
    </I18nextProvider>,
  );

const USER: AnnotationView = { direction: 'user', resolved: false };
const AGENT: AnnotationView = { direction: 'agent', resolved: false };

describe('AnnotationMessage', () => {
  // Rule 01. The side is direction, full stop — the component is never told who
  // authored the row, so there is nothing else it could side on.
  it('sides the card by direction', () => {
    expect(render(USER)).toContain('justify-end');
    expect(render(AGENT)).toContain('justify-start');
  });

  // Rule 02: exactly two titles, and the action never enters them.
  it('titles the card by direction only, in the UI language', () => {
    expect(render(USER)).toContain('User annotation');
    expect(render(AGENT)).toContain('Agent annotation');
    expect(render(USER, 'zh')).toContain('用户批注');
    expect(render(AGENT, 'zh')).toContain('Agent 批注');
  });

  it('keeps the title the same for a resolved mark', () => {
    const html = render({ ...AGENT, resolved: true });
    expect(html).toContain('Agent annotation');
    expect(html).not.toContain('User annotation');
  });

  // Rule 03: the bubble IS the column's existing bubble. Asserting against the
  // shared constants rather than a copied class string means a future restyle of
  // the ordinary bubbles carries the card with it and cannot leave it behind.
  it('reuses the transcript bubble the row would otherwise have drawn', () => {
    // The Tailwind arbitrary-variant classes carry ``&``, which React escapes in
    // the attribute; escape the constant the same way rather than loosening the
    // match, so this stays an assertion about the WHOLE class string.
    const asAttr = (cls: string) => cls.replaceAll('&', '&amp;');
    expect(render(USER)).toContain(asAttr(USER_BUBBLE));
    expect(render(AGENT)).toContain(asAttr(AGENT_BUBBLE));
  });

  // Rule 04: quoted only when the anchor carried copy the reader can find.
  it('draws the anchor quote when there is one, and nothing when there is not', () => {
    expect(render({ ...USER, quote: 'Model Hub' })).toContain('Model Hub');
    expect(render(USER)).not.toContain('border-l-2');
  });

  // Rule 07: created / updated / dismissed draw no marker at all.
  it('marks only a resolved annotation', () => {
    expect(render({ ...AGENT, resolved: true })).toContain('Resolved');
    expect(render({ ...AGENT, resolved: true }, 'zh')).toContain('已处理');
    expect(render(AGENT)).not.toContain('Resolved');
  });

  // Rule 06: the screenshot is the transcript's own thumbnail, passed in. The card
  // renders no image element of its own.
  it('passes the transcript body, attachments and timestamp straight through', () => {
    const html = render(USER);
    expect(html).toContain('<p>Body</p>');
    expect(html).toContain('/api/media/med_9a71c33f8b2e');
    expect(html).toContain('12:04');
  });

  it('carries the deep-link row dressing so an annotation can be jumped to', () => {
    expect(render(USER)).toContain('data-message-id="msg_01J8XK2Q4W"');
  });
});
