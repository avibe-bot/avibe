/** @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { createRef, type ComponentProps } from 'react';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { count?: number }) => (options?.count === undefined ? key : `${key}:${options.count}`),
  }),
}));
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
vi.mock('./Composer', () => ({ Composer: () => null }));

import { Transcript } from './ChatPage';
import type { WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import type { ActivityGroup } from '../../lib/agentActivity';

type Props = ComponentProps<typeof Transcript>;

let clock = 0;
const message = (id: string, over: Partial<WorkbenchMessage>): WorkbenchMessage =>
  ({
    id,
    session_id: 'session-1',
    platform: 'avibe',
    content: {},
    metadata: {},
    created_at: new Date(Date.UTC(2026, 9, 30, 4, 0, clock++)).toISOString(),
    ...over,
  }) as WorkbenchMessage;
const watch = (id: string) =>
  message(id, { author: 'harness', source: 'harness', type: 'harness', author_name: 'watch', text: `Watch ${id} finished` });
const userMessage = (id: string) => message(id, { author: 'user', source: 'user', type: 'user', text: `User ${id}` });
const reply = (id: string) => message(id, { author: 'agent', source: 'agent', type: 'result', text: `Reply ${id}` });

const chip = (id: string, anchorMessageId: string, anchorPosition: 'before' | 'after'): ActivityGroup => ({
  id,
  anchorMessageId,
  anchorPosition,
  open: false,
  status: 'done',
  steps: 3,
  durationMs: 1000,
});

const activity = (groups: ActivityGroup[]): NonNullable<Props['activity']> => {
  const before = new Map<string, ActivityGroup[]>();
  const after = new Map<string, ActivityGroup[]>();
  for (const group of groups) (group.anchorPosition === 'before' ? before : after).set(group.anchorMessageId!, [group]);
  return {
    enabled: true,
    beforeAnchor: before,
    afterAnchor: after,
    topGroups: [],
    liveRows: [],
    liveStartedAt: null,
    cardExpanded: false,
    onToggleCard: vi.fn(),
    expanded: {},
    loading: {},
    error: {},
    onToggleGroup: vi.fn(),
    onRetryGroup: vi.fn(),
    showToolCalls: false,
    onToggleTools: vi.fn(),
  };
};

const renderTranscript = (over: Partial<Props>) => {
  const props: Props = {
    messages: [],
    session: { id: 'session-1', title: 'Session', agent_name: null, archived_at: null } as WorkbenchSession,
    agentDisplayName: null,
    working: false,
    hasOlder: false,
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
  const rerender = (next: Partial<Props>) => {
    Object.assign(props, next);
    view.rerender(<MemoryRouter><Transcript {...props} /></MemoryRouter>);
  };
  return { ...view, rerender };
};

const groupToggle = (count: number) => screen.getByRole('button', { name: `chat.harnessGroup.count:${count}` });
const rendered = (id: string) => document.querySelector(`[data-message-id="${id}"]`);

describe('transcript Harness groups', () => {
  beforeEach(() => {
    clock = 0;
    vi.stubGlobal('CSS', { escape: (value: string) => value });
    vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
    vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} unobserve() {} });
    vi.stubGlobal('requestAnimationFrame', () => 0);
    vi.stubGlobal('cancelAnimationFrame', () => {});
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  // The motion and the scroller know the transcript's items by these marks (lib/transcriptMotion,
  // lib/transcriptScrollAnchor); the chrome above the history, which older rows go in under, has none.
  it('marks every item under the history for the motion and the scroller, and nothing above it', () => {
    const members = [watch('h1'), watch('h2'), watch('h3')];
    const above = { ...chip('above', 'none', 'after'), anchorMessageId: null };
    renderTranscript({
      messages: [userMessage('u1'), ...members, reply('r1')],
      activity: { ...activity([chip('answering', 'r1', 'before')]), topGroups: [above] },
    });
    fireEvent.click(groupToggle(3));
    const marked = [...document.querySelectorAll('[data-transcript-key]')]
      .map((el) => `${el.getAttribute('data-transcript-key')}${el.hasAttribute('data-transcript-row') ? ' row' : ''}`);
    expect(marked).toEqual([
      'harness-member:h1 row', 'harness-member:h2 row', 'harness-member:h3 row',
      expect.stringMatching(/^harness-collapse:\S+ row$/),
      'activity:answering row',
    ]);
    // The chip with no message is drawn, above the history, as neither.
    const column = document.querySelector('[data-testid="chat-transcript"]')!.firstElementChild!.firstElementChild!;
    const first = column.firstElementChild!;
    expect(first.querySelector('button')).toBeTruthy();
    expect(first.hasAttribute('data-transcript-row') || first.hasAttribute('data-transcript-key')).toBe(false);
    expect(document.querySelector('[data-transcript-key="activity:answering"]')!.getAttribute('data-transcript-for')).toBe('r1');
  });

  it('folds a run with its silent Turns and opens it in place', () => {
    const brief = watch('brief');
    const members = [watch('h1'), watch('h2'), watch('h3')];
    const answer = reply('r1');
    renderTranscript({
      messages: [brief, ...members, answer],
      activity: activity([chip('silent-1', 'h1', 'after'), chip('answering', 'r1', 'before')]),
    });

    // The Session's opening brief stays its own row; the three after it fold.
    expect(rendered('brief')).toBeTruthy();
    const toggle = groupToggle(3);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.textContent).toContain('Watch h3 finished');
    for (const { id } of members) expect(rendered(id)).toBeNull();
    // A silent Turn's chip folds with its trigger; the reply's own chip does not.
    expect(screen.getAllByText(/chat\.agentActivity\.label/)).toHaveLength(1);

    fireEvent.click(toggle);
    expect(groupToggle(3).getAttribute('aria-expanded')).toBe('true');
    for (const { id } of members) expect(rendered(id)).toBeTruthy();
    expect(screen.getAllByText(/chat\.agentActivity\.label/)).toHaveLength(2);

    fireEvent.click(screen.getByRole('button', { name: /chat\.harnessGroup\.collapseCount:3/ }));
    for (const { id } of members) expect(rendered(id)).toBeNull();
  });

  it('grows in place as Harness messages arrive and as older ones load', () => {
    const view = renderTranscript({ messages: [userMessage('u1'), watch('h3'), watch('h4')], hasOlder: true });
    const row = groupToggle(2).closest('[data-transcript-row]');

    view.rerender({ messages: [userMessage('u1'), watch('h3'), watch('h4'), watch('h5')] });
    expect(groupToggle(3).getAttribute('aria-expanded')).toBe('false');
    expect(groupToggle(3).textContent).toContain('Watch h5 finished');
    // The same element, so a scroll anchor held on it survives the growth.
    expect(groupToggle(3).closest('[data-transcript-row]')).toBe(row);

    fireEvent.click(groupToggle(3));
    view.rerender({ messages: [watch('h1'), watch('h2'), watch('h3'), watch('h4'), watch('h5'), watch('h6')] });
    expect(groupToggle(6).getAttribute('aria-expanded')).toBe('true');
    expect(groupToggle(6).closest('[data-transcript-row]')).toBe(row);
    expect(rendered('h1')).toBeTruthy();
    expect(rendered('h6')).toBeTruthy();
  });

  it('opens the group a jump lands in and keeps it open afterwards', () => {
    const messages = [userMessage('u1'), watch('h1'), watch('h2'), watch('h3'), reply('r1')];
    const view = renderTranscript({ messages });
    expect(rendered('h2')).toBeNull();

    act(() => view.rerender({ jumpTarget: 'h2', highlightedId: 'h2' }));
    expect(rendered('h2')).toBeTruthy();

    act(() => view.rerender({ jumpTarget: null, highlightedId: null }));
    expect(rendered('h2')).toBeTruthy();
    expect(groupToggle(3).getAttribute('aria-expanded')).toBe('true');
  });

  it('keeps the row a reader opened in view when the next Harness message folds it', () => {
    const view = renderTranscript({ messages: [userMessage('u1'), watch('h1')], hasOlder: true });
    const chipOf = (id: string) => rendered(id)!.querySelector<HTMLButtonElement>('button[aria-expanded]')!;
    fireEvent.click(chipOf('h1'));
    expect(chipOf('h1').getAttribute('aria-expanded')).toBe('true');

    view.rerender({ messages: [userMessage('u1'), watch('h1'), watch('h2')] });
    // The group opens in place: the row being read stays where it was, still open.
    expect(groupToggle(2).getAttribute('aria-expanded')).toBe('true');
    expect(chipOf('h1').getAttribute('aria-expanded')).toBe('true');
    expect(rendered('h2')).toBeTruthy();

    // Closing that row does not fold the group out from under the reader.
    fireEvent.click(chipOf('h1'));
    expect(groupToggle(2).getAttribute('aria-expanded')).toBe('true');

    // Closing the group closes its rows, so reopening it starts them closed.
    fireEvent.click(chipOf('h1'));
    fireEvent.click(groupToggle(2));
    expect(rendered('h1')).toBeNull();
    fireEvent.click(groupToggle(2));
    expect(chipOf('h1').getAttribute('aria-expanded')).toBe('false');
  });

  it('brings a group closed from below its head back to the top of the view', () => {
    const members = Array.from({ length: 12 }, (_, index) => watch(`h${index + 1}`));
    const view = renderTranscript({ messages: [userMessage('u1'), ...members, reply('r1')] });
    fireEvent.click(groupToggle(12));

    // jsdom has no layout: every direct child of the transcript column is 100px
    // tall, stacked in DOM order, and the viewport is 800px tall at the top.
    const scroller = view.getByTestId('chat-transcript');
    const column = scroller.firstElementChild!.firstElementChild!;
    let scrollTop = 0;
    Object.defineProperties(scroller, {
      clientHeight: { configurable: true, get: () => 800 },
      scrollHeight: { configurable: true, get: () => column.children.length * 100 },
      scrollTop: { configurable: true, get: () => scrollTop, set: (value: number) => { scrollTop = Math.max(0, value); } },
    });
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this === scroller) return { top: 0, bottom: 800, height: 800 } as DOMRect;
      const index = Array.prototype.indexOf.call(column.children, this);
      const top = index < 0 ? 0 : index * 100 - scrollTop;
      return { top, bottom: top + 90, height: 90 } as DOMRect;
    });

    // Read down into the open group: its head (row 1) is far above the viewport.
    fireEvent.pointerDown(scroller);
    scroller.scrollTop = 500;
    fireEvent.scroll(scroller);
    fireEvent.click(screen.getByRole('button', { name: /chat\.harnessGroup\.collapseCount:12/ }));

    expect(groupToggle(12).getAttribute('aria-expanded')).toBe('false');
    // The collapsed group now sits at the top of the viewport, not off-screen above it.
    expect(scrollTop).toBe(100);
  });

  // jsdom has no layout: every direct child of the transcript column is 100px
  // tall, stacked in DOM order, and a nested element sits at its top-level
  // ancestor's place. The viewport is 800px tall.
  const layOut = (scroller: HTMLElement) => {
    const column = scroller.firstElementChild!.firstElementChild!;
    let scrollTop = 0;
    Object.defineProperties(scroller, {
      clientHeight: { configurable: true, get: () => 800 },
      scrollHeight: { configurable: true, get: () => column.children.length * 100 },
      scrollTop: { configurable: true, get: () => scrollTop, set: (value: number) => { scrollTop = Math.max(0, value); } },
    });
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this === scroller) return { top: 0, bottom: 800, height: 800 } as DOMRect;
      const ancestor = [...column.children].find((child) => child === this || child.contains(this));
      const index = ancestor ? Array.prototype.indexOf.call(column.children, ancestor) : -1;
      const top = index < 0 ? 0 : index * 100 - scrollTop;
      return { top, bottom: top + 90, height: 90 } as DOMRect;
    });
    return { top: () => scrollTop };
  };
  const tail = Array.from({ length: 10 }, (_, index) => reply(`r${index}`));

  it.each([
    ['opened, so it re-mounts inside the opened group', true, 400],
    ['closed, so it folds into the collapsed head', false, 100],
  ])('holds the reading position when an older page makes the anchored row a group member (%s)', (_label, opened, expected) => {
    const view = renderTranscript({ messages: [watch('h5'), ...tail], hasOlder: true });
    const scroller = view.getByTestId('chat-transcript');
    const layout = layOut(scroller);
    if (opened) fireEvent.click(rendered('h5')!.querySelector('button[aria-expanded]')!);
    // Read at the top of the window: h5 is the anchored row, at the top of the view.
    fireEvent.pointerDown(scroller);
    scroller.scrollTop = 0;
    fireEvent.scroll(scroller);

    view.rerender({ messages: [userMessage('u1'), watch('h3'), watch('h4'), watch('h5'), ...tail] });

    // u1 is row 0 and the group head row 1. Opened, h5 renders as row 4; closed,
    // the head stands in for it. Either way it is back at the top of the view.
    expect(groupToggle(3).getAttribute('aria-expanded')).toBe(opened ? 'true' : 'false');
    expect(layout.top()).toBe(expected);
  });
});

