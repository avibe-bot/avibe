// @vitest-environment jsdom
import { createRef } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SelectionQuoteToolbar } from './SelectionQuoteToolbar';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const TEXT = 'Selected 中文消息 🌱';
const quote = vi.fn();
const ask = vi.fn();
const writeText = vi.fn().mockResolvedValue(undefined);
const rect = { top: 200, bottom: 236, left: 40, right: 340, width: 300, height: 36 };

// jsdom has selections but no layout, pointer capture, or PointerEvent. Only
// those browser boundaries are supplied; selection tracking and action dispatch
// come from the real component. Before the fix, clearing the selection during a
// press unmounts the action and none of its consumers receive the selected text.
class TestPointerEvent extends MouseEvent {
  pointerId: number;
  isPrimary: boolean;
  pointerType: string;

  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
    this.isPrimary = init.isPrimary ?? true;
    this.pointerType = init.pointerType ?? 'touch';
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  vi.stubGlobal('PointerEvent', TestPointerEvent);
  vi.stubGlobal('matchMedia', () => ({ matches: true }));
  Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
    configurable: true,
    value: () => rect,
  });
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(rect as DOMRect);
  Object.defineProperty(HTMLElement.prototype, 'setPointerCapture', {
    configurable: true,
    value: vi.fn(),
  });
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText },
  });
});

afterEach(() => {
  cleanup();
  window.getSelection()?.removeAllRanges();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(Range.prototype, 'getBoundingClientRect');
  Reflect.deleteProperty(HTMLElement.prototype, 'setPointerCapture');
  Reflect.deleteProperty(navigator, 'clipboard');
});

function mountToolbar() {
  const containerRef = createRef<HTMLDivElement>();
  render(
    <>
      <div ref={containerRef}>{TEXT}</div>
      <SelectionQuoteToolbar containerRef={containerRef} onQuote={quote} onAskInNew={ask} />
    </>,
  );
  const range = document.createRange();
  range.selectNodeContents(containerRef.current!);
  window.getSelection()!.addRange(range);
  fireEvent(document, new Event('selectionchange'));
  act(() => vi.advanceTimersByTime(150));
  return containerRef.current!;
}

function collapseSelection() {
  window.getSelection()!.removeAllRanges();
  fireEvent(document, new Event('selectionchange'));
  act(() => vi.advanceTimersByTime(200));
}

const press = { pointerId: 1, isPrimary: true, pointerType: 'touch', button: 0, clientX: 100, clientY: 220 };

describe('chat selection action gesture lifetime', () => {
  it.each([
    ['quote', quote],
    ['askInNew', ask],
    ['copy', writeText],
  ] as const)('keeps %s alive until release after selection collapse and scrolling', async (action, consumer) => {
    const container = mountToolbar();
    const button = screen.getByRole('button', { name: `chat.selection.${action}` });
    fireEvent.pointerDown(button, press);
    expect(consumer).not.toHaveBeenCalled();

    collapseSelection();
    fireEvent.scroll(container);
    act(() => vi.advanceTimersByTime(200));
    expect(screen.getByRole('toolbar').contains(button)).toBe(true);

    await act(async () => fireEvent.pointerUp(button, press));
    // Browsers may follow pointerup with click. It must not run the action twice.
    fireEvent.click(button, { detail: 1 });
    expect(consumer).toHaveBeenCalledExactlyOnceWith(TEXT);
  });

  it.each(['pointerCancel', 'lostPointerCapture'] as const)(
    '%s ends ownership without activating',
    (event) => {
      mountToolbar();
      const button = screen.getByRole('button', { name: 'chat.selection.quote' });
      fireEvent.pointerDown(button, press);
      collapseSelection();
      expect(screen.getByRole('toolbar').contains(button)).toBe(true);
      fireEvent[event](button, press);
      expect(screen.queryByRole('toolbar')).toBeNull();
      expect(quote).not.toHaveBeenCalled();
    },
  );

  it('does not activate when a captured pointer releases outside the button', () => {
    mountToolbar();
    const button = screen.getByRole('button', { name: 'chat.selection.quote' });
    fireEvent.pointerDown(button, press);
    collapseSelection();
    expect(screen.getByRole('toolbar').contains(button)).toBe(true);
    fireEvent.pointerUp(button, { ...press, clientX: 500 });
    expect(quote).not.toHaveBeenCalled();
    expect(screen.queryByRole('toolbar')).toBeNull();
  });

  it('ignores another pointer while the primary press owns the selection', () => {
    mountToolbar();
    const button = screen.getByRole('button', { name: 'chat.selection.quote' });
    fireEvent.pointerDown(button, press);
    collapseSelection();
    fireEvent.pointerDown(button, { ...press, pointerId: 2, isPrimary: false });
    fireEvent.pointerUp(button, { ...press, pointerId: 2, isPrimary: false });
    expect(quote).not.toHaveBeenCalled();
    fireEvent.pointerUp(button, press);
    expect(quote).toHaveBeenCalledExactlyOnceWith(TEXT);
  });

  it('does not treat a release without a primary press as activation', () => {
    mountToolbar();
    const button = screen.getByRole('button', { name: 'chat.selection.quote' });
    fireEvent.pointerDown(button, { ...press, button: 2 });
    fireEvent.pointerUp(button, { ...press, button: 2 });
    expect(quote).not.toHaveBeenCalled();
    collapseSelection();
    expect(screen.queryByRole('toolbar')).toBeNull();
  });

  it.each(['Enter', ' '])('preserves %s keyboard activation', (key) => {
    mountToolbar();
    fireEvent.keyDown(screen.getByRole('button', { name: 'chat.selection.quote' }), { key });
    expect(quote).toHaveBeenCalledExactlyOnceWith(TEXT);
  });

  it('dismisses a cleared selection when no action is being pressed', () => {
    mountToolbar();
    collapseSelection();
    expect(screen.queryByRole('toolbar')).toBeNull();
  });
});
