/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import {
  MAX_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
  MIN_WIDTH_BESIDE_SIDEBAR,
  SIDEBAR_WIDTH_VAR,
  maxSidebarWidth,
} from '../lib/sidebarWidth';
import { DESKTOP_MIN_WIDTH } from '../lib/useIsDesktop';
import { SidebarResizer } from './SidebarResizer';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

// jsdom implements pointer events but not pointer capture, which the gesture
// really does use in a browser; stubbing just that primitive keeps the component
// free of a fallback path no shipped browser needs.
const capture = vi.fn();
const release = vi.fn();

beforeEach(() => {
  Element.prototype.setPointerCapture = capture;
  Element.prototype.releasePointerCapture = release;
});

afterEach(() => {
  // After cleanup: unmounting a live gesture is itself one of the restores, so
  // the body is reset once nothing can write to it again.
  cleanup();
  document.body.removeAttribute('style');
  vi.clearAllMocks();
  delete (Element.prototype as Partial<Element>).setPointerCapture;
  delete (Element.prototype as Partial<Element>).releasePointerCapture;
});

const separator = () => screen.getByRole('separator');
const publishedWidth = () => document.documentElement.style.getPropertyValue(SIDEBAR_WIDTH_VAR);
const reportedWidth = () => Number(separator().getAttribute('aria-valuenow'));

/** One gesture: press on the edge, travel through every offset, release. */
const drag = (...offsets: number[]) => {
  const handle = separator();
  const startX = reportedWidth();
  fireEvent.pointerDown(handle, { pointerId: 1, button: 0, clientX: startX });
  offsets.forEach((offset) => fireEvent.pointerMove(handle, { pointerId: 1, clientX: startX + offset }));
  fireEvent.pointerUp(handle, { pointerId: 1 });
};

describe('SidebarResizer width', () => {
  it('starts at the shipped width and leaves the stylesheet in charge', () => {
    render(<SidebarResizer />);

    expect(reportedWidth()).toBe(MIN_SIDEBAR_WIDTH);
    expect(publishedWidth()).toBe('');
  });

  it('publishes one width for the sidebar, the content offset and the overlay', () => {
    render(<SidebarResizer />);

    drag(60);

    expect(reportedWidth()).toBe(MIN_SIDEBAR_WIDTH + 60);
    expect(publishedWidth()).toBe(`${MIN_SIDEBAR_WIDTH + 60}px`);
  });

  it('holds the bounds without compounding them across repeated gestures', () => {
    render(<SidebarResizer />);

    // Travel far past the maximum, then back inside the range within the same
    // gesture: the overshoot is not remembered, so the width follows the pointer
    // again from where the bound clamped it.
    fireEvent.pointerDown(separator(), { pointerId: 1, button: 0, clientX: MIN_SIDEBAR_WIDTH });
    fireEvent.pointerMove(separator(), { pointerId: 1, clientX: MIN_SIDEBAR_WIDTH + 900 });
    expect(reportedWidth()).toBe(MAX_SIDEBAR_WIDTH);
    fireEvent.pointerMove(separator(), { pointerId: 1, clientX: MIN_SIDEBAR_WIDTH + 50 });
    expect(reportedWidth()).toBe(MIN_SIDEBAR_WIDTH + 50);
    fireEvent.pointerUp(separator(), { pointerId: 1 });

    // A second gesture that pushes out and a third that pushes in stop at the
    // same two numbers as the first.
    drag(900);
    expect(reportedWidth()).toBe(MAX_SIDEBAR_WIDTH);
    drag(900);
    expect(reportedWidth()).toBe(MAX_SIDEBAR_WIDTH);
    drag(-900);
    expect(reportedWidth()).toBe(MIN_SIDEBAR_WIDTH);
    drag(-900);
    expect(reportedWidth()).toBe(MIN_SIDEBAR_WIDTH);
    drag(900);
    expect(reportedWidth()).toBe(MAX_SIDEBAR_WIDTH);
  });

  it('takes the pointer capture that keeps a drag alive away from the edge', () => {
    render(<SidebarResizer />);

    fireEvent.pointerDown(separator(), { pointerId: 7, button: 0, clientX: MIN_SIDEBAR_WIDTH });

    expect(capture).toHaveBeenCalledWith(7);
  });

  it('ignores a secondary button and a stray move from another pointer', () => {
    render(<SidebarResizer />);

    fireEvent.pointerDown(separator(), { pointerId: 1, button: 2, clientX: MIN_SIDEBAR_WIDTH });
    fireEvent.pointerMove(separator(), { pointerId: 1, clientX: MIN_SIDEBAR_WIDTH + 80 });
    expect(reportedWidth()).toBe(MIN_SIDEBAR_WIDTH);

    fireEvent.pointerDown(separator(), { pointerId: 1, button: 0, clientX: MIN_SIDEBAR_WIDTH });
    fireEvent.pointerMove(separator(), { pointerId: 2, clientX: MIN_SIDEBAR_WIDTH + 80 });
    expect(reportedWidth()).toBe(MIN_SIDEBAR_WIDTH);
  });
});

describe('SidebarResizer gesture end', () => {
  // Non-empty on purpose: the drag borrows the body's cursor and selection, and
  // clearing them instead of giving them back would erase whatever else had set
  // them. Every ending is held to the same restore.
  const borrowedFrom = { cursor: 'progress', userSelect: 'text' };

  beforeEach(() => {
    document.body.style.cursor = borrowedFrom.cursor;
    document.body.style.userSelect = borrowedFrom.userSelect;
  });

  it.each(['pointerUp', 'pointerCancel', 'lostPointerCapture'] as const)(
    'gives the borrowed cursor and selection back on %s',
    (ending) => {
      render(<SidebarResizer />);

      fireEvent.pointerDown(separator(), { pointerId: 1, button: 0, clientX: MIN_SIDEBAR_WIDTH });
      expect(document.body.style.cursor).toBe('col-resize');
      expect(document.body.style.userSelect).toBe('none');
      expect(separator().getAttribute('data-dragging')).toBe('true');

      fireEvent[ending](separator(), { pointerId: 1 });

      expect(document.body.style.cursor).toBe(borrowedFrom.cursor);
      expect(document.body.style.userSelect).toBe(borrowedFrom.userSelect);
      expect(separator().getAttribute('data-dragging')).toBeNull();

      // The gesture is over, so a late move from the same pointer moves nothing.
      fireEvent.pointerMove(separator(), { pointerId: 1, clientX: MIN_SIDEBAR_WIDTH + 80 });
      expect(reportedWidth()).toBe(MIN_SIDEBAR_WIDTH);
    },
  );

  it('keeps a live drag when another pointer goes down, up or away', () => {
    render(<SidebarResizer />);

    fireEvent.pointerDown(separator(), { pointerId: 1, button: 0, clientX: MIN_SIDEBAR_WIDTH });
    // A second finger neither takes the edge over nor ends what has it.
    fireEvent.pointerDown(separator(), { pointerId: 2, button: 0, clientX: MIN_SIDEBAR_WIDTH + 200 });
    fireEvent.pointerUp(separator(), { pointerId: 2 });
    fireEvent.pointerCancel(separator(), { pointerId: 2 });

    expect(separator().getAttribute('data-dragging')).toBe('true');
    fireEvent.pointerMove(separator(), { pointerId: 1, clientX: MIN_SIDEBAR_WIDTH + 40 });
    expect(reportedWidth()).toBe(MIN_SIDEBAR_WIDTH + 40);

    fireEvent.pointerUp(separator(), { pointerId: 1 });
    expect(separator().getAttribute('data-dragging')).toBeNull();
    expect(document.body.style.cursor).toBe(borrowedFrom.cursor);
  });

  it('ends a live gesture at unmount and hands the width back to the stylesheet', () => {
    const view = render(<SidebarResizer />);

    fireEvent.pointerDown(separator(), { pointerId: 1, button: 0, clientX: MIN_SIDEBAR_WIDTH });
    fireEvent.pointerMove(separator(), { pointerId: 1, clientX: MIN_SIDEBAR_WIDTH + 120 });
    expect(publishedWidth()).toBe(`${MIN_SIDEBAR_WIDTH + 120}px`);

    // What leaving desktop layout, or a standalone app tab taking over, does.
    view.unmount();

    expect(document.body.style.cursor).toBe(borrowedFrom.cursor);
    expect(document.body.style.userSelect).toBe(borrowedFrom.userSelect);
    expect(publishedWidth()).toBe('');
  });
});

describe('SidebarResizer keyboard', () => {
  it('steps within the same bounds and reaches them with Home and End', () => {
    render(<SidebarResizer />);

    fireEvent.keyDown(separator(), { key: 'ArrowRight' });
    const step = reportedWidth() - MIN_SIDEBAR_WIDTH;
    expect(step).toBeGreaterThan(0);
    expect(publishedWidth()).toBe(`${MIN_SIDEBAR_WIDTH + step}px`);

    fireEvent.keyDown(separator(), { key: 'ArrowLeft' });
    expect(reportedWidth()).toBe(MIN_SIDEBAR_WIDTH);
    fireEvent.keyDown(separator(), { key: 'ArrowLeft' });
    expect(reportedWidth()).toBe(MIN_SIDEBAR_WIDTH);

    fireEvent.keyDown(separator(), { key: 'End' });
    expect(reportedWidth()).toBe(MAX_SIDEBAR_WIDTH);
    fireEvent.keyDown(separator(), { key: 'ArrowRight' });
    expect(reportedWidth()).toBe(MAX_SIDEBAR_WIDTH);

    fireEvent.keyDown(separator(), { key: 'Home' });
    expect(reportedWidth()).toBe(MIN_SIDEBAR_WIDTH);
  });

  it('leaves other keys to the page', () => {
    render(<SidebarResizer />);

    const handled = fireEvent.keyDown(separator(), { key: 'ArrowRight' });
    const ignored = fireEvent.keyDown(separator(), { key: 'PageUp' });

    // fireEvent returns false when a handler called preventDefault.
    expect(handled).toBe(false);
    expect(ignored).toBe(true);
  });

  it('describes the range it is adjusting', () => {
    render(<SidebarResizer />);

    const handle = separator();
    expect(handle.getAttribute('aria-orientation')).toBe('vertical');
    expect(handle.getAttribute('aria-label')).toBe('appShell.resizeSidebar');
    expect(handle.getAttribute('aria-valuemin')).toBe(String(MIN_SIDEBAR_WIDTH));
    expect(handle.getAttribute('aria-valuemax')).toBe(String(MAX_SIDEBAR_WIDTH));
    expect(handle.tabIndex).toBe(0);
  });
});

// `MAX_SIDEBAR_WIDTH` is a constant, so on its own it lets the sidebar take a
// share of a narrow window that leaves nothing workable beside it: at 768 a full
// drag leaves 272px, and inline Settings spends 196 of that on its rail before
// the page gets any. The cap is the sidebar's own business — every consumer
// would otherwise re-derive the same budget — and it is expressed as the width
// the shell keeps free, not as a second magic number.
describe('SidebarResizer viewport budget', () => {
  const setViewport = (width: number) => Object.defineProperty(window, 'innerWidth', {
    configurable: true,
    writable: true,
    value: width,
  });

  afterEach(() => setViewport(1024));

  it.each([
    // [viewport, widest allowed, why]
    [1920, MAX_SIDEBAR_WIDTH, 'room to spare: the plain maximum'],
    [1016, MAX_SIDEBAR_WIDTH, 'exactly enough for the maximum and the reserve'],
    [900, 380, 'the reserve wins, so the sidebar stops early'],
    [DESKTOP_MIN_WIDTH, MIN_SIDEBAR_WIDTH, 'the narrowest desktop affords no growth'],
    [600, MIN_SIDEBAR_WIDTH, 'below md nothing is rendered, so nothing to constrain'],
  ])('caps the sidebar at %ipx viewport (%s)', (viewport, widest) => {
    expect(maxSidebarWidth(viewport)).toBe(widest);
    expect(viewport - maxSidebarWidth(viewport)).toBeGreaterThanOrEqual(
      Math.min(MIN_WIDTH_BESIDE_SIDEBAR, viewport - MIN_SIDEBAR_WIDTH),
    );
  });

  it('stops the drag at the cap and says so', () => {
    setViewport(900);
    render(<SidebarResizer />);

    expect(separator().getAttribute('aria-valuemax')).toBe('380');
    drag(900);
    expect(reportedWidth()).toBe(380);
    // `End` asks for the constant maximum and lands on the affordable one.
    fireEvent.keyDown(separator(), { key: 'End' });
    expect(reportedWidth()).toBe(380);
  });

  // Dragging wide and then narrowing the window reaches the same starved layout
  // the cap exists to prevent, so the cap follows the viewport too.
  it('gives width back when the window narrows under it', async () => {
    render(<SidebarResizer />);
    drag(900);
    expect(reportedWidth()).toBe(MAX_SIDEBAR_WIDTH);

    setViewport(900);
    fireEvent(window, new Event('resize'));
    await waitFor(() => expect(reportedWidth()).toBe(380));
    expect(publishedWidth()).toBe('380px');
    expect(separator().getAttribute('aria-valuemax')).toBe('380');
  });

  // An untouched shell needs no JS to lay itself out, and a resize must not
  // quietly take that over just because the budget moved.
  it('leaves the stylesheet in charge while nothing has been dragged', async () => {
    setViewport(900);
    render(<SidebarResizer />);
    expect(separator().getAttribute('aria-valuemax')).toBe('380');
    expect(publishedWidth()).toBe('');

    setViewport(1400);
    fireEvent(window, new Event('resize'));
    await waitFor(() => expect(separator().getAttribute('aria-valuemax'))
      .toBe(String(MAX_SIDEBAR_WIDTH)));
    expect(publishedWidth()).toBe('');
  });
});
