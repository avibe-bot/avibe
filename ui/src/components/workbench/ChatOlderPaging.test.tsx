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

import { act, cleanup, render } from '@testing-library/react';
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

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
    FakeIntersectionObserver.live.push(this);
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

const renderTranscript = (over: {
  hasOlder?: boolean;
  loadingOlder?: boolean;
  onLoadOlder?: () => void | Promise<boolean>;
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
  props.followingTailRef.current = false;

  const view = render(
    <MemoryRouter>
      <Transcript {...props} />
    </MemoryRouter>,
  );
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
