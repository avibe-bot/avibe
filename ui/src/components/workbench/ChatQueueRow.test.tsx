import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import type { WorkbenchMessage } from '../../context/ApiContext';
import { QueueRow } from './ChatPage';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'zh',
  fallbackLng: 'en',
  resources: { en: { translation: en }, zh: { translation: zh } },
  interpolation: { escapeValue: false },
});

const render = (item: Partial<WorkbenchMessage>) =>
  renderToStaticMarkup(
    <I18nextProvider i18n={i18n}>
      <QueueRow
        item={
          {
            id: 'msg_01J8XK5M8T',
            type: 'queued',
            author: 'harness',
            source: 'harness',
            text: '这两个卡片的间距不一致',
            content: {},
            created_at: '2026-07-27T10:15:48.006Z',
            ...item,
          } as WorkbenchMessage
        }
        onRemove={() => undefined}
        onRecall={() => undefined}
      />
    </I18nextProvider>,
  );

describe('queue strip / a queued annotation', () => {
  it('names the annotation in the strip, with the title its card will carry', () => {
    // Rule 08: while it is queued the annotation lives in the strip and nowhere
    // else, so this row is the only place the user can see it — it has to say
    // what it is. Same title as the bubble that replaces it on flush.
    const html = render({
      content: { text: '这两个卡片的间距不一致', annotation: { direction: 'user', action: 'created' } },
    });
    expect(html).toContain('用户批注');
    expect(html).toContain('这两个卡片的间距不一致');
  });

  it('leaves an ordinary queued prompt exactly as it was', () => {
    const html = render({ source: 'user', content: {} });
    expect(html).not.toContain('批注');
    expect(html).toContain('这两个卡片的间距不一致');
  });
});
