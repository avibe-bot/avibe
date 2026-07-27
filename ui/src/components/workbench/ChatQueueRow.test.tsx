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

// ``text`` is the annotator's own words and nothing else, so an annotation is
// allowed to arrive with none: a pure highlight, or a boxed region submitted
// without a comment. The strip is the ONLY place a queued row is visible, so
// "no words" must not mean "no row".
describe('QueueRow — a queued annotation nobody wrote words for', () => {
  const wordless = (over: Partial<WorkbenchMessage> = {}) => queued({ text: '', ...over });
  const withAnnotation = (annotation: Record<string, unknown>, attachments?: unknown[]) =>
    ({ annotation, ...(attachments ? { attachments } : {}) }) as WorkbenchMessage['content'];
  const screenshot = [{ url: '/api/media/med_9a71c33f8b2e', name: 'annotation-region.png', kind: 'image' }];

  it('shows the boxed region a screenshot-only annotation is made of', () => {
    const html = renderQueued(
      wordless({ content: withAnnotation({ direction: 'user', action: 'created' }, screenshot) }),
    );
    expect(html).toContain('User annotation');
    expect(html).toContain('Screenshot');
  });

  it('shows the highlight an anchor-only annotation is made of', () => {
    const html = renderQueued(
      wordless({ content: withAnnotation({ direction: 'user', action: 'created', quote: 'Model Hub' }) }),
    );
    expect(html).toContain('User annotation');
    expect(html).toContain('Model Hub');
  });

  // Both present: the strip has one line and the card puts the quote above the
  // screenshot, so the line takes the quote.
  it('takes the quote over the screenshot, and the words over both', () => {
    const both = withAnnotation({ direction: 'user', action: 'created', quote: 'Model Hub' }, screenshot);
    const silent = renderQueued(wordless({ content: both }));
    expect(silent).toContain('Model Hub');
    expect(silent).not.toContain('Screenshot');

    const spoken = renderQueued(queued({ text: 'The spacing is off', content: both }));
    expect(spoken).toContain('The spacing is off');
    expect(spoken).not.toContain('Model Hub');
    expect(spoken).not.toContain('Screenshot');
  });

  // Neither a quote nor a region: nothing about the row can be shown that the
  // reader could act on, so the title stands alone — without the separator that
  // would promise something after it.
  it('leaves no dangling separator when the title is all there is', () => {
    const html = renderQueued(wordless({ content: withAnnotation({ direction: 'user', action: 'created' }) }));
    expect(html).toContain('User annotation');
    expect(html).not.toContain('·');
  });

  // Same requirement as the title, one level down: the strip entry and the card
  // that replaces it on flush must show the reader the same quote, so sending
  // does not appear to change what was annotated.
  it('quotes what the card will quote', () => {
    const view = { direction: 'user' as const, resolved: false, quote: 'Model Hub' };
    const strip = renderQueued(
      wordless({ content: withAnnotation({ direction: 'user', action: 'created', quote: view.quote }) }),
    );
    const card = wrap(
      <AnnotationMessage
        messageId="msg_01J8XK5M8T"
        view={view}
        body={null}
        attachments={null}
        time={null}
        rowClass={(extra) => extra}
      />,
    );

    for (const html of [strip, card]) {
      expect(html).toContain('User annotation');
      expect(html).toContain('Model Hub');
    }
  });
});
