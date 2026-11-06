/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import type { TFunction } from 'i18next';

import type { InboxSession } from '../../context/ApiContext';
import { InboxCard } from './InboxCard';

// A finger swipes an unread Inbox card sideways, either way, to mark it read (`useCardSwipe`'s rule), as
// the card's handlers see a finger: it follows once the finger has gone 10 px more across than down; let
// go past 30% of its width, or flicked, it marks read through the card's own 「Mark read」 path; any other
// release springs it back. A touch that goes down first is the page's scroll, the left screen edge is the
// system's Back, a mouse never swipes, and a card that moved never opens. jsdom has no pointer input or
// layout, so the events carry their fields as plain properties and the card is given its width.

const t = ((key: string) => key) as unknown as TFunction;
const session = (unread: number): InboxSession => ({
  session_id: 'ses_a', title: 'Fix the cache', scope_id: null, project_id: 'proj', project_name: 'avibe',
  last_activity_at: '2026-11-06T05:00:00Z', last_message_author: 'agent', replied: false,
  preview_text: 'Opened PR #3141, waiting on review.', preview_at: null, unread_count: unread, unread: unread > 0,
});

const onOpen = vi.fn();
const onMarkRead = vi.fn();
const card = ({ unread = 2, canChat = true } = {}) => (
  <MemoryRouter>
    <InboxCard
      session={session(unread)}
      unread={unread}
      quiet={false}
      inBotSection={false}
      bot={undefined}
      botLabel=""
      canChat={canChat}
      minute={0}
      t={t}
      onOpen={onOpen}
      onMarkRead={onMarkRead}
    />
  </MemoryRouter>
);

const WIDTH = 300;
const row = () => document.querySelector<HTMLElement>('[data-flip-id="ses_a"]')!;
const slider = () => document.querySelector<HTMLElement>('[data-inbox-session-id="ses_a"]')!.parentElement!;
const action = () => document.querySelector<HTMLElement>('.swipe-action');
const preview = () => screen.getByText(/waiting on review/);

function mount(options?: { unread?: number; canChat?: boolean }) {
  const view = render(card(options));
  vi.spyOn(row(), 'getBoundingClientRect').mockReturnValue({ width: WIDTH } as DOMRect);
  return view;
}

/** A pointer event over the preview at (`x`, `y`), `at` ms into the gesture. */
function pointer(type: string, x: number, y: number, at: number, pointerType = 'touch') {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperties(event, {
    pointerId: { value: 1 },
    pointerType: { value: pointerType },
    isPrimary: { value: true },
    button: { value: 0 },
    clientX: { value: x },
    clientY: { value: y },
    timeStamp: { value: at },
  });
  act(() => {
    preview().dispatchEvent(event);
  });
}

/** A finger down at the first point, through the others 16 ms apart, then still for half a second
 *  (so it lets go slowly) unless `flick`, and lifted at the last. */
function swipe(points: [number, number][], { flick = false } = {}) {
  const [first, ...rest] = points;
  pointer('pointerdown', first[0], first[1], 0);
  rest.forEach(([x, y], index) => pointer('pointermove', x, y, 16 * (index + 1)));
  const last = points[points.length - 1];
  pointer('pointerup', last[0], last[1], 16 * rest.length + (flick ? 16 : 500));
}

/** Across from x = 150 by each step in turn, at a steady y. */
const across = (direction: 1 | -1, ...steps: number[]): [number, number][] => [[150, 100], ...steps.map((dx): [number, number] => [150 + direction * dx, 100])];

beforeEach(() => {
  onOpen.mockClear();
  onMarkRead.mockClear();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe.each([
  ['left', -1],
  ['right', 1],
] as const)('an unread card swiped %s', (_name, direction) => {
  it('follows the finger once it has gone 10 px across, and springs back when let go short of 30%', () => {
    mount();
    pointer('pointerdown', 150, 100, 0);
    pointer('pointermove', 150 + direction * 6, 101, 16);
    expect(slider().style.transform).toBe('');
    // Measured from where it started moving, so it does not jump by the first 10 px.
    pointer('pointermove', 150 + direction * 12, 101, 32);
    pointer('pointermove', 150 + direction * 72, 102, 48);
    expect(slider().style.transform).toBe(`translateX(${direction * 60}px)`);
    pointer('pointerup', 150 + direction * 72, 102, 548);
    expect(onMarkRead).not.toHaveBeenCalled();
    expect(slider().style.transform).toBe('');
  });

  it('marks read when let go past 30% of its width, and the action fills from that point', () => {
    mount();
    pointer('pointerdown', 150, 100, 0);
    pointer('pointermove', 150 + direction * 12, 100, 16);
    pointer('pointermove', 150 + direction * 90, 100, 32);
    expect(action()!.style.getPropertyValue('--swipe-armed')).not.toBe('1');
    pointer('pointermove', 150 + direction * 110, 100, 48);
    expect(action()!.style.getPropertyValue('--swipe-armed')).toBe('1');
    pointer('pointerup', 150 + direction * 110, 100, 548);
    expect(onMarkRead).toHaveBeenCalledTimes(1);
    expect(onMarkRead).toHaveBeenCalledWith('ses_a');
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('springs back when the finger returns short of 30% before letting go, and the action empties', () => {
    mount();
    pointer('pointerdown', 150, 100, 0);
    pointer('pointermove', 150 + direction * 12, 100, 16);
    pointer('pointermove', 150 + direction * 120, 100, 32);
    expect(action()!.style.getPropertyValue('--swipe-armed')).toBe('1');
    pointer('pointermove', 150 + direction * 50, 100, 48);
    expect(action()!.style.getPropertyValue('--swipe-armed')).toBe('0');
    pointer('pointerup', 150 + direction * 50, 100, 548);
    expect(onMarkRead).not.toHaveBeenCalled();
  });

  it('marks read on a flick well short of 30%, but not on a twitch or when the finger stopped', () => {
    mount();
    // Under the minimum distance, however fast.
    swipe(across(direction, 12, 30), { flick: true });
    expect(onMarkRead).not.toHaveBeenCalled();
    // Far enough, but held still before letting go.
    swipe(across(direction, 12, 60));
    expect(onMarkRead).not.toHaveBeenCalled();
    // Far enough (48 px, 16% of the card), still moving fast as it lets go.
    swipe(across(direction, 12, 60), { flick: true });
    expect(onMarkRead).toHaveBeenCalledTimes(1);
  });

  it('springs back when the browser takes the touch, however far and fast it went', () => {
    mount();
    pointer('pointerdown', 150, 100, 0);
    pointer('pointermove', 150 + direction * 12, 100, 16);
    pointer('pointermove', 150 + direction * 162, 100, 32);
    expect(slider().style.transform).toBe(`translateX(${direction * 150}px)`);
    pointer('pointercancel', 150 + direction * 162, 100, 48);
    expect(onMarkRead).not.toHaveBeenCalled();
    expect(slider().style.transform).toBe('');
  });
});

describe('a touch on an unread card', () => {
  it('that goes down first is the page scroll: the card stays put for the rest of that touch', () => {
    mount();
    swipe([[150, 100], [154, 112], [230, 116], [300, 120]]);
    expect(onMarkRead).not.toHaveBeenCalled();
    // Across first, the same card swipes.
    swipe([[150, 100], [162, 102], [300, 104]]);
    expect(onMarkRead).toHaveBeenCalledTimes(1);
  });

  it('that starts at the left edge of the screen is the system\'s Back', () => {
    mount();
    swipe([[12, 100], [24, 100], [200, 100]]);
    expect(onMarkRead).not.toHaveBeenCalled();
    swipe([[40, 100], [52, 100], [200, 100]]);
    expect(onMarkRead).toHaveBeenCalledTimes(1);
  });

  it('never opens the card it moved, even when it ends where it began; the next tap does', () => {
    mount();
    swipe(across(1, 12, 60, 0));
    fireEvent.click(preview(), { clientX: 150, clientY: 100, detail: 1 });
    expect(onOpen).not.toHaveBeenCalled();
    expect(onMarkRead).not.toHaveBeenCalled();
    pointer('pointerdown', 150, 100, 0);
    pointer('pointerup', 150, 100, 80);
    fireEvent.click(preview(), { clientX: 150, clientY: 100, detail: 1 });
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});

describe('what does not swipe', () => {
  it.each([
    ['a read card', { unread: 0 }],
    ['a card the reader cannot mark read', { canChat: false }],
  ])('%s does not move', (_name, options) => {
    const view = mount(options);
    swipe(across(1, 12, 150));
    expect(slider().style.transform).toBe('');
    expect(action()).toBeNull();
    expect(onMarkRead).not.toHaveBeenCalled();
    // The same card once it can be marked read.
    view.rerender(card());
    swipe(across(1, 12, 150));
    expect(onMarkRead).toHaveBeenCalledTimes(1);
  });

  it('a mouse or a pen drags as it did: the card does not move; a finger moves it', () => {
    mount();
    for (const pointerType of ['mouse', 'pen']) {
      pointer('pointerdown', 150, 100, 0, pointerType);
      pointer('pointermove', 162, 100, 16, pointerType);
      pointer('pointermove', 300, 100, 32, pointerType);
      expect(slider().style.transform).toBe('');
      pointer('pointerup', 300, 100, 532, pointerType);
    }
    expect(onMarkRead).not.toHaveBeenCalled();
    swipe(across(1, 12, 150));
    expect(onMarkRead).toHaveBeenCalledTimes(1);
  });
});
