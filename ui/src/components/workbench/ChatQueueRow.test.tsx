import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import type { WorkbenchMessage } from '../../context/ApiContext';
import { AnnotationMessage } from './AnnotationMessage';
import { QueueRow } from './ChatPage';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const wrap = (ui: React.ReactElement) =>
  renderToStaticMarkup(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>);

// A queued forward annotation, exactly as the contract freezes it
// (docs/plans/show-annotation-message-type/examples.json, msg_01J8XK5M8T): the
// display record is already on the row; only ``type`` says it has not been sent.
const queued = (over: Partial<WorkbenchMessage> = {}): WorkbenchMessage =>
  ({
    id: 'msg_01J8XK5M8T',
    type: 'queued',
    author: 'harness',
    source: 'harness',
    author_name: 'show_annotation',
    text: 'This chart needs a legend',
    content: { annotation: { direction: 'user', action: 'created' } },
    metadata: { source: 'show_page' },
    created_at: '2026-07-27T04:04:00Z',
    ...over,
  }) as WorkbenchMessage;

const renderQueued = (item: WorkbenchMessage) =>
  wrap(<QueueRow item={item} onRemove={() => undefined} onRecall={() => undefined} />);

describe('QueueRow — a queued annotation in the strip (rule 08)', () => {
  it('names a queued annotation, and leaves an ordinary queued prompt unlabelled', () => {
    expect(renderQueued(queued())).toContain('User annotation');
    expect(renderQueued(queued())).toContain('This chart needs a legend');

    const plain = renderQueued(queued({ content: {} as WorkbenchMessage['content'] }));
    expect(plain).not.toContain('User annotation');
    expect(plain).not.toContain('Agent annotation');
  });

  // The point of the strip label. The row the user is looking at now and the
  // bubble that replaces it when the queue flushes must name the same thing the
  // same way — otherwise sending appears to turn one thing into another. Both
  // sides are rendered here rather than compared by key, so a divergence in
  // either renderer fails this.
  it('uses the same title the card will use once the queue flushes', () => {
    const strip = renderQueued(queued());
    const card = wrap(
      <AnnotationMessage
        messageId="msg_01J8XK5M8T"
        view={{ direction: 'user', resolved: false }}
        body={null}
        attachments={null}
        time={null}
        rowClass={(extra) => extra}
      />,
    );
    const title = (html: string) => html.match(/(User|Agent) annotation/)?.[0];

    expect(title(strip)).toBeDefined();
    expect(title(strip)).toBe(title(card));
  });

  it('names a queued reverse mark with the agent title', () => {
    const html = renderQueued(
      queued({ content: { annotation: { direction: 'agent', action: 'created' } } as WorkbenchMessage['content'] }),
    );
    expect(html).toContain('Agent annotation');
  });
});
