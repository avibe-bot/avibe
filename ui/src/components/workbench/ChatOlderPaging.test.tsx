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
  // ``overscroll``: how far past the bottom the platform's rubber band may pull.
  const geometry = { extraAbove: 0, clientHeight: 800, overscroll: 0 };
  let scrollTop = 0;
  // jsdom has no layout, so geometry comes from the committed content column's
  // direct children: the head slot and the rows, which pickScrollAnchor walks too.
  // Walk siblings instead of querying the subtree. The anchor search reads one
  // rect per row above the viewport, and a subtree query per rect made each
  // scroll cost (rows above) x (whole DOM): seconds over 300 rows on CI.
  // The content column, inside the box that clips the conversation's glide.
  const column = () => scroller.firstElementChild!.firstElementChild!;
  const rowsFrom = (node: Element | null) => {
    let count = 0;
    for (; node; node = node.previousElementSibling) if ((node as HTMLElement).dataset.messageId !== undefined) count += 1;
    return count;
  };
  // The head slot above the rows (held while older history exists; loading,
  // failed-load retry, or end of history), plus the column gap.
  const loadingSlotHeight = () => {
    for (const child of column().children) {
      if ((child as HTMLElement).dataset.messageId !== undefined) return 0;
      if (child.classList.contains('h-8')) return 44;
    }
    return 0;
  };
  Object.defineProperties(scroller, {
    clientHeight: { configurable: true, get: () => geometry.clientHeight },
    scrollHeight: { configurable: true, get: () => rowsFrom(column().lastElementChild) * 112 + 40 + loadingSlotHeight() + geometry.extraAbove },
    scrollTop: {
      configurable: true,
      get: () => scrollTop,
      set: (value: number) => { scrollTop = Math.max(0, Math.min(value, scroller.scrollHeight - scroller.clientHeight + geometry.overscroll)); },
    },
  });
  scroller.getBoundingClientRect = () => ({ top: 10, bottom: 10 + geometry.clientHeight, height: geometry.clientHeight }) as DOMRect;
  // A row's position counts the rows before it and the transient loading slot.
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    // The content column scrolls with the rows, below the container's padding.
    if (this === column()) return { top: 30 - scrollTop, bottom: 30 - scrollTop + scroller.scrollHeight - 40, height: scroller.scrollHeight - 40 } as DOMRect;
    if (this.dataset.messageId === undefined) return { top: 0, bottom: 0, height: 0 } as DOMRect;
    const index = rowsFrom(this.previousElementSibling);
    const top = 30 + index * 112 + loadingSlotHeight() + (index > 0 ? geometry.extraAbove : 0) - scrollTop;
    return { top, bottom: top + 100, height: 100 } as DOMRect;
  });
  act(flushFrames);
  const rerender = (next: Partial<Props>) => {
    Object.assign(props, next);
    view.rerender(<MemoryRouter><Transcript {...props} /></MemoryRouter>);
  };
  const scrollTo = (top: number) => {
    fireEvent.pointerDown(scroller, { pointerType: 'mouse' });
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
    const now = vi.spyOn(performance, 'now').mockReturnValue(0);
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
    now.mockReturnValue(300);
    await act(async () => fireEvent.wheel(scroller, { deltaY: -100 }));
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  it('shows the older page on its way as a thin block in its slot, not a spinner', () => {
    const { scroller, rerender } = renderTranscript();
    rerender({ loadingOlder: true });
    const slot = scroller.querySelector('[role="status"]');
    expect(slot?.className).toContain('h-8');
    expect(slot?.querySelector('[aria-hidden]')).not.toBeNull();
    expect(scroller.querySelector('.animate-spin')).toBeNull();
  });

  it.each([50, 300])('preserves the reader through a prepend with %i retained rows and no resize notification', async (count) => {
    const page = pendingPage();
    const onLoadOlder = vi.fn().mockReturnValueOnce(page.promise).mockReturnValue(new Promise(() => {}));
    const { scroller, scrollTo, rerender, geometry } = renderTranscript({ messages: messages(301, count), onLoadOlder });
    scrollTo(0);
    const anchor = scroller.querySelector('[data-message-id="msg-301"]')!;
    const before = anchor.getBoundingClientRect().top;
    const height = scroller.scrollHeight;
    // The head slot holds its height while older history exists, so starting
    // the load moves nothing and writes nothing.
    rerender({ loadingOlder: true });
    expect(anchor.getBoundingClientRect().top).toBe(before);
    expect(scroller.scrollTop).toBe(0);

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

  it('consumes one touch drag across settled empty pages and momentum scrolls', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { scroller } = renderTranscript({ messages: messages(1, 1), onLoadOlder });
    fireEvent.touchStart(scroller, { touches: [{ clientY: 100 }] });
    for (const clientY of [140, 180, 220]) {
      await act(async () => fireEvent.touchMove(scroller, { touches: [{ clientY }] }));
      expect(onLoadOlder).toHaveBeenCalledTimes(1);
    }
    fireEvent.touchEnd(scroller);
    fireEvent.scroll(scroller);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    await act(async () => upwardInputs.touch(scroller));
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  it('keeps a wheel burst consumed through momentum and rearms after an idle gap', async () => {
    const now = vi.spyOn(performance, 'now').mockReturnValue(0);
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { scroller } = renderTranscript({ messages: messages(1, 1), onLoadOlder });
    for (const timestamp of [0, 50, 150, 250]) {
      now.mockReturnValue(timestamp);
      await act(async () => upwardInputs.wheel(scroller));
      expect(onLoadOlder).toHaveBeenCalledTimes(1);
    }
    now.mockReturnValue(550);
    await act(async () => upwardInputs.wheel(scroller));
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  it('shares gesture consumption with scroll events inside the trigger band', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { scroller, scrollTo } = renderTranscript({ onLoadOlder });
    scrollTo(200);
    fireEvent.touchStart(scroller, { touches: [{ clientY: 100 }] });
    for (const top of [110, 90]) {
      await act(async () => {
        scroller.scrollTop = top;
        fireEvent.scroll(scroller);
      });
      expect(onLoadOlder).toHaveBeenCalledTimes(1);
    }
    fireEvent.touchEnd(scroller);
    await act(async () => {
      scroller.scrollTop = 70;
      fireEvent.scroll(scroller);
    });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    await act(async () => upwardInputs.touch(scroller));
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  // A long fling reads through history: the page it asked for landed above the
  // reader, so the same gesture may ask for the next one.
  it('lets a gesture whose page landed above the reader ask for the next one', async () => {
    const first = pendingPage();
    const onLoadOlder = vi.fn().mockReturnValueOnce(first.promise).mockReturnValue(new Promise(() => {}));
    const { scroller, rerender } = renderTranscript({ onLoadOlder });
    // The fling's own scrolling, with no new touch.
    const flingTo = (top: number) => act(async () => {
      scroller.scrollTop = top;
      fireEvent.scroll(scroller);
    });
    fireEvent.touchStart(scroller, { touches: [{ clientY: 100 }] });
    fireEvent.touchMove(scroller, { touches: [{ clientY: 140 }] });
    fireEvent.touchEnd(scroller);
    await flingTo(3000);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    rerender({ loadingOlder: true });
    await act(async () => {
      rerender({ messages: messages(251, 100), loadingOlder: false });
      first.land(true);
    });
    // The page landed above: the reader is a page further from the top.
    expect(scroller.scrollTop).toBe(3000 + 50 * 112);
    await flingTo(8000);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    await flingTo(6000);
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  // At the 300-row cap a landing page drops the newest rows. Prefetching from
  // where those rows are on screen would take them, and the reader's place, away.
  it('asks for a page that would drop rows near the reader only from the top', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { scrollTo, geometry } = renderTranscript({ messages: messages(1, 300), onLoadOlder });
    // A tall viewport puts the whole window inside the prefetch band.
    geometry.clientHeight = 8000;
    scrollTo(300 * 112 + 84 - 8000);
    await act(async () => scrollTo(25000));
    expect(onLoadOlder).not.toHaveBeenCalled();
    // Far enough up, the rows a page would drop are screens below.
    await act(async () => scrollTo(5000));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });

  // Following the tail resumes only when the reader comes back to it, not when
  // a shift above them is compensated while they stand near the bottom.
  it('stays off the tail through a compensated shift near the bottom', async () => {
    const { scroller, geometry, props } = renderTranscript({ hasOlder: false });
    expect(props.followingTailRef.current).toBe(true);
    scroller.scrollTop -= 40;
    fireEvent.scroll(scroller);
    expect(props.followingTailRef.current).toBe(false);
    geometry.extraAbove = 180;
    act(TestResizeObserver.resize);
    fireEvent.scroll(scroller);
    expect(props.followingTailRef.current).toBe(false);
    scroller.scrollTop = scroller.scrollHeight;
    fireEvent.scroll(scroller);
    expect(props.followingTailRef.current).toBe(true);
  });

  // iOS takes back a scroll write made while it is still moving the transcript,
  // so rows above the reader wait for rest: no finger down, no scroll for a beat.
  it('lets the page in only once the transcript is at rest', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] });
    try {
      let landed = false;
      const onLoadOlder = vi.fn(async (whenSettled: () => Promise<void>) => {
        await whenSettled();
        landed = true;
        return true;
      });
      const { scroller } = renderTranscript({ onLoadOlder });
      fireEvent.touchStart(scroller, { touches: [{ clientY: 100 }] });
      await act(async () => fireEvent.touchMove(scroller, { touches: [{ clientY: 140 }] }));
      expect(onLoadOlder).toHaveBeenCalledTimes(1);
      await act(async () => { vi.advanceTimersByTime(500); });
      expect(landed).toBe(false);
      fireEvent.touchEnd(scroller, { touches: [] });
      // Momentum: scroll events keep coming after the finger lifts.
      for (let frame = 0; frame < 6; frame += 1) {
        await act(async () => {
          scroller.scrollTop -= 20;
          fireEvent.scroll(scroller);
          vi.advanceTimersByTime(50);
        });
      }
      expect(landed).toBe(false);
      // iOS momentum can go quiet for 300 ms before its last pixels: silence
      // alone is not rest while the platform has not said its scroll ended.
      await act(async () => { vi.advanceTimersByTime(400); });
      expect(landed).toBe(false);
      await act(async () => {
        fireEvent(scroller, new Event('scrollend'));
        vi.advanceTimersByTime(100);
      });
      expect(landed).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  // iOS sends ``scrollend`` for a write of ours too, while its momentum goes on:
  // that one does not say the platform's scrolling is over.
  it('does not take the scrollend of its own write for the end of momentum', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] });
    try {
      let landed = false;
      const onLoadOlder = vi.fn(async (whenSettled: () => Promise<void>) => {
        await whenSettled();
        landed = true;
        return true;
      });
      const { scroller, geometry } = renderTranscript({ onLoadOlder });
      fireEvent.touchStart(scroller, { touches: [{ clientY: 100 }] });
      await act(async () => fireEvent.touchMove(scroller, { touches: [{ clientY: 140 }] }));
      fireEvent.touchEnd(scroller, { touches: [] });
      for (let frame = 0; frame < 3; frame += 1) {
        await act(async () => {
          scroller.scrollTop -= 20;
          fireEvent.scroll(scroller);
          vi.advanceTimersByTime(30);
        });
      }
      // An image above the reader lands mid-momentum: compensated, and that write's
      // own scroll and scrollend arrive.
      geometry.extraAbove = 180;
      act(TestResizeObserver.resize);
      fireEvent.scroll(scroller);
      await act(async () => {
        vi.advanceTimersByTime(15);
        fireEvent(scroller, new Event('scrollend'));
      });
      // Momentum goes quiet before its last pixel: not rest yet.
      await act(async () => { vi.advanceTimersByTime(400); });
      expect(landed).toBe(false);
      await act(async () => {
        scroller.scrollTop -= 1;
        fireEvent.scroll(scroller);
        vi.advanceTimersByTime(15);
        fireEvent(scroller, new Event('scrollend'));
        vi.advanceTimersByTime(200);
      });
      expect(landed).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  // iOS finishing a scroll of its own (the last pixel of momentum) can put back
  // the position from before the write that held the reader's place.
  it('makes a write the platform put back once more, unless a finger is down', async () => {
    const first = pendingPage();
    const second = pendingPage();
    const onLoadOlder = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { scroller, scrollTo, rerender } = renderTranscript({ onLoadOlder });
    await act(async () => scrollTo(3000));
    await act(async () => scrollTo(2990));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    await act(async () => {
      rerender({ messages: messages(251, 100) });
      first.land(true);
    });
    const held = 2990 + 50 * 112;
    expect(scroller.scrollTop).toBe(held);
    // The write's own scroll event, then the platform putting the old position back.
    fireEvent.scroll(scroller);
    scroller.scrollTop = 2990;
    fireEvent.scroll(scroller);
    expect(scroller.scrollTop).toBe(held);
    // Once only: the same again is the reader's.
    scroller.scrollTop = 2990;
    fireEvent.scroll(scroller);
    expect(scroller.scrollTop).toBe(2990);

    // With a finger down, a move back is the reader's.
    await act(async () => scrollTo(2980));
    await act(async () => {
      rerender({ messages: messages(201, 150) });
      second.land(true);
    });
    const heldAgain = 2980 + 50 * 112;
    expect(scroller.scrollTop).toBe(heldAgain);
    fireEvent.touchStart(document.body, { touches: [{ identifier: 1, clientY: 300 }] });
    scroller.scrollTop = 2980;
    fireEvent.scroll(scroller);
    expect(scroller.scrollTop).toBe(2980);
    fireEvent.touchEnd(document.body, { touches: [] });
  });

  // The rubber band at the bottom takes a write back on iOS: content that grew
  // above a reader following the tail is pinned once the band has come to rest,
  // and the band settling is not the reader leaving the tail.
  it('pins a bottom that grew during the rubber band once the band comes to rest', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] });
    try {
      const { scroller, geometry, props } = renderTranscript({ hasOlder: false });
      const bottom = () => scroller.scrollHeight - scroller.clientHeight;
      scroller.scrollTop = bottom();
      fireEvent.scroll(scroller);
      expect(props.followingTailRef.current).toBe(true);
      // Pulled 3 px past the bottom, and settling back.
      geometry.overscroll = 3;
      scroller.scrollTop = bottom() + 3;
      fireEvent.scroll(scroller);
      const settledAt = bottom();
      // Content grows above the reader while the band is out.
      geometry.extraAbove = 380;
      act(TestResizeObserver.resize);
      expect(scroller.scrollTop).toBe(settledAt + 3);
      // The band returns to where it was heading: 380 px above the new bottom.
      scroller.scrollTop = settledAt;
      fireEvent.scroll(scroller);
      expect(props.followingTailRef.current).toBe(true);
      geometry.overscroll = 0;
      await act(async () => {
        fireEvent(scroller, new Event('scrollend'));
        vi.advanceTimersByTime(300);
      });
      expect(scroller.scrollTop).toBe(bottom());
      expect(props.followingTailRef.current).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  // Following the tail, opening something (a disclosure control) leaves it and
  // holds the row that was tapped; a plain tap keeps following.
  it('leaves the tail for a tap on a disclosure control, and only for that', async () => {
    const { scroller, props } = renderTranscript({ hasOlder: false });
    scroller.scrollTop = scroller.scrollHeight;
    fireEvent.scroll(scroller);
    expect(props.followingTailRef.current).toBe(true);
    const rows = scroller.querySelectorAll<HTMLElement>('[data-message-id]');
    const tapped = rows[rows.length - 2];

    fireEvent.click(tapped);
    expect(props.followingTailRef.current).toBe(true);

    const control = document.createElement('button');
    control.setAttribute('aria-expanded', 'false');
    tapped.append(control);
    fireEvent.click(control);
    expect(props.followingTailRef.current).toBe(false);
  });

  // The way back to the latest message glides, one write a frame toward a
  // bottom read again each frame; from a historical window it first installs
  // the latest one, and glides only once those rows are in.
  it('glides to the latest window once it is installed, and follows the tail at the bottom', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] });
    try {
      const reload = pendingPage();
      const onReloadLatest = vi.fn().mockReturnValue(reload.promise);
      const { scroller, rerender, props, getByRole, scrollTo } = renderTranscript({ hasOlder: false, needsLatestReload: true, onReloadLatest });
      await act(async () => scrollTo(1000));
      await act(async () => { vi.advanceTimersByTime(1600); });
      fireEvent.click(getByRole('button', { name: 'chat.scrollToBottom' }));
      expect(onReloadLatest).toHaveBeenCalledTimes(1);
      // Nothing moves before the latest window is in.
      act(flushFrames);
      expect(scroller.scrollTop).toBe(1000);
      await act(async () => {
        rerender({ needsLatestReload: false, messages: messages(1001, 50) });
        reload.land(true);
      });
      const bottom = () => scroller.scrollHeight - scroller.clientHeight;
      const tops: number[] = [];
      for (let frame = 0; frame < 30 && scroller.scrollTop < bottom(); frame += 1) {
        await act(async () => {
          vi.advanceTimersByTime(16);
          flushFrames();
        });
        tops.push(scroller.scrollTop);
      }
      expect(scroller.scrollTop).toBe(bottom());
      // Several frames on the way, not one cut.
      expect(tops.filter((top) => top > 1000 && top < bottom()).length).toBeGreaterThanOrEqual(5);
      expect(props.followingTailRef.current).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  // At the retained-window cap, a row arriving while the glide runs (following
  // is off) detaches the live tail: the glide lands on the latest window, not on
  // the bottom of the detached one.
  it('installs the latest window when the tail detached during the glide, and lands on it', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] });
    try {
      const reload = pendingPage();
      const onReloadLatest = vi.fn().mockReturnValue(reload.promise);
      const { scroller, rerender, props, getByRole, scrollTo } = renderTranscript({ hasOlder: false, onReloadLatest });
      await act(async () => scrollTo(1000));
      await act(async () => { vi.advanceTimersByTime(1600); });
      fireEvent.click(getByRole('button', { name: 'chat.scrollToBottom' }));
      await act(async () => {
        vi.advanceTimersByTime(16);
        flushFrames();
      });
      // A row came in: ChatPage detached the tail.
      await act(async () => rerender({ needsLatestReload: true }));
      for (let frame = 0; frame < 30; frame += 1) {
        await act(async () => {
          vi.advanceTimersByTime(16);
          flushFrames();
        });
      }
      expect(onReloadLatest).toHaveBeenCalledTimes(1);
      expect(props.followingTailRef.current).toBe(false);
      await act(async () => {
        rerender({ needsLatestReload: false, messages: messages(1001, 50) });
        reload.land(true);
      });
      for (let frame = 0; frame < 40; frame += 1) {
        await act(async () => {
          vi.advanceTimersByTime(16);
          flushFrames();
        });
      }
      expect(scroller.scrollTop).toBe(scroller.scrollHeight - scroller.clientHeight);
      expect(props.followingTailRef.current).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  // Whatever else moves the transcript (here a jump to a search result) takes
  // over from the glide, which must not write over it.
  it('stops the glide to the latest message when a jump takes over', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] });
    const scrollIntoView = Element.prototype.scrollIntoView;
    try {
      const { scroller, rerender, getByRole, scrollTo } = renderTranscript({ hasOlder: false });
      await act(async () => scrollTo(1000));
      await act(async () => { vi.advanceTimersByTime(1600); });
      fireEvent.click(getByRole('button', { name: 'chat.scrollToBottom' }));
      for (let frame = 0; frame < 3; frame += 1) {
        await act(async () => {
          vi.advanceTimersByTime(16);
          flushFrames();
        });
      }
      expect(scroller.scrollTop).toBeGreaterThan(1000);
      // The jump puts its row at 2000.
      Element.prototype.scrollIntoView = function scrollIntoViewStub() { scroller.scrollTop = 2000; };
      await act(async () => rerender({ jumpTarget: 'msg-320', highlightedId: 'msg-320' }));
      for (let frame = 0; frame < 30; frame += 1) {
        await act(async () => {
          vi.advanceTimersByTime(16);
          flushFrames();
        });
      }
      expect(scroller.scrollTop).toBe(2000);
    } finally {
      Element.prototype.scrollIntoView = scrollIntoView;
      vi.useRealTimers();
    }
  });

  // A second finger elsewhere (the composer) can outlast the one on the
  // transcript and end where the transcript never hears of it.
  it('counts a touch that ends outside the transcript as ended', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] });
    try {
      let landed = false;
      const onLoadOlder = vi.fn(async (whenSettled: () => Promise<void>) => {
        await whenSettled();
        landed = true;
        return true;
      });
      const { scroller } = renderTranscript({ onLoadOlder });
      const outside = document.createElement('div');
      document.body.append(outside);
      const own = { identifier: 1, clientY: 100 };
      const other = { identifier: 2, clientY: 600 };
      fireEvent.touchStart(scroller, { touches: [own] });
      await act(async () => fireEvent.touchMove(scroller, { touches: [{ ...own, clientY: 140 }] }));
      expect(onLoadOlder).toHaveBeenCalledTimes(1);
      fireEvent.touchStart(outside, { touches: [own, other] });
      fireEvent.touchEnd(scroller, { touches: [other] });
      await act(async () => { vi.advanceTimersByTime(500); });
      expect(landed).toBe(false);
      fireEvent.touchEnd(outside, { touches: [] });
      await act(async () => { vi.advanceTimersByTime(200); });
      expect(landed).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('consumes input begun while a page is pending until the next gesture', async () => {
    const first = pendingPage();
    const onLoadOlder = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(true);
    const { scroller, scrollTo } = renderTranscript({ onLoadOlder });
    scrollTo(0);
    fireEvent.touchStart(scroller, { touches: [{ clientY: 100 }] });
    fireEvent.touchMove(scroller, { touches: [{ clientY: 140 }] });
    await act(async () => first.land(true));
    await act(async () => fireEvent.touchMove(scroller, { touches: [{ clientY: 180 }] }));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    fireEvent.touchEnd(scroller);
    await act(async () => upwardInputs.touch(scroller));
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  it('is keyboard focusable and requires a fresh key press after an empty page', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { scroller } = renderTranscript({ messages: messages(1, 1), onLoadOlder });
    expect(scroller.tabIndex).toBe(0);
    scroller.focus();
    expect(document.activeElement).toBe(scroller);
    await act(async () => upwardInputs.keyboard(scroller));
    await act(async () => fireEvent.keyDown(scroller, { key: 'PageUp', repeat: true }));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    await act(async () => upwardInputs.keyboard(scroller));
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  it.each(Object.entries(upwardInputs))('lets a nested scroller own %s input until it can chain upward', async (_name, upward) => {
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { scroller } = renderTranscript({ messages: messages(1, 1), onLoadOlder });
    const panel = document.createElement('div');
    panel.style.overflowY = 'auto';
    Object.defineProperties(panel, { clientHeight: { value: 100 }, scrollHeight: { value: 500 } });
    panel.scrollTop = 120;
    const target = document.createElement('span');
    panel.append(target);
    scroller.append(panel);
    await act(async () => upward(target));
    expect(onLoadOlder).not.toHaveBeenCalled();
    panel.scrollTop = 0;
    panel.style.overscrollBehaviorY = 'contain';
    await act(async () => upward(target));
    expect(onLoadOlder).not.toHaveBeenCalled();
    panel.style.overscrollBehaviorY = 'auto';
    await act(async () => upward(target));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
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
    const now = vi.spyOn(performance, 'now').mockReturnValue(0);
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { scroller, scrollTo, rerender } = renderTranscript({ onLoadOlder, jumpTarget: 'missing-target' });
    scrollTo(0);
    fireEvent.wheel(scroller, { deltaY: -100 });
    expect(onLoadOlder).not.toHaveBeenCalled();
    act(flushFrames);
    rerender({ jumpTarget: null });
    expect(onLoadOlder).not.toHaveBeenCalled();
    now.mockReturnValue(300);
    await act(async () => fireEvent.wheel(scroller, { deltaY: -100 }));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });

  it('surfaces failures and offers one explicit retry without an automatic retry loop', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(false);
    // A window taller than the prefetch band (eight screens), so a reader can leave it.
    const { getByText, scroller, scrollTo } = renderTranscript({ messages: messages(201, 100), onLoadOlder });
    await act(async () => scrollTo(0));
    expect(getByText('chat.olderLoadFailed')).toBeTruthy();
    act(TestResizeObserver.resize);
    fireEvent.wheel(scroller, { deltaY: -100 });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    await act(async () => fireEvent.click(getByText('chat.olderLoadFailed')));
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
    // Leaving the prefetch band and coming back is a fresh ask.
    scrollTo(10000);
    await act(async () => scrollTo(0));
    expect(onLoadOlder).toHaveBeenCalledTimes(3);
  });

  it('does not hold a failure against a reader who left before it arrived', async () => {
    const first = pendingPage();
    const onLoadOlder = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(true);
    const { scrollTo, queryByText } = renderTranscript({ messages: messages(201, 100), onLoadOlder });
    scrollTo(0);
    scrollTo(10000);
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
