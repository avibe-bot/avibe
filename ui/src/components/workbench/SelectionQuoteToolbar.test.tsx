// @vitest-environment jsdom
import { createRef } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Markdown } from '../ui/markdown';
import { SelectionQuoteToolbar } from './SelectionQuoteToolbar';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const TEXT = 'Selected 中文消息 🌱';
const NEXT_TEXT = 'New selection 新消息';
const quote = vi.fn();
const ask = vi.fn();
const writeText = vi.fn().mockResolvedValue(undefined);
const defaultBounds = {
  top: 200,
  bottom: 236,
  left: 40,
  right: 340,
  width: 300,
  height: 36,
};

let selectionBounds = defaultBounds;
let selectionLineRects = [defaultBounds];
let coarsePointer = true;
const originalInnerHeight = window.innerHeight;
const originalInnerWidth = window.innerWidth;

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
  coarsePointer = true;
  vi.stubGlobal('PointerEvent', TestPointerEvent);
  vi.stubGlobal('matchMedia', () => ({ matches: coarsePointer }));
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 844 });
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 });
  Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
    configurable: true,
    value: () => selectionBounds,
  });
  Object.defineProperty(Range.prototype, 'getClientRects', {
    configurable: true,
    value: () => selectionLineRects,
  });
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(
    defaultBounds as DOMRect,
  );
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText },
  });
  selectionBounds = defaultBounds;
  selectionLineRects = [defaultBounds];
});

afterEach(() => {
  cleanup();
  window.getSelection()?.removeAllRanges();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Object.defineProperty(window, 'innerHeight', {
    configurable: true,
    value: originalInnerHeight,
  });
  Object.defineProperty(window, 'innerWidth', {
    configurable: true,
    value: originalInnerWidth,
  });
  Reflect.deleteProperty(Range.prototype, 'getBoundingClientRect');
  Reflect.deleteProperty(Range.prototype, 'getClientRects');
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
  selectContainer(containerRef.current!);
  return containerRef.current!;
}

const BUBBLE = 'Use **bold** now';

function mountBubble() {
  const containerRef = createRef<HTMLDivElement>();
  render(
    <>
      <div ref={containerRef}>
        <Markdown content={BUBBLE} />
        <Markdown content="Another bubble" />
      </div>
      <SelectionQuoteToolbar containerRef={containerRef} onQuote={quote} />
    </>,
  );
  return containerRef.current!;
}

function settle() {
  fireEvent(document, new Event('selectionchange'));
  act(() => vi.advanceTimersByTime(150));
}

function selectContainer(container: HTMLDivElement) {
  const range = document.createRange();
  range.selectNodeContents(container);
  window.getSelection()!.removeAllRanges();
  window.getSelection()!.addRange(range);
  fireEvent(document, new Event('selectionchange'));
  act(() => vi.advanceTimersByTime(150));
}

function clearSelection() {
  window.getSelection()!.removeAllRanges();
  fireEvent(document, new Event('selectionchange'));
  act(() => vi.advanceTimersByTime(200));
}

const press = {
  pointerId: 1,
  isPrimary: true,
  pointerType: 'touch',
  button: 0,
  clientX: 100,
  clientY: 220,
};

describe('chat selection action gesture lifetime', () => {
  it.each([
    ['quote', quote],
    ['askInNew', ask],
  ] as const)('activates %s on a complete touch gesture', (action, consumer) => {
    mountToolbar();
    const button = screen.getByRole('button', { name: `chat.selection.${action}` });

    fireEvent.pointerDown(button, press);
    fireEvent.pointerUp(button, press);

    expect(consumer).toHaveBeenCalledExactlyOnceWith(TEXT);
    expect(screen.queryByRole('toolbar')).toBeNull();
  });

  it('copies on a complete touch gesture and then dismisses', async () => {
    mountToolbar();
    const button = screen.getByRole('button', { name: 'chat.selection.copy' });

    await act(async () => {
      fireEvent.pointerDown(button, press);
      fireEvent.pointerUp(button, press);
      await Promise.resolve();
    });

    // No rendered Markdown under the selection: the plain text is copied, and
    // there is no bubble to select all of.
    expect(writeText).toHaveBeenCalledExactlyOnceWith(TEXT);
    expect(screen.queryByRole('button', { name: 'chat.selection.selectAll' })).toBeNull();
    expect(screen.queryByRole('toolbar')).not.toBeNull();
    act(() => vi.advanceTimersByTime(800));
    expect(screen.queryByRole('toolbar')).toBeNull();
  });

  it('copies the Markdown the selection was rendered from', async () => {
    const container = mountBubble();
    const range = document.createRange();
    range.selectNodeContents(container.querySelector('.vr-markdown')!);
    window.getSelection()!.addRange(range);
    settle();

    await act(async () => {
      const button = screen.getByRole('button', { name: 'chat.selection.copy' });
      fireEvent.pointerDown(button, press);
      fireEvent.pointerUp(button, press);
      await Promise.resolve();
    });

    expect(writeText).toHaveBeenCalledExactlyOnceWith(BUBBLE);
  });

  it('copies no more of a bubble than is selected', async () => {
    const container = mountBubble();
    const strong = container.querySelector('strong')!;
    const range = document.createRange();
    range.setStart(strong.firstChild!, 0);
    range.setEnd(strong.nextSibling!, ' no'.length);
    window.getSelection()!.addRange(range);
    settle();

    await act(async () => {
      const button = screen.getByRole('button', { name: 'chat.selection.copy' });
      fireEvent.pointerDown(button, press);
      fireEvent.pointerUp(button, press);
      await Promise.resolve();
    });

    expect(writeText).toHaveBeenCalledExactlyOnceWith('**bold** no');
  });

  it('offers Copy for an image alone, which has Markdown but no text', async () => {
    const containerRef = createRef<HTMLDivElement>();
    const image = '![chart](/api/media/abc123)';
    render(
      <>
        <div ref={containerRef}>
          <Markdown content={`Intro.\n\n${image}`} />
        </div>
        <SelectionQuoteToolbar containerRef={containerRef} onQuote={quote} />
      </>,
    );
    const range = document.createRange();
    range.selectNode(containerRef.current!.querySelector('img')!);
    window.getSelection()!.addRange(range);
    settle();

    expect(screen.queryByRole('button', { name: 'chat.selection.quote' })).toBeNull();
    await act(async () => {
      const copy = screen.getByRole('button', { name: 'chat.selection.copy' });
      fireEvent.pointerDown(copy, press);
      fireEvent.pointerUp(copy, press);
      await Promise.resolve();
    });
    expect(writeText).toHaveBeenCalledExactlyOnceWith(image);
  });

  it('copies the whole bubble right after Select all, before the selection settles', async () => {
    const containerRef = createRef<HTMLDivElement>();
    const content = 'Use **bold** now.\n\nSecond paragraph.';
    render(
      <>
        <div ref={containerRef}>
          <Markdown content={content} />
        </div>
        <SelectionQuoteToolbar containerRef={containerRef} onQuote={quote} />
      </>,
    );
    const range = document.createRange();
    range.selectNodeContents(containerRef.current!.querySelector('strong')!);
    window.getSelection()!.addRange(range);
    settle();

    // Two clicks, with no time for the 150 ms settle between them.
    const selectAll = screen.getByRole('button', { name: 'chat.selection.selectAll' });
    fireEvent.pointerDown(selectAll, press);
    fireEvent.pointerUp(selectAll, press);
    await act(async () => {
      const copy = screen.getByRole('button', { name: 'chat.selection.copy' });
      fireEvent.pointerDown(copy, press);
      fireEvent.pointerUp(copy, press);
      await Promise.resolve();
    });

    expect(writeText).toHaveBeenCalledExactlyOnceWith(content);
  });

  it('leaves a selection made after Copy in place', async () => {
    const container = mountToolbar();
    await act(async () => {
      const copy = screen.getByRole('button', { name: 'chat.selection.copy' });
      fireEvent.pointerDown(copy, press);
      fireEvent.pointerUp(copy, press);
      await Promise.resolve();
    });
    expect(writeText).toHaveBeenCalledOnce();

    container.textContent = NEXT_TEXT;
    selectContainer(container);
    act(() => vi.advanceTimersByTime(800));

    expect(window.getSelection()!.toString()).toBe(NEXT_TEXT);
    expect(screen.queryByRole('button', { name: 'chat.selection.quote' })).not.toBeNull();
  });

  it('selects the whole bubble on desktop and stays up over the new selection', async () => {
    coarsePointer = false;
    const container = mountBubble();
    const range = document.createRange();
    range.selectNodeContents(container.querySelector('strong')!);
    window.getSelection()!.addRange(range);
    settle();

    const selectAll = screen.getByRole('button', { name: 'chat.selection.selectAll' });
    fireEvent.pointerDown(selectAll, { ...press, pointerType: 'mouse' });
    fireEvent.pointerUp(selectAll, { ...press, pointerType: 'mouse' });
    settle();

    expect(window.getSelection()!.toString()).toBe('Use bold now');
    expect(screen.queryByRole('toolbar')).not.toBeNull();
    await act(async () => {
      const copy = screen.getByRole('button', { name: 'chat.selection.copy' });
      fireEvent.pointerDown(copy, { ...press, pointerType: 'mouse' });
      fireEvent.pointerUp(copy, { ...press, pointerType: 'mouse' });
      await Promise.resolve();
    });
    expect(writeText).toHaveBeenCalledExactlyOnceWith(BUBBLE);
  });

  it('keeps a complete gesture alive when the selection clears before release', () => {
    mountToolbar();
    const button = screen.getByRole('button', { name: 'chat.selection.quote' });

    fireEvent.pointerDown(button, press);
    clearSelection();
    fireEvent.pointerUp(button, press);

    expect(quote).toHaveBeenCalledExactlyOnceWith(TEXT);
    expect(screen.queryByRole('toolbar')).toBeNull();
  });

  it('recovers when iOS delivers pointerdown but never delivers an end event', () => {
    const container = mountToolbar();
    const button = screen.getByRole('button', { name: 'chat.selection.quote' });

    // iOS can route this touch to its native selection recognizer. The page
    // receives pointerdown, but no pointerup/cancel/lostpointercapture.
    fireEvent.pointerDown(button, press);
    clearSelection();
    act(() => vi.advanceTimersByTime(600));
    expect(screen.queryByRole('toolbar')).toBeNull();

    container.textContent = NEXT_TEXT;
    selectContainer(container);
    const nextButton = screen.getByRole('button', { name: 'chat.selection.quote' });
    fireEvent.pointerDown(nextButton, press);
    fireEvent.pointerUp(nextButton, press);

    expect(quote).toHaveBeenCalledExactlyOnceWith(NEXT_TEXT);
  });

  it('recomputes after a deferred press is released outside the button', () => {
    mountToolbar();
    const button = screen.getByRole('button', { name: 'chat.selection.quote' });

    fireEvent.pointerDown(button, press);
    clearSelection();
    fireEvent.pointerUp(button, { ...press, clientX: 500 });

    expect(quote).not.toHaveBeenCalled();
    expect(screen.queryByRole('toolbar')).toBeNull();
  });

  it('allows a long primary press to activate', () => {
    mountToolbar();
    const button = screen.getByRole('button', { name: 'chat.selection.quote' });

    fireEvent.pointerDown(button, press);
    act(() => vi.advanceTimersByTime(800));
    fireEvent.pointerUp(button, press);

    expect(quote).toHaveBeenCalledExactlyOnceWith(TEXT);
  });

  it('preserves a complete gesture across selectionchange', () => {
    mountToolbar();
    const button = screen.getByRole('button', { name: 'chat.selection.quote' });

    fireEvent.pointerDown(button, press);
    fireEvent(document, new Event('selectionchange'));
    fireEvent.pointerUp(button, press);

    expect(quote).toHaveBeenCalledExactlyOnceWith(TEXT);
  });

  it('cancels a pending recompute when a press defers selection clearing', () => {
    mountToolbar();
    const button = screen.getByRole('button', { name: 'chat.selection.quote' });

    fireEvent(document, new Event('selectionchange'));
    act(() => vi.advanceTimersByTime(50));
    fireEvent.pointerDown(button, press);
    window.getSelection()!.removeAllRanges();
    fireEvent(document, new Event('selectionchange'));

    act(() => vi.advanceTimersByTime(100));
    expect(screen.queryByRole('toolbar')).not.toBeNull();

    fireEvent.pointerUp(button, press);

    expect(quote).toHaveBeenCalledExactlyOnceWith(TEXT);
  });

  it.each([
    ['secondary button', { ...press, button: 2, isPrimary: false }],
    ['without a matching press', null],
  ])('does not activate %s', (_case, down) => {
    mountToolbar();
    const button = screen.getByRole('button', { name: 'chat.selection.quote' });

    if (down) fireEvent.pointerDown(button, down);
    fireEvent.pointerUp(button, press);

    expect(quote).not.toHaveBeenCalled();
  });

  it('does not activate when the pointer id or button does not match', () => {
    mountToolbar();
    const quoteButton = screen.getByRole('button', { name: 'chat.selection.quote' });
    const askButton = screen.getByRole('button', { name: 'chat.selection.askInNew' });

    fireEvent.pointerDown(quoteButton, press);
    fireEvent.pointerUp(quoteButton, { ...press, pointerId: 2 });
    expect(quote).not.toHaveBeenCalled();

    fireEvent.pointerDown(quoteButton, press);
    fireEvent.pointerUp(askButton, press);
    expect(quote).not.toHaveBeenCalled();
    expect(ask).not.toHaveBeenCalled();
  });

  it('does not reuse a press after a new gesture starts elsewhere', () => {
    mountToolbar();
    const button = screen.getByRole('button', { name: 'chat.selection.quote' });

    fireEvent.pointerDown(button, press);
    fireEvent.pointerDown(document.body, press);
    fireEvent.pointerUp(button, press);

    expect(quote).not.toHaveBeenCalled();
  });

  it('hides after a cleared selection while scrolling', () => {
    const container = mountToolbar();
    expect(screen.queryByRole('toolbar')).not.toBeNull();

    clearSelection();
    fireEvent.scroll(container);
    act(() => vi.advanceTimersByTime(200));

    expect(screen.queryByRole('toolbar')).toBeNull();
  });

  it('keeps the touch toolbar outside the selection-handle hit region above', () => {
    mountToolbar();
    const toolbar = screen.getByRole('toolbar');
    const top = Number.parseFloat(toolbar.getAttribute('style')!.match(/top:\s*([^;]+)/)![1]);

    expect(top + 36).toBeLessThanOrEqual(selectionLineRects[0].top - 44 - 8);
  });

  it('keeps the touch toolbar outside the selection-handle hit region below', () => {
    selectionBounds = { ...defaultBounds, top: 700, bottom: 736 };
    selectionLineRects = [selectionBounds];
    mountToolbar();
    const toolbar = screen.getByRole('toolbar');
    const top = Number.parseFloat(toolbar.getAttribute('style')!.match(/top:\s*([^;]+)/)![1]);

    expect(top).toBeGreaterThanOrEqual(selectionBounds.bottom + 44 + 8);
  });

  it('clamps a touch toolbar when the selection is above the viewport', () => {
    selectionBounds = { ...defaultBounds, top: -120, bottom: -84 };
    selectionLineRects = [selectionBounds];
    mountToolbar();
    const toolbar = screen.getByRole('toolbar');
    const top = Number.parseFloat(toolbar.getAttribute('style')!.match(/top:\s*([^;]+)/)![1]);

    expect(top).toBe(8);
  });

  it('clamps a touch toolbar when the selection is below the viewport', () => {
    selectionBounds = { ...defaultBounds, top: 900, bottom: 936 };
    selectionLineRects = [selectionBounds];
    mountToolbar();
    const toolbar = screen.getByRole('toolbar');
    const top = Number.parseFloat(toolbar.getAttribute('style')!.match(/top:\s*([^;]+)/)![1]);

    expect(top).toBe(800);
  });

  it.each([
    {
      name: 'first line is near the top and the last line is below the viewport',
      bounds: { top: 20, bottom: 1036, left: 40, right: 340, width: 300, height: 1016 },
      lines: [
        { top: 20, bottom: 56, left: 40, right: 340, width: 300, height: 36 },
        { top: 1000, bottom: 1036, left: 40, right: 340, width: 300, height: 36 },
      ],
    },
    {
      name: 'first line is above the viewport and the last line is near the bottom',
      bounds: { top: -120, bottom: 800, left: 40, right: 340, width: 300, height: 920 },
      lines: [
        { top: -120, bottom: -84, left: 40, right: 340, width: 300, height: 36 },
        { top: 764, bottom: 800, left: 40, right: 340, width: 300, height: 36 },
      ],
    },
    {
      name: 'both endpoint lines are visible with a safe middle band',
      bounds: { top: 80, bottom: 764, left: 40, right: 340, width: 300, height: 684 },
      lines: [
        { top: 80, bottom: 116, left: 40, right: 340, width: 300, height: 36 },
        { top: 728, bottom: 764, left: 40, right: 340, width: 300, height: 36 },
      ],
    },
  ])('avoids both endpoint hit regions when $name', ({ bounds, lines }) => {
    selectionBounds = bounds;
    selectionLineRects = lines;
    mountToolbar();
    const toolbar = screen.getByRole('toolbar');
    const top = Number.parseFloat(toolbar.getAttribute('style')!.match(/top:\s*([^;]+)/)![1]);

    for (const line of [lines[0], lines[lines.length - 1]]) {
      const outsideHitRegion = (
        top >= line.bottom + 44 + 8
        || top + 36 <= line.top - 44 - 8
      );
      expect(outsideHitRegion).toBe(true);
    }
  });

  it('keeps desktop placement at the original selection gap', () => {
    coarsePointer = false;
    selectionBounds = { ...defaultBounds, top: 80, bottom: 116 };
    selectionLineRects = [selectionBounds];
    mountToolbar();
    const toolbar = screen.getByRole('toolbar');
    const top = Number.parseFloat(toolbar.getAttribute('style')!.match(/top:\s*([^;]+)/)![1]);

    expect(top).toBe(36);
  });

  it.each([
    ['quote', () => quote],
    ['askInNew', () => ask],
  ] as const)('activates %s from a click alone, as assistive technology sends', (action, consumer) => {
    mountToolbar();
    fireEvent.click(screen.getByRole('button', { name: `chat.selection.${action}` }));
    expect(consumer()).toHaveBeenCalledExactlyOnceWith(TEXT);
  });

  it('selects the whole bubble from a click alone', () => {
    coarsePointer = false;
    const containerRef = createRef<HTMLDivElement>();
    render(
      <>
        <div ref={containerRef}><Markdown content={BUBBLE} /></div>
        <SelectionQuoteToolbar containerRef={containerRef} onQuote={quote} />
      </>,
    );
    const range = document.createRange();
    range.selectNodeContents(containerRef.current!.querySelector('strong')!);
    window.getSelection()!.addRange(range);
    settle();

    fireEvent.click(screen.getByRole('button', { name: 'chat.selection.selectAll' }));

    expect(window.getSelection()!.toString()).toBe('Use bold now');
  });

  it('runs once for a pointer activation and the click that follows it', async () => {
    // Copy keeps the toolbar mounted, so the following click reaches the button.
    mountToolbar();
    const button = screen.getByRole('button', { name: 'chat.selection.copy' });
    await act(async () => {
      fireEvent.pointerDown(button, press);
      fireEvent.pointerUp(button, press);
      fireEvent.click(button);
      await Promise.resolve();
    });
    expect(writeText).toHaveBeenCalledExactlyOnceWith(TEXT);
  });

  it.each(['Enter', ' '])('preserves %s keyboard activation', (key) => {
    mountToolbar();
    fireEvent.keyDown(screen.getByRole('button', { name: 'chat.selection.quote' }), { key });
    expect(quote).toHaveBeenCalledExactlyOnceWith(TEXT);
  });
});
