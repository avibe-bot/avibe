/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// ``ChatPage`` reaches the composer's mention editor at import time, which reads
// ``matchMedia`` on the module's first evaluation — before any test body runs.
//
// The attachment group reads the `sm` breakpoint through the same API, so this
// mock is a real one for that query: a test picks a width by setting it, and
// crosses the breakpoint by dispatching the `change` event a browser dispatches.
// jsdom has no media queries of its own, which is exactly why the CSS version of
// this component had a focus defect no unit test could see.
const breakpoint = vi.hoisted(() => {
  const WIDE = '(min-width: 640px)';
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  const state = { wide: false };
  const media = {
    get matches() {
      return state.wide;
    },
    addEventListener: (_: string, fn: (event: MediaQueryListEvent) => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: (event: MediaQueryListEvent) => void) => listeners.delete(fn),
  };
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn((query: string) =>
      query === WIDE ? media : { matches: false, addEventListener: () => {}, removeEventListener: () => {} },
    ),
  });
  return {
    state,
    emit: () => listeners.forEach((fn) => fn({ matches: state.wide } as MediaQueryListEvent)),
  };
});

const viewport = {
  /** Before a render: picks the width. After one: crosses the breakpoint. */
  set(wide: boolean) {
    if (breakpoint.state.wide === wide) return;
    breakpoint.state.wide = wide;
    act(() => breakpoint.emit());
  },
};

import en from '../../i18n/en.json';
import type { WorkbenchMessage } from '../../context/ApiContext';
import { FileViewerContext } from '../ui/file-viewer-context';
import { ImageViewerContext } from '../ui/image-viewer-context';
import { AnnotationMessage } from './AnnotationMessage';
import { QueueRow, QueueStrip } from './ChatPage';

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

afterEach(cleanup);
beforeEach(() => {
  breakpoint.state.wide = false;
});

// A live row under both viewers, so what an attachment click actually asks for
// is observable — the two handles are the whole boundary between the queue and
// the surfaces that inspect a file.
const mountQueued = (item: WorkbenchMessage, handlers: Partial<{
  onRemove: (id: string) => void;
  onRecall: (item: WorkbenchMessage) => void;
}> = {}) => {
  const openImage = vi.fn();
  const openFile = vi.fn();
  const view = render(
    <I18nextProvider i18n={i18n}>
      <ImageViewerContext.Provider value={{ open: openImage }}>
        <FileViewerContext.Provider value={{ open: openFile }}>
          <QueueRow
            item={item}
            onRemove={handlers.onRemove ?? (() => undefined)}
            onRecall={handlers.onRecall ?? (() => undefined)}
          />
        </FileViewerContext.Provider>
      </ImageViewerContext.Provider>
    </I18nextProvider>,
  );
  return { ...view, openImage, openFile };
};

const row = () => document.querySelector('[data-queue-row="true"]') as HTMLElement;
const thumbs = () => [...document.querySelectorAll('[data-queue-attachment^="image"]')] as HTMLElement[];
const chips = () => [...document.querySelectorAll('[data-queue-attachment^="file"]')] as HTMLElement[];
const more = () => [...document.querySelectorAll('[data-queue-attachment-more="true"]')] as HTMLElement[];

describe('QueueStrip — compatible queued messages read as one batch', () => {
  it('MESSAGE-DELIVERY-031: shows the server-owned retry hold and preserves Send now', () => {
    const onSendNow = vi.fn();
    const items = [queued({
      text: '请继续检查附件', content: {}, requires_explicit_retry: true,
      retry_reason: 'no_terminal_result',
    })];
    const view = render(
      <I18nextProvider i18n={i18n}>
        <QueueStrip queue={items} onRemove={vi.fn()} onRecall={vi.fn()} onSendNow={onSendNow} />
      </I18nextProvider>,
    );
    expect(screen.getByText('Retry needed · 1')).toBeTruthy();
    expect(screen.getByText(/Automatic startup retries stopped/)).toBeTruthy();
    expect(screen.getByText('请继续检查附件')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Send now' }));
    expect(onSendNow).toHaveBeenCalledTimes(1);
    view.rerender(
      <I18nextProvider i18n={i18n}>
        <QueueStrip
          queue={[{ ...items[0], requires_explicit_retry: false, metadata: { requires_explicit_retry: true } }]}
          onRemove={vi.fn()} onRecall={vi.fn()} onSendNow={onSendNow}
        />
      </I18nextProvider>,
    );
    expect(screen.queryByText(/Automatic startup retries stopped/)).toBeNull();
    expect(screen.getByText('Queued · 1')).toBeTruthy();
  });

  it('gives immediate feedback and blocks duplicate sends while flushing', () => {
    render(
      <I18nextProvider i18n={i18n}>
        <QueueStrip
          queue={[queued()]}
          onRemove={vi.fn()}
          onRecall={vi.fn()}
          onSendNow={vi.fn()}
          sendingNow
        />
      </I18nextProvider>,
    );

    const button = screen.getByRole('button', { name: 'Sending…' });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button.getAttribute('aria-busy')).toBe('true');
    expect(button.querySelector('svg')).toBeTruthy();
  });

  it('uses one shared bubble and reveals each original row boundary on hover or focus', () => {
    const html = wrap(
      <QueueStrip
        queue={[queued({ id: 'msg_1', text: 'One' }), queued({ id: 'msg_2', text: 'Two' })]}
        onRemove={() => undefined}
        onRecall={() => undefined}
        onSendNow={() => undefined}
      />,
    );

    expect(html.match(/data-queue-batch="true"/g)).toHaveLength(1);
    expect(html.match(/data-queue-row="true"/g)).toHaveLength(2);
    expect(html).toContain('hover:rounded-lg');
    expect(html).toContain('focus-within:rounded-lg');
    expect(html).not.toContain('flex-col gap-1');
  });
});

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

  // The region itself is on the row now, so the word that stood in for it while
  // it could not be drawn gives way to the thing and its name. The annotation's
  // own identity — the title — is untouched by that.
  it('shows the boxed region a screenshot-only annotation is made of', () => {
    const html = renderQueued(
      wordless({ content: withAnnotation({ direction: 'user', action: 'created' }, screenshot) }),
    );
    expect(html).toContain('User annotation');
    expect(html).toContain('annotation-region.png');
    expect(html).toContain('src="/api/media/med_9a71c33f8b2e"');
    expect(html).not.toContain('Screenshot');
  });

  // …and when there is nothing renderable to put there, the word comes back
  // rather than leaving the title alone with a separator.
  it('keeps the generic label when the region cannot be drawn', () => {
    const html = renderQueued(
      wordless({ content: withAnnotation({ direction: 'user', action: 'created' }, ['not-an-object']) }),
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

// The files waiting to be sent with a queued message. Until now the row drew
// only words, so an image-only input — the thing a Show Page annotation
// produces most often — arrived in the queue as an empty line.
describe('QueueRow — the files waiting to be sent with a queued message', () => {
  const media = (name: string, id: string) => ({ url: `/api/media/${id}`, name, kind: 'image' });
  const item = (attachments: unknown[], over: Partial<WorkbenchMessage> = {}) =>
    queued({ text: '', source: 'user', content: { attachments } as WorkbenchMessage['content'], ...over });
  const five = [
    media('one.png', 'med_1'),
    media('two.png', 'med_2'),
    media('three.png', 'med_3'),
    media('four.png', 'med_4'),
    media('five.png', 'med_5'),
  ];

  it('names an image-only queued message with its own file rather than a placeholder', () => {
    mountQueued(item([media('shot.png', 'med_1')]));

    expect(thumbs()).toHaveLength(1);
    expect(thumbs()[0].querySelector('img')?.getAttribute('src')).toBe('/api/media/med_1');
    expect(row().textContent).toContain('shot.png');
  });

  it('names the first file and counts the rest', () => {
    mountQueued(item([media('one.png', 'med_1'), media('two.png', 'med_2')]));
    expect(row().textContent).toContain('one.png and 1 more');
  });

  // A newline left behind by a paste, or a stray space, is not something the
  // author wrote — everywhere else empty text is judged on the trimmed value, and
  // this line has to agree or the row renders blank where its filename belongs.
  it.each(['   ', '\n', ' \n '])('treats whitespace-only text as wordless (%j)', (text) => {
    mountQueued(item([media('one.png', 'med_1'), media('two.png', 'med_2')], { text }));
    expect(row().textContent?.trim()).toBe('one.png and 1 more');
  });

  it('leaves authored words in charge of the line and still previews the file', () => {
    mountQueued(item([media('shot.png', 'med_1')], { text: 'Compare these two' }));

    expect(row().textContent).toContain('Compare these two');
    expect(row().textContent).not.toContain('shot.png');
    expect(thumbs()).toHaveLength(1);
  });

  it('gives an ordinary file a named chip instead of a picture', () => {
    mountQueued(item([{ url: '/api/media/med_7', name: 'console-log.txt', mime: 'text/plain' }]));

    expect(thumbs()).toHaveLength(0);
    expect(chips()[0].getAttribute('aria-label')).toBe('console-log.txt · TXT');
    expect(document.querySelector('img')).toBeNull();
  });

  // The row paints without anyone asking it to, so an <img> here is an automatic
  // request. Only the same-origin media proxy is ever allowed to become one.
  it('never renders an image the browser would fetch from somewhere else', () => {
    mountQueued(item([{ url: 'https://example.com/remote.png', name: 'remote.png', kind: 'image' }]));

    expect(document.querySelector('img')).toBeNull();
    expect(chips()).toHaveLength(1);
  });

  // The in-app viewer refuses to fetch a host that is not ours, so it would only
  // ever say "preview failed" here. A delivered attachment in the same position
  // opens an external link, and so does this one.
  it('opens a third-party file the way a delivered one does, not in the viewer', () => {
    // A signed link carries its own query. The URL is handed over exactly as it
    // arrived — nothing appended, nothing rewritten — or the signature no longer
    // matches what the far end will check.
    const signed = 'https://files.example.com/spec.pdf?sig=abc123&expires=1789';
    const { openFile } = mountQueued(item([{ url: signed, name: 'spec.pdf', mime: 'application/pdf' }]));

    const chip = chips()[0];
    expect(chip.getAttribute('data-queue-attachment')).toBe('file-external');
    expect(chip.tagName).toBe('A');
    expect(chip.getAttribute('href')).toBe(signed);
    expect(chip.getAttribute('target')).toBe('_blank');
    expect(chip.getAttribute('rel')).toBe('noopener noreferrer');
    expect(chip.getAttribute('aria-label')).toBe('spec.pdf · PDF');

    fireEvent.click(chip);
    expect(openFile).not.toHaveBeenCalled();
  });

  // An IM inbound stores ``{token, mimetype}`` where a Web upload stores
  // ``{url, mime, kind}`` — the same media object, written by a different path.
  // A screenshot pasted into Feishu has to reach the Web queue as a picture, not
  // as a chip with nothing behind it.
  it('reads the token shape an IM inbound writes, not only the upload shape', () => {
    const { openImage } = mountQueued(
      item([
        { token: 'med_im1', name: 'screenshot.png', mimetype: 'image/png', size: 4096 },
        { token: 'med_im2', name: 'trace.log', mimetype: 'text/plain', size: 12 },
      ]),
    );

    expect(thumbs()).toHaveLength(1);
    expect(thumbs()[0].querySelector('img')?.getAttribute('src')).toBe('/api/media/med_im1');
    fireEvent.click(thumbs()[0]);
    expect(openImage).toHaveBeenCalledWith('/api/media/med_im1', { isolated: true });

    // And the file beside it is openable rather than inert.
    expect(chips()[0].getAttribute('data-queue-attachment')).toBe('file');
    expect(chips()[0].getAttribute('aria-label')).toBe('trace.log · LOG');
  });

  // The escape is what keeps a minted URL inside the media-proxy route, which is
  // the same test that decides whether it may become an <img> at all.
  it('does not let a token escape the media-proxy path', () => {
    mountQueued(item([{ token: '../secrets?x=1', name: 'odd.png', mimetype: 'image/png' }]));

    expect(thumbs()[0].querySelector('img')?.getAttribute('src')).toBe(
      '/api/media/..%2Fsecrets%3Fx%3D1',
    );
  });

  it('opens an image on its own, and a file in the file viewer', () => {
    const notes = { url: '/api/media/med_9', name: 'notes.pdf', mime: 'application/pdf' };
    const { openImage, openFile } = mountQueued(item([media('shot.png', 'med_1'), notes]));

    fireEvent.click(thumbs()[0]);
    // Stated, not inferred from the gallery's current contents: a queued file is
    // not in the transcript, and must stay un-pageable even once it is.
    expect(openImage).toHaveBeenCalledWith('/api/media/med_1', { isolated: true });

    fireEvent.click(chips()[0]);
    expect(openFile).toHaveBeenCalledWith({ url: '/api/media/med_9', name: 'notes.pdf' });
  });

  it('keeps the slot, the name and a way to read it when the picture will not load', () => {
    const { openFile } = mountQueued(item([media('console-log.png', 'med_1')]));
    const slot = () => thumbs()[0].querySelector('span')!.className;
    const before = slot();

    fireEvent.error(thumbs()[0].querySelector('img')!);

    expect(thumbs()).toHaveLength(1);
    expect(slot()).toBe(before);
    expect(thumbs()[0].getAttribute('data-queue-attachment')).toBe('image-unavailable');
    expect(thumbs()[0].getAttribute('aria-label')).toBe('console-log.png · PNG · Preview unavailable');
    // The picture is gone, so the filename has to be reachable by an action —
    // the viewer titles itself with it — not only by hovering the slot.
    fireEvent.click(thumbs()[0]);
    expect(openFile).toHaveBeenCalledWith({ url: '/api/media/med_1', name: 'console-log.png' });
  });

  // Capacity is one number now, so each width renders exactly the slots it has
  // room for and exactly one disclosure — rather than both widths' markup with
  // one of each pair hidden, which put unreachable controls in the tab order and
  // let a resize blur whichever one held focus.
  it.each([
    ['narrow', false, 2, 'Show 3 more attachments'],
    ['wide', true, 3, 'Show 2 more attachments'],
  ])('shows the %s inline run and counts exactly what it hides', (_width, wide, inline, label) => {
    viewport.set(wide as boolean);
    mountQueued(item(five));

    expect(thumbs()).toHaveLength(inline as number);
    expect(more()).toHaveLength(1);
    expect(more()[0].getAttribute('aria-label')).toBe(label);
  });

  // Nothing hidden, nothing disclosed: a `+0` control would be a focusable
  // element that promises something it cannot deliver.
  it.each([
    ['narrow', false, 2],
    ['wide', true, 3],
  ])('offers no disclosure when the %s run already holds everything', (_width, wide, count) => {
    viewport.set(wide as boolean);
    mountQueued(item(five.slice(0, count as number)));

    expect(thumbs()).toHaveLength(count as number);
    expect(more()).toHaveLength(0);
  });

  it('discloses the rest and collapses again without expanding the row text', () => {
    mountQueued(item(five));
    const text = row().querySelector('div[role="button"]')!;

    expect(row().className).not.toContain('flex-wrap');
    fireEvent.click(more()[0]);

    const sheet = document.querySelector('[data-queue-attachments="disclosed"]') as HTMLElement;
    expect(within(sheet).getAllByRole('button')).toHaveLength(5);
    // Source order, unchanged by being disclosed.
    expect(within(sheet).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual(
      five.map((a) => `${a.name} · PNG`),
    );
    // The row wraps only now, and the text control is exactly as it was.
    expect(row().className).toContain('flex-wrap');
    expect(text.getAttribute('aria-expanded')).toBe('false');
    // The inline previews are gone — the sheet is the one place the files are
    // listed, so nothing is shown twice — and the one control stays put, now
    // offering the way back.
    expect(thumbs()).toHaveLength(5);
    expect(document.querySelector('[data-queue-attachments="inline"]')).toBeNull();
    expect(more()).toHaveLength(1);
    expect(more()[0].getAttribute('aria-label')).toBe('Collapse attachments');
    expect(more()[0].getAttribute('aria-expanded')).toBe('true');

    fireEvent.click(more()[0]);
    expect(document.querySelector('[data-queue-attachments="disclosed"]')).toBeNull();
    expect(row().className).not.toContain('flex-wrap');
  });

  // Unmounting the button under the user's finger is not a cosmetic problem: the
  // browser drops focus to the document, so the next Tab restarts from the top
  // of the page and the control they just used cannot be pressed again.
  it.each([
    ['narrow', false, 'Show 3 more attachments'],
    ['wide', true, 'Show 2 more attachments'],
  ])('leaves focus on the disclosure it was activated from (%s)', (_width, wide, collapsedLabel) => {
    viewport.set(wide as boolean);
    mountQueued(item(five));
    const control = more()[0];
    control.focus();

    fireEvent.click(control);

    expect(document.activeElement).toBe(control);
    expect(control.getAttribute('aria-label')).toBe('Collapse attachments');

    fireEvent.click(control);

    expect(document.activeElement).toBe(control);
    expect(control.getAttribute('aria-label')).toBe(collapsedLabel);
  });

  it('leaves the queued message itself alone while its files are inspected', () => {
    const onRemove = vi.fn();
    const onRecall = vi.fn();
    mountQueued(item(five), { onRemove, onRecall });

    fireEvent.click(thumbs()[0]);
    fireEvent.click(more()[0]);
    fireEvent.click(more()[0]);

    expect(onRemove).not.toHaveBeenCalled();
    expect(onRecall).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Remove from queue')).toBeTruthy();
  });

  // Recall drops `content.attachments`, so it stays off for a row that carries
  // any — drawing them changes what the row shows, not what it can do.
  it('still refuses to recall a message that carries files', () => {
    mountQueued(item([media('shot.png', 'med_1')]));
    expect(screen.queryByLabelText('Recall to input')).toBeNull();

    cleanup();
    mountQueued(queued({ text: 'plain', source: 'user', content: {} as WorkbenchMessage['content'] }));
    expect(screen.getByLabelText('Recall to input')).toBeTruthy();
  });

  it('keeps every attachment target a real button outside the row text control', () => {
    mountQueued(item([media('a.png', 'med_1'), { url: '/api/media/med_2', name: 'b.txt' }]));

    for (const target of [...thumbs(), ...chips(), ...more()]) {
      expect(target.tagName).toBe('BUTTON');
      expect(target.getAttribute('type')).toBe('button');
      // A sibling of the text, never inside it: inspecting a file cannot also
      // toggle the row's expansion.
      expect(target.closest('div[role="button"]')).toBeNull();
    }
  });

  // No URL to open, so no action can work — the name is shown rather than put
  // behind one, and the row still counts it.
  it('still shows and counts a file it cannot open', () => {
    mountQueued(item([{ name: 'orphan.bin' }, media('shot.png', 'med_1')]));

    expect(chips()).toHaveLength(1);
    expect(chips()[0].tagName).toBe('SPAN');
    expect(chips()[0].textContent).toContain('orphan.bin');
    expect(row().textContent).toContain('orphan.bin and 1 more');
  });

  // Crossing `sm` is the second way focus used to be lost, and the one no unit
  // test could see while capacity lived in CSS: the element holding focus became
  // `display:none`, and CSS cannot hand focus to whatever replaces it. Every case
  // below is a boundary crossing, and each has exactly one right answer — the
  // same element keeps focus, or the row's text takes it. Never the document.
  describe('across the sm breakpoint', () => {
    const text = () => row().querySelector('div[role="button"]') as HTMLElement;
    const three = five.slice(0, 3);

    it('keeps one element, and its focus, while a disclosure is still needed', () => {
      mountQueued(item(five));
      const control = more()[0];
      control.focus();

      viewport.set(true);

      // Same node, not a same-looking replacement — that distinction is the
      // whole fix, and it is what keeps the browser's focus where it was.
      expect(more()).toHaveLength(1);
      expect(more()[0]).toBe(control);
      expect(document.activeElement).toBe(control);
      // …and it now states the number this width actually hides.
      expect(control.getAttribute('aria-label')).toBe('Show 2 more attachments');
    });

    it('carries focus back the other way too', () => {
      viewport.set(true);
      mountQueued(item(five));
      const control = more()[0];
      control.focus();

      viewport.set(false);

      expect(more()[0]).toBe(control);
      expect(document.activeElement).toBe(control);
      expect(control.getAttribute('aria-label')).toBe('Show 3 more attachments');
    });

    // The reported case, end to end: three attachments, expanded narrow, widened,
    // then collapsed. The control is meaningful right up to the collapse, and
    // meaningless the instant after it.
    it('hands focus to the row text when collapsing leaves nothing to disclose', () => {
      mountQueued(item(three));
      const control = more()[0];
      control.focus();

      fireEvent.click(control);
      expect(document.activeElement).toBe(control);

      viewport.set(true);
      expect(more()[0]).toBe(control);
      expect(document.activeElement).toBe(control);

      fireEvent.click(control);

      expect(more()).toHaveLength(0);
      expect(document.body.contains(control)).toBe(false);
      expect(document.activeElement).toBe(text());
    });

    it('hands focus to the row text when widening makes the +1 unnecessary', () => {
      mountQueued(item(three));
      expect(more()[0].getAttribute('aria-label')).toBe('Show 1 more attachments');
      more()[0].focus();

      viewport.set(true);

      expect(thumbs()).toHaveLength(3);
      expect(more()).toHaveLength(0);
      expect(document.activeElement).toBe(text());
    });

    it('hands focus to the row text when narrowing drops the third preview', () => {
      viewport.set(true);
      mountQueued(item(five));
      thumbs()[2].focus();

      viewport.set(false);

      expect(thumbs()).toHaveLength(2);
      expect(document.activeElement).toBe(text());
    });

    it('leaves a surviving preview holding its own focus', () => {
      viewport.set(true);
      mountQueued(item(five));
      const first = thumbs()[0];
      first.focus();

      viewport.set(false);

      expect(document.activeElement).toBe(first);
    });

    // The transfer is conditional on this group having had focus in the first
    // place. A user part-way through the row's own actions must not be dragged
    // back to its text because an attachment control happened to disappear.
    it('never takes focus from a control that is not its own', () => {
      mountQueued(item(three));
      const remove = screen.getByLabelText('Remove from queue');
      remove.focus();

      viewport.set(true);

      expect(more()).toHaveLength(0);
      expect(document.activeElement).toBe(remove);
    });

    // A mouse user has not focused the button, so collapsing it away must not
    // move focus either — there was none in the group to move.
    it('moves nothing when the disclosure is clicked without focus', () => {
      mountQueued(item(three));
      viewport.set(true);
      expect(more()).toHaveLength(0);

      viewport.set(false);
      fireEvent.click(more()[0]);
      fireEvent.click(more()[0]);

      expect(document.activeElement).toBe(document.body);
    });
  });
});
