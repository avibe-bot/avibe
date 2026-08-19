/** @vitest-environment jsdom */

// The transcript's older-page loader must be LEVEL-triggered: for as long as the
// reader is inside the trigger band at the top of the loaded window and older
// history remains, pages keep arriving. The invariant is deliberately stated
// without reference to scroll events, because the reader most likely to want
// another page — already parked at the very top — cannot produce one: an upward
// gesture at scrollTop 0 moves nothing, so no scroll event is emitted at all.
//
// The IntersectionObserver stub below models the one browser guarantee the fix
// rests on: ``observe()`` reports the CURRENT intersection state, rather than
// waiting for the next crossing. So each test declares where the reader is as a
// standing fact and lets the component ask as often as it likes.

import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { createRef } from 'react';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('../../context/ApiContext', () => ({
  useApi: () => ({}),
}));

vi.mock('../../context/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({
    capabilities: {
      can_chat: true,
      can_manage_instance: false,
      can_use_show_pages: false,
      can_use_vault_secrets: false,
    },
  }),
}));

vi.mock('../../context/WindowManagerContext', () => ({
  useWindowManager: () => ({ focusedId: null, focusCanvas: vi.fn() }),
}));

vi.mock('../../lib/useIsDesktop', () => ({
  isDesktopViewport: () => false,
  useIsDesktop: () => false,
}));

// Importing ChatPage pulls in the composer's lexical stack, which touches
// browser APIs jsdom lacks at module scope. The transcript never renders it.
vi.mock('./Composer', () => ({
  Composer: () => null,
}));

import { Transcript } from './ChatPage';
import type { WorkbenchSession } from '../../context/ApiContext';

// One live IntersectionObserver stub shared by the render under test. ``atTop``
// is the world: where the reader is sitting. Every observe() answers with it,
// which is what a real observer does for a freshly observed target.
class FakeIntersectionObserver {
  static live: FakeIntersectionObserver[] = [];
  static atTop = false;

  private callback: IntersectionObserverCallback;
  private observed = new Set<Element>();
  private readonly root: Element | null;

  constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
    this.callback = callback;
    this.root = (options?.root as Element | null) ?? null;
    FakeIntersectionObserver.live.push(this);
  }

  // The element the transcript scrolls. The observer is rooted at it, so a test
  // reaches it through the observer rather than by matching a class name.
  static scroller(): HTMLElement {
    const root = FakeIntersectionObserver.live[0]?.root;
    if (!(root instanceof HTMLElement)) {
      throw new Error('the older-page observer was not rooted at the scroll container');
    }
    return root;
  }

  observe(target: Element) {
    this.observed.add(target);
    this.deliver(target, FakeIntersectionObserver.atTop);
  }

  unobserve(target: Element) {
    this.observed.delete(target);
  }

  disconnect() {
    this.observed.clear();
    FakeIntersectionObserver.live = FakeIntersectionObserver.live.filter((io) => io !== this);
  }

  // Simulate the reader's position changing under a live observer.
  static move(atTop: boolean) {
    FakeIntersectionObserver.atTop = atTop;
    for (const io of [...FakeIntersectionObserver.live]) {
      for (const target of io.observed) io.deliver(target, atTop);
    }
  }

  private deliver(target: Element, isIntersecting: boolean) {
    this.callback([{ target, isIntersecting } as IntersectionObserverEntry], this as never);
  }
}

const session = (): WorkbenchSession =>
  ({
    id: 'session-1',
    title: 'Session',
    agent_name: null,
    archived_at: null,
  }) as unknown as WorkbenchSession;

// A transcript taller than its viewport, scrolled up to the top of the loaded
// window: the reader is in history, which is what paging exists for.
const READING_HISTORY = { scrollTop: 0, scrollHeight: 10_000, clientHeight: 800 };
// A transcript SHORT enough to fit its viewport. There is nothing to scroll, so
// the reader is still following the live tail even though the top of the loaded
// window — and the sentinel with it — is permanently in view.
const FITS_VIEWPORT = { scrollTop: 0, scrollHeight: 800, clientHeight: 800 };

// jsdom has no layout, so where the reader sits is stated as geometry and
// announced with the scroll event a browser would emit. This is the production
// path: the transcript derives "following the live tail" from exactly these
// three numbers, and nothing else tells it where the reader is.
const placeReader = (
  el: HTMLElement,
  geometry: { scrollTop: number; scrollHeight: number; clientHeight: number },
) => {
  for (const [prop, value] of Object.entries(geometry)) {
    Object.defineProperty(el, prop, { value, writable: true, configurable: true });
  }
  fireEvent.scroll(el);
};

const renderTranscript = (over: {
  hasOlder?: boolean;
  loadingOlder?: boolean;
  onLoadOlder?: () => void | Promise<boolean>;
  // Whether the reader is following the live tail. Defaults to "scrolled up in
  // history", the state every paging assertion below is about.
  following?: boolean;
}) => {
  const props = {
    messages: [],
    session: session(),
    agentDisplayName: null,
    // No rows, but ``working`` keeps the transcript out of its empty state, so the
    // scroll container (and the sentinel inside it) mounts. The trigger does not
    // depend on row content.
    working: true,
    hasOlder: over.hasOlder ?? true,
    loadingOlder: over.loadingOlder ?? false,
    onLoadOlder: over.onLoadOlder ?? vi.fn(),
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
  };
  const view = render(
    <MemoryRouter>
      <Transcript {...props} />
    </MemoryRouter>,
  );
  // The mount effect jumps to the bottom inside a frame callback and pins the
  // transcript there; frames run synchronously here, so that jump has already
  // landed and this placement is what the component ends up believing.
  placeReader(FakeIntersectionObserver.scroller(), over.following ? FITS_VIEWPORT : READING_HISTORY);

  const rerender = (next: Partial<typeof props>) =>
    view.rerender(
      <MemoryRouter>
        <Transcript {...props} {...next} />
      </MemoryRouter>,
    );
  return { ...view, rerender, props };
};

describe('transcript older-page loading', () => {
  beforeEach(() => {
    FakeIntersectionObserver.live = [];
    FakeIntersectionObserver.atTop = false;
    vi.stubGlobal('IntersectionObserver', FakeIntersectionObserver);
    // Run frame callbacks inline so the mount jump-to-bottom is complete by the
    // time ``renderTranscript`` returns, instead of landing mid-test and
    // re-pinning the transcript behind the assertions' back.
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      cb(0);
      return 0;
    });
    vi.stubGlobal('cancelAnimationFrame', () => {});
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('keeps loading pages while the reader stays at the top, with no scroll event', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { rerender } = renderTranscript({ onLoadOlder });

    // The reader scrolls up into the trigger band.
    await act(async () => FakeIntersectionObserver.move(true));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    // The page is in flight: nothing else may start.
    await act(async () => rerender({ loadingOlder: true }));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    // The page settled and the reader never moved — still at the top of the
    // loaded window, still more history behind it. This is the case the old
    // scroll-armed latch deadlocked on, and the whole point of the fix.
    await act(async () => rerender({ loadingOlder: false }));
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  it('stops once the reader leaves the band or history runs out', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { rerender } = renderTranscript({ onLoadOlder });

    await act(async () => FakeIntersectionObserver.move(true));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    // The prepended page pushes the sentinel out of the band (the anchor restore
    // does this for real) — the reader is no longer asking for anything.
    await act(async () => FakeIntersectionObserver.move(false));
    await act(async () => rerender({ loadingOlder: false }));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    // Back at the top, but the server says that was the last page.
    await act(async () => rerender({ hasOlder: false }));
    await act(async () => FakeIntersectionObserver.move(true));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });

  it('does not page while the reader is following the live tail', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(true);
    const { rerender } = renderTranscript({ onLoadOlder, following: true });

    // A transcript short enough to fit its viewport leaves the sentinel in the
    // band with the reader still parked on the latest message. Sentinel in view
    // is only a REQUEST for older history if the reader went looking for it —
    // otherwise opening or resizing such a chat would walk backwards through
    // history nobody asked to see.
    await act(async () => FakeIntersectionObserver.move(true));
    await act(async () => rerender({ loadingOlder: true }));
    await act(async () => rerender({ loadingOlder: false }));
    expect(onLoadOlder).not.toHaveBeenCalled();
  });

  it('offers an explicit retry instead of re-asking after a failed page', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(false);
    const { rerender, getByText } = renderTranscript({ onLoadOlder });

    await act(async () => FakeIntersectionObserver.move(true));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    // The failed page runs the same in-flight → settled cycle a successful one
    // does, so it reaches the post-load re-evaluation. But a failure adds no
    // content: the reader is still in the band, and re-asking would spin against
    // a server that is still failing.
    await act(async () => rerender({ loadingOlder: true }));
    await act(async () => rerender({ loadingOlder: false }));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    expect(getByText('chat.olderLoadFailed')).toBeTruthy();

    // Clicking the retry line is the way forward for a reader who stays put.
    await act(async () => getByText('chat.olderLoadFailed').click());
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  it('retries once when the reader leaves the band and comes back', async () => {
    const onLoadOlder = vi.fn().mockResolvedValue(false);
    const { rerender } = renderTranscript({ onLoadOlder });

    await act(async () => FakeIntersectionObserver.move(true));
    await act(async () => rerender({ loadingOlder: true }));
    await act(async () => rerender({ loadingOlder: false }));
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    // Moving away and back is a fresh ask, not the same doomed one repeated —
    // otherwise one failure would leave the retry line as the only way to page
    // for the rest of the session.
    await act(async () => FakeIntersectionObserver.move(false));
    await act(async () => FakeIntersectionObserver.move(true));
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });
});
