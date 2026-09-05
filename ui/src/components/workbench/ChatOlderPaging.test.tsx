/** @vitest-environment jsdom */

import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { createRef, type ComponentProps } from 'react';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../../context/ApiContext', () => ({ useApi: () => ({}) }));
vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({
    capabilities: { can_chat: true, can_manage_instance: false, can_use_show_pages: false, can_use_vault_secrets: false },
  }),
}));
vi.mock('../../context/WindowManagerContext', () => ({
  useWindowManager: () => ({ focusedId: null, focusCanvas: vi.fn() }),
}));
vi.mock('../../lib/useIsDesktop', () => ({ isDesktopViewport: () => false, useIsDesktop: () => false }));
// The transcript does not render the composer's browser-only Lexical stack.
vi.mock('./Composer', () => ({ Composer: () => null }));

import { Transcript } from './ChatPage';
import type { WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';

type Props = ComponentProps<typeof Transcript>;
const messages = (first: number, count: number): WorkbenchMessage[] =>
  Array.from({ length: count }, (_, index) => ({
    id: `msg-${first + index}`,
    session_id: 'session-1',
    platform: 'avibe',
    author: 'user',
    type: 'user',
    text: `Message ${first + index}`,
    content: {},
    metadata: {},
    created_at: new Date(Date.UTC(2026, 8, 1, 0, 0, first + index)).toISOString(),
  }) as WorkbenchMessage);

const pendingPage = () => {
  let land!: (ok: boolean) => void;
  const promise = new Promise<boolean>((resolve) => { land = resolve; });
  return { promise, land };
};

let nextFrameId = 1;
const frames = new Map<number, FrameRequestCallback>();
const flushFrames = () => {
  const queued = [...frames.values()];
  frames.clear();
  queued.forEach((callback) => callback(0));
};

class TestResizeObserver {
  static live = new Set<TestResizeObserver>();
  constructor(private callback: ResizeObserverCallback) { TestResizeObserver.live.add(this); }
  observe() {}
  unobserve() {}
  disconnect() { TestResizeObserver.live.delete(this); }
  static resize() { for (const observer of TestResizeObserver.live) observer.callback([], observer as unknown as ResizeObserver); }
}

const renderTranscript = (over: Partial<Props> = {}) => {
  const props: Props = {
    messages: messages(301, 50),
    session: { id: 'session-1', title: 'Session', agent_name: null, archived_at: null } as WorkbenchSession,
    agentDisplayName: null,
    working: false,
    hasOlder: true,
    loadingOlder: false,
    onLoadOlder: vi.fn().mockResolvedValue(true),
    needsLatestReload: false,
    onReloadLatest: vi.fn().mockResolvedValue(true),
    jumpTarget: null,
    onJumpHandled: vi.fn(),
    highlightedId: null,
    messageFontSize: 14,
    onQuickReply: vi.fn(),
    provisionRequestsByMessage: new Map(),
    onVaultRequestResolved: vi.fn(),
    onQuoteSelection: vi.fn(),
    onAskInNewSession: vi.fn(),
    readOnly: false,
    followingTailRef: createRef<boolean>() as React.MutableRefObject<boolean>,
    ...over,
  };
  const view = render(<MemoryRouter><Transcript {...props} /></MemoryRouter>);
  const scroller = view.getByTestId('chat-transcript');
  const geometry = { extraAbove: 0 };
  let scrollTop = 0;
  const loadingSlotHeight = () => scroller.querySelector('[role="status"], button')?.classList.contains('h-8') ? 44 : 0;
  Object.defineProperties(scroller, {
    clientHeight: { configurable: true, get: () => 800 },
    scrollHeight: { configurable: true, get: () => scroller.querySelectorAll('[data-message-id]').length * 112 + 40 + loadingSlotHeight() + geometry.extraAbove },
    scrollTop: {
      configurable: true,
      get: () => scrollTop,
      set: (value: number) => { scrollTop = Math.max(0, Math.min(value, scroller.scrollHeight - scroller.clientHeight)); },
    },
  });
  scroller.getBoundingClientRect = () => ({ top: 10, bottom: 810, height: 800 }) as DOMRect;
  // jsdom has no layout. Derive every row's position from the committed DOM,
  // including prepended rows and the transient loading slot + column gap.
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    if (this.dataset.messageId === undefined) return { top: 0, bottom: 0, height: 0 } as DOMRect;
    const rows = Array.from(scroller.querySelectorAll('[data-message-id]'));
    const index = rows.indexOf(this);
    const top = 30 + index * 112 + loadingSlotHeight() + (index > 0 ? geometry.extraAbove : 0) - scrollTop;
    return { top, bottom: top + 100, height: 100 } as DOMRect;
  });
  act(flushFrames);
  const rerender = (next: Partial<Props>) => {
    Object.assign(props, next);
    view.rerender(<MemoryRouter><Transcript {...props} /></MemoryRouter>);
  };
  const scrollTo = (top: number) => {
    scroller.scrollTop = top;
    fireEvent.scroll(scroller);
  };
  return { ...view, scroller, geometry, props, rerender, scrollTo };
};

describe('transcript older-page loading', () => {
  beforeEach(() => {
    vi.stubGlobal('CSS', { escape: (value: string) => value });
    TestResizeObserver.live.clear();
    vi.stubGlobal('ResizeObserver', TestResizeObserver);
    vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} unobserve() {} });
    nextFrameId = 1;
    frames.clear();
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      const id = nextFrameId++;
      frames.set(id, callback);
      return id;
    });
    vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('requires fresh upward input for each page, even when a page adds no visible rows', async () => {
    const page = pendingPage();
    const onLoadOlder = vi.fn().mockReturnValueOnce(page.promise).mockResolvedValue(true);
    const { scroller, scrollTo, rerender } = renderTranscript({ onLoadOlder });
    scrollTo(0);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    rerender({ loadingOlder: true });
    fireEvent.wheel(scroller, { deltaY: -100 });
    fireEvent.scroll(scroller);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    await act(async () => { rerender({ loadingOlder: false }); page.land(true); });
    act(TestResizeObserver.resize);
    fireEvent.scroll(scroller);
    expect(scroller.scrollTop).toBe(0);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    await act(async () => fireEvent.wheel(scroller, { deltaY: -100 }));
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  it.each([50, 300])('preserves the reader through a prepend with %i retained rows and no resize notification', async (count) => {
    const page = pendingPage();
    const onLoadOlder = vi.fn().mockReturnValueOnce(page.promise).mockReturnValue(new Promise(() => {}));
    const { scroller, scrollTo, rerender, geometry } = renderTranscript({ messages: messages(301, count), onLoadOlder });
    scrollTo(0);
    const anchor = scroller.querySelector('[data-message-id="msg-301"]')!;
    const before = anchor.getBoundingClientRect().top;
    const height = scroller.scrollHeight;
    rerender({ loadingOlder: true });
    expect(anchor.getBoundingClientRect().top).toBe(before);
    expect(scroller.scrollTop).toBe(44);

    await act(async () => {
      rerender({ messages: messages(251, Math.min(count + 50, 300)), loadingOlder: false });
      page.land(true);
    });
    expect(anchor.getBoundingClientRect().top).toBe(before);
    expect(scroller.scrollTop).toBe(50 * 112);
    if (count === 300) expect(scroller.scrollHeight).toBe(height);
    // A queued scroll from mounting/removing the spinner sees the restored DOM.
    fireEvent.scroll(scroller);
    rerender({ agentDisplayName: 'Renamed agent' });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    geometry.extraAbove = 180;
    act(TestResizeObserver.resize);
    fireEvent.scroll(scroller);
    expect(anchor.getBoundingClientRect().top).toBe(before);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    scrollTo(0);
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  it('allows another upward scroll inside the trigger band without a down-and-up detour', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { scrollTo } = renderTranscript({ onLoadOlder });
    await act(async () => scrollTo(110));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    await act(async () => scrollTo(100));
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  const upwardInputs = {
    wheel: (el: HTMLElement) => fireEvent.wheel(el, { deltaY: -100 }),
    touch: (el: HTMLElement) => {
      fireEvent.touchStart(el, { touches: [{ clientY: 100 }] });
      fireEvent.touchMove(el, { touches: [{ clientY: 140 }] });
      fireEvent.touchEnd(el);
    },
    keyboard: (el: HTMLElement) => fireEvent.keyDown(el, { key: 'PageUp' }),
  };
  it.each(Object.entries(upwardInputs))('accepts %s input at the hard top without a scroll event', async (_name, upward) => {
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { scroller, rerender, props } = renderTranscript({ messages: messages(1, 1), onLoadOlder });
    act(TestResizeObserver.resize);
    rerender({ agentDisplayName: 'Renamed agent' });
    expect(scroller.scrollTop).toBe(0);
    expect(onLoadOlder).not.toHaveBeenCalled();
    await act(async () => upward(scroller));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    expect(props.followingTailRef.current).toBe(false);
  });

  it('does not interpret downward, pinch, or editing input as a request for history', () => {
    const onLoadOlder = vi.fn();
    const { scroller } = renderTranscript({ messages: messages(1, 1), onLoadOlder });
    fireEvent.wheel(scroller, { deltaY: 100 });
    fireEvent.keyDown(scroller, { key: 'PageDown' });
    fireEvent.touchStart(scroller, { touches: [{ clientY: 100 }] });
    fireEvent.touchMove(scroller, { touches: [{ clientY: 80 }] });
    fireEvent.touchMove(scroller, { touches: [{ clientY: 140 }, { clientY: 160 }] });
    const input = document.createElement('input');
    scroller.append(input);
    fireEvent.keyDown(input, { key: 'Home' });
    expect(onLoadOlder).not.toHaveBeenCalled();
  });

  it('serializes multiple upward inputs until the active request settles', async () => {
    const first = pendingPage();
    const onLoadOlder = vi.fn().mockReturnValue(first.promise);
    const { scroller, scrollTo } = renderTranscript({ onLoadOlder });
    scrollTo(0);
    for (const upward of Object.values(upwardInputs)) upward(scroller);
    scrollTo(500);
    scrollTo(0);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    await act(async () => first.land(true));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });

  it('stops when history is exhausted', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { scroller, scrollTo, rerender } = renderTranscript({ onLoadOlder });
    await act(async () => scrollTo(0));
    rerender({ hasOlder: false });
    for (const upward of Object.values(upwardInputs)) upward(scroller);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });

  it('keeps a deep-link jump stable until the reader asks for more history', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { scroller, scrollTo, rerender } = renderTranscript({ onLoadOlder, jumpTarget: 'missing-target' });
    scrollTo(0);
    fireEvent.wheel(scroller, { deltaY: -100 });
    expect(onLoadOlder).not.toHaveBeenCalled();
    act(flushFrames);
    rerender({ jumpTarget: null });
    expect(onLoadOlder).not.toHaveBeenCalled();
    await act(async () => fireEvent.wheel(scroller, { deltaY: -100 }));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });

  it('surfaces failures and offers one explicit retry without an automatic retry loop', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(false);
    const { getByText, scroller, scrollTo } = renderTranscript({ onLoadOlder });
    await act(async () => scrollTo(0));
    expect(getByText('chat.olderLoadFailed')).toBeTruthy();
    act(TestResizeObserver.resize);
    fireEvent.wheel(scroller, { deltaY: -100 });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    await act(async () => fireEvent.click(getByText('chat.olderLoadFailed')));
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
    scrollTo(500);
    await act(async () => scrollTo(0));
    expect(onLoadOlder).toHaveBeenCalledTimes(3);
  });

  it('does not hold a failure against a reader who left before it arrived', async () => {
    const first = pendingPage();
    const onLoadOlder = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(true);
    const { scrollTo, queryByText } = renderTranscript({ onLoadOlder });
    scrollTo(0);
    scrollTo(500);
    await act(async () => first.land(false));
    expect(queryByText('chat.olderLoadFailed')).toBeNull();
    await act(async () => scrollTo(0));
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  it('keeps an old session request from settling a new session request', async () => {
    const previous = pendingPage();
    const current = pendingPage();
    const onLoadOlder = vi.fn().mockReturnValueOnce(previous.promise).mockReturnValue(current.promise);
    const { scroller, scrollTo, rerender, props, queryByText } = renderTranscript({ onLoadOlder });
    scrollTo(0);
    rerender({ session: { ...props.session, id: 'session-2' }, messages: messages(401, 50) });
    act(flushFrames);
    scrollTo(0);
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
    await act(async () => previous.land(false));
    expect(queryByText('chat.olderLoadFailed')).toBeNull();
    fireEvent.wheel(scroller, { deltaY: -100 });
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
    await act(async () => current.land(true));
  });
});
