/* @vitest-environment jsdom */

import type { ReactElement } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';

// On a phone, sending a message ends the typing: the soft keyboard closes as the
// message leaves (the field gives up focus), whether it was sent or queued. A
// desktop, and a tablet typing on a hardware keyboard, keep the field focused for
// the next message. A send the server refuses gives the text back without
// raising the keyboard again; a tap on the field does that.
//
// The device is set up before the modules load, as the app meets it: whether the
// screen is touch, and the visual viewport the keyboard shrinks.

class FakeViewport extends EventTarget {
  height = 844;
  width = 390;
  offsetTop = 0;
  offsetLeft = 0;
  pageTop = 0;
  scale = 1;
}

type Device = { touch: boolean };
let viewport: FakeViewport;

async function load({ touch }: Device) {
  vi.resetModules();
  Object.defineProperty(navigator, 'maxTouchPoints', { configurable: true, value: touch ? 5 : 0 });
  viewport = new FakeViewport();
  Object.defineProperty(window, 'visualViewport', { configurable: true, value: viewport });
  const [{ Composer }, { ToastProvider }, i18next, reactI18next, { createRef }] = await Promise.all([
    import('./Composer'),
    import('../../context/ToastProvider'),
    import('i18next'),
    import('react-i18next'),
    import('react'),
    // The chat page loads its composer's editor before it mounts (routePages.ts).
    import('./deferredMentionEditor').then((m) => m.DeferredMentionEditor.preload()),
  ]);
  const i18n = i18next.createInstance();
  await i18n.use(reactI18next.initReactI18next).init({
    lng: 'en',
    fallbackLng: 'en',
    resources: { en: { translation: en } },
    interpolation: { escapeValue: false },
  });
  const providers = (ui: ReactElement) => (
    <reactI18next.I18nextProvider i18n={i18n}>
      <ToastProvider>{ui}</ToastProvider>
    </reactI18next.I18nextProvider>
  );
  return { Composer, providers, createRef };
}

/** The soft keyboard opens over the focused field: the visual viewport shrinks. */
const openKeyboard = () => {
  act(() => {
    viewport.height = 500;
    viewport.dispatchEvent(new Event('resize'));
  });
};

const mentionSearch = { onSearchAgents: async () => [], onSearchSessions: async () => [] };
const editor = () => document.querySelector<HTMLElement>('[contenteditable="true"]')!;
const textarea = () => screen.getByRole('textbox') as HTMLTextAreaElement;

// jsdom lacks two things a browser editor has: an element's own editability,
// and a range's geometry (the editor scrolls its caret into view).
if (!('isContentEditable' in HTMLElement.prototype)) {
  Object.defineProperty(HTMLElement.prototype, 'isContentEditable', {
    configurable: true,
    get(this: HTMLElement) {
      return this.closest('[contenteditable="true"]') !== null;
    },
  });
}
Range.prototype.getBoundingClientRect = () => new DOMRect();
Range.prototype.getClientRects = () => ({ length: 0, item: () => null, [Symbol.iterator]: [][Symbol.iterator] }) as unknown as DOMRectList;

beforeEach(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false, addEventListener() {}, removeEventListener() {} }),
  });
});

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(navigator, 'maxTouchPoints');
  Reflect.deleteProperty(window, 'visualViewport');
});

describe('Composer closes the soft keyboard on send', () => {
  it('blurs the chat editor the moment a message is sent on a phone', async () => {
    const { Composer, providers } = await load({ touch: true });
    let answer!: (started: boolean | void) => void;
    const onSend = vi.fn(() => new Promise<boolean | void>((resolve) => { answer = resolve; }));
    render(providers(<Composer sessionId="ses-1" initialDraft="hello" onSend={onSend} {...mentionSearch} />));
    act(() => editor().focus());
    openKeyboard();
    expect(document.activeElement).toBe(editor());

    fireEvent.click(screen.getByLabelText(en.chat.compose.send));

    // Before the server answers.
    expect(onSend).toHaveBeenCalled();
    expect(document.activeElement).not.toBe(editor());
    // Nothing puts focus back while the editor is emptied.
    await act(async () => undefined);
    expect(document.activeElement).not.toBe(editor());
    await act(async () => answer(undefined));
    expect(document.activeElement).not.toBe(editor());
  });

  it('blurs the chat editor when a message goes to the queue on a phone', async () => {
    const { Composer, providers } = await load({ touch: true });
    render(providers(<Composer sessionId="ses-1" busy initialDraft="hello" onSend={vi.fn(async () => undefined)} {...mentionSearch} />));
    act(() => editor().focus());
    openKeyboard();

    fireEvent.click(screen.getByLabelText(en.chat.compose.queueSend));

    await act(async () => undefined);
    expect(document.activeElement).not.toBe(editor());
  });

  it('blurs the plain textarea too', async () => {
    const { Composer, providers } = await load({ touch: true });
    render(providers(<Composer sessionId="ses-1" onSend={vi.fn(async () => undefined)} />));
    act(() => textarea().focus());
    fireEvent.change(textarea(), { target: { value: 'hello' } });
    openKeyboard();

    fireEvent.click(screen.getByLabelText(en.chat.compose.send));

    await act(async () => undefined);
    expect(document.activeElement).not.toBe(textarea());
  });

  it('keeps the field focused on a desktop', async () => {
    const { Composer, providers } = await load({ touch: false });
    const onSend = vi.fn(async () => undefined);
    render(providers(<Composer sessionId="ses-1" initialDraft="hello" onSend={onSend} {...mentionSearch} />));
    act(() => editor().focus());

    fireEvent.click(screen.getByLabelText(en.chat.compose.send));

    await waitFor(() => expect(onSend).toHaveBeenCalled());
    await act(async () => undefined);
    expect(document.activeElement).toBe(editor());
    // And the caret is in it: the next message can be typed at once.
    expect(editor().contains(window.getSelection()?.anchorNode ?? null)).toBe(true);
  });

  it('keeps the field focused on a touch screen typing on a hardware keyboard', async () => {
    const { Composer, providers } = await load({ touch: true });
    const onSend = vi.fn(async () => undefined);
    render(providers(<Composer sessionId="ses-1" onSend={onSend} />));
    act(() => textarea().focus());
    fireEvent.change(textarea(), { target: { value: 'hello' } });
    // No on-screen keyboard: the visual viewport keeps its height.

    fireEvent.click(screen.getByLabelText(en.chat.compose.send));

    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(document.activeElement).toBe(textarea());
  });

  it('gives a refused message back without focusing the editor again', async () => {
    const { Composer, providers } = await load({ touch: true });
    render(providers(<Composer sessionId="ses-1" initialDraft="hello" onSend={vi.fn(async () => false)} {...mentionSearch} />));
    act(() => editor().focus());
    openKeyboard();

    fireEvent.click(screen.getByLabelText(en.chat.compose.send));

    await waitFor(() => expect(editor().textContent).toBe('hello'));
    await act(async () => undefined);
    expect(document.activeElement).not.toBe(editor());
  });
});

// iOS reports the keyboard's pan as the visual viewport's offset and already
// applies it to the caret's rect; Lexical's scroll-into-view counts it again and
// scrolls the page by most of it on the first keystroke, dropping the composer
// under the keyboard. While a soft keyboard is open the editor leaves the page
// where it is; elsewhere Lexical still brings the caret into view.
describe('Composer leaves the page where it is while typing on a soft keyboard', () => {
  const typeInto = (root: HTMLElement, handle: { appendText: (text: string) => void }) => {
    act(() => {
      // A keystroke, and the edit it makes, in one turn of the event loop.
      root.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
      handle.appendText('a');
    });
  };

  it('does not scroll the page on a phone with the keyboard panned', async () => {
    const { Composer, providers, createRef } = await load({ touch: true });
    const ref = createRef<import('./Composer').ComposerHandle>();
    render(providers(<Composer ref={ref} sessionId="ses-1" onSend={vi.fn()} {...mentionSearch} />));
    act(() => editor().focus());
    act(() => {
      viewport.height = 415;
      viewport.offsetTop = 397;
      viewport.dispatchEvent(new Event('resize'));
    });
    const scrollBy = vi.spyOn(window, 'scrollBy').mockImplementation(() => {});

    typeInto(editor(), ref.current!);
    await act(async () => undefined);

    expect(editor().textContent).toContain('a');
    expect(scrollBy).not.toHaveBeenCalled();
    scrollBy.mockRestore();
  });

  it('still brings the caret into view on a desktop', async () => {
    const { Composer, providers, createRef } = await load({ touch: false });
    const ref = createRef<import('./Composer').ComposerHandle>();
    render(providers(<Composer ref={ref} sessionId="ses-1" onSend={vi.fn()} {...mentionSearch} />));
    act(() => editor().focus());
    // The caret sits above the visible band (a desktop page scrolled down).
    viewport.offsetTop = 397;
    const scrollBy = vi.spyOn(window, 'scrollBy').mockImplementation(() => {});

    typeInto(editor(), ref.current!);
    await act(async () => undefined);

    expect(scrollBy).toHaveBeenCalled();
    scrollBy.mockRestore();
  });
});

