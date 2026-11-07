/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { createRef, type RefObject } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.hoisted(() => {
  Range.prototype.getBoundingClientRect = () => new DOMRect();
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  });
});
const apiFetch = vi.hoisted(() => vi.fn());
vi.mock('../../lib/apiFetch', () => ({ apiFetch }));
const originalScrollIntoView = Element.prototype.scrollIntoView;

import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import { Composer, type ComposerHandle } from './Composer';
import { DeferredMentionEditor } from './deferredMentionEditor';

// A chat painted from its snapshot mounts its composer before the bootstrap
// answers, with the draft this device last cached, and keeps that composer when
// the bootstrap lands (ChatPage.localSnapshot.test.tsx). The bootstrap's saved
// draft then reaches the box only while the user has not touched it: what they
// typed meanwhile is never overwritten, and the late seed is not re-saved as if
// they had typed it.

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

type View = { initialDraft: string | null; mentions: boolean; handle?: RefObject<ComposerHandle | null> };

const composer = ({ initialDraft, mentions, handle }: View, onDraftChange: (text: string) => void) => (
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <Composer
        ref={handle}
        sessionId="ses_a"
        onSend={() => undefined}
        initialDraft={initialDraft}
        onDraftChange={onDraftChange}
        onSearchAgents={mentions ? async () => [] : undefined}
        onSearchSessions={mentions ? async () => [] : undefined}
      />
    </ToastProvider>
  </I18nextProvider>
);

const textOf = (box: HTMLElement) => (box instanceof HTMLTextAreaElement ? box.value : box.textContent ?? '');

// A user's own edit. jsdom cannot type into the rich editor, so that path takes
// the quote action, which is the same kind of user edit (it lands as a normal
// editor change, not a seed).
const typeInto = async (mentions: boolean, handle: RefObject<ComposerHandle | null>, text: string) => {
  if (mentions) {
    act(() => handle.current!.appendText(text));
    return;
  }
  const user = userEvent.setup();
  await user.click(screen.getByRole('textbox'));
  await user.keyboard(text);
};

// The chat page loads its composer's editor before it mounts (routePages.ts).
beforeAll(async () => {
  await DeferredMentionEditor.preload();
});

beforeEach(() => {
  apiFetch.mockReset().mockResolvedValue({
    ok: true,
    json: async () => ({ available: false, max_file_bytes: null }),
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  Reflect.deleteProperty(window, 'avibeNative');
  Reflect.deleteProperty(window, 'visualViewport');
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  });
  if (originalScrollIntoView) Element.prototype.scrollIntoView = originalScrollIntoView;
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
});

describe.each([
  { path: 'mention editor', mentions: true },
  { path: 'textarea', mentions: false },
])('Composer saved draft read again after mount ($path)', ({ mentions }) => {
  it('replaces the draft while the user has not touched it, without saving it as an edit', async () => {
    const onDraftChange = vi.fn();
    const view = render(composer({ initialDraft: 'cached on this device', mentions }, onDraftChange));
    const box = await screen.findByRole('textbox');
    await waitFor(() => expect(textOf(box)).toBe('cached on this device'));

    view.rerender(composer({ initialDraft: 'saved on another device', mentions }, onDraftChange));
    await waitFor(() => expect(textOf(screen.getByRole('textbox'))).toBe('saved on another device'));
    expect(onDraftChange).not.toHaveBeenCalled();
  });

  it('never overwrites what the user typed', async () => {
    const handle = createRef<ComposerHandle>();
    const onDraftChange = vi.fn();
    const view = render(composer({ initialDraft: null, mentions, handle }, onDraftChange));
    await screen.findByRole('textbox');
    await typeInto(mentions, handle, 'typed before the bootstrap');
    await waitFor(() => expect(textOf(screen.getByRole('textbox'))).toBe('typed before the bootstrap'));

    view.rerender(composer({ initialDraft: 'older server draft', mentions, handle }, onDraftChange));
    await act(async () => undefined);
    expect(textOf(screen.getByRole('textbox'))).toBe('typed before the bootstrap');
  });

  it('never refills a box the user sent from and emptied', async () => {
    const handle = createRef<ComposerHandle>();
    const view = render(composer({ initialDraft: null, mentions, handle }, () => undefined));
    await screen.findByRole('textbox');
    await typeInto(mentions, handle, 'sent before the bootstrap');
    await waitFor(() => expect(textOf(screen.getByRole('textbox'))).toBe('sent before the bootstrap'));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: en.chat.compose.send }));
    });
    await waitFor(() => expect(textOf(screen.getByRole('textbox'))).toBe(''));

    // The bootstrap read the draft before the send cleared it.
    view.rerender(composer({ initialDraft: 'sent before the bootstrap', mentions, handle }, () => undefined));
    await act(async () => undefined);
    expect(textOf(screen.getByRole('textbox'))).toBe('');
  });
});

describe('Composer programmatic Home focus', () => {
  it('reveals the complete composer after the native keyboard viewport settles', async () => {
    vi.useFakeTimers();
    const viewport = Object.assign(new EventTarget(), {
      height: 500,
      width: 390,
      offsetTop: 0,
      offsetLeft: 0,
      pageTop: 0,
      pageLeft: 0,
      scale: 1,
    });
    Object.defineProperty(window, 'visualViewport', {
      configurable: true,
      value: viewport,
    });
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }),
    });
    Object.defineProperty(window, 'avibeNative', {
      configurable: true,
      value: {
        contract: 1,
        platform: 'ios',
        shellVersion: '1.0 (1)',
        capabilities: { keyboardResizesViewport: 1 },
        call: vi.fn(async () => null),
        on: vi.fn(() => () => undefined),
      },
    });
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    const handle = createRef<ComposerHandle>();
    const view = render(composer({ initialDraft: null, mentions: false, handle }, () => undefined));
    const root = view.container.querySelector<HTMLElement>('[data-composer-root]')!;
    root.getBoundingClientRect = () => ({
      x: 0,
      y: 430,
      top: 430,
      right: 390,
      bottom: 560,
      left: 0,
      width: 390,
      height: 130,
      toJSON: () => ({}),
    });

    act(() => handle.current!.setDraft('Add a Bot that owns security'));
    expect(document.activeElement).toBe(screen.getByRole('textbox'));
    act(() => {
      viewport.dispatchEvent(new Event('resize'));
      vi.advanceTimersByTime(150);
    });

    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'end', inline: 'nearest' });
  });
});
