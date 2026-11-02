/* @vitest-environment jsdom */

import { useRef } from 'react';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useIosKeyboardInset } from './useIosKeyboardInset';

// jsdom implements neither the visual viewport nor contenteditable editing, so
// the browser surfaces the hook reads are stood in for here: a viewport whose
// size the test sets, the mobile breakpoint, and `isContentEditable`.
class FakeVisualViewport extends EventTarget {
  offsetTop = 0;
  height = 800;
}

let vv: FakeVisualViewport;

beforeEach(() => {
  vi.useFakeTimers();
  vv = new FakeVisualViewport();
  Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  });
  Object.defineProperty(HTMLElement.prototype, 'isContentEditable', {
    configurable: true,
    get(this: HTMLElement) {
      return this.closest('[contenteditable]:not([contenteditable="false"])') !== null;
    },
  });
  HTMLElement.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function ChatSurface() {
  const ref = useRef<HTMLDivElement | null>(null);
  useIosKeyboardInset(ref);
  return (
    <div ref={ref} data-testid="surface">
      <input aria-label="Title" />
      {/* The chat composer is a Lexical contenteditable, not a textarea. */}
      <div contentEditable suppressContentEditableWarning tabIndex={0} aria-label="Composer" />
    </div>
  );
}

// The keyboard opens: the visual viewport shrinks and pans, then settles.
function openKeyboard() {
  vv.offsetTop = 120;
  vv.height = 380;
  act(() => {
    vv.dispatchEvent(new Event('resize'));
    vi.advanceTimersByTime(140);
  });
}

describe('useIosKeyboardInset with the contenteditable chat composer', () => {
  it('sizes the surface to the visible area and reveals the focused composer', () => {
    render(<ChatSurface />);
    const composer = screen.getByLabelText('Composer');
    act(() => composer.focus());

    openKeyboard();

    expect(screen.getByTestId('surface').style.height).toBe('500px');
    expect(composer.scrollIntoView).toHaveBeenCalledWith({ block: 'nearest', behavior: 'smooth' });
  });

  it('keeps the inset when focus moves from another field to the composer', () => {
    render(<ChatSurface />);
    act(() => screen.getByLabelText('Title').focus());
    openKeyboard();

    act(() => {
      screen.getByLabelText('Composer').focus();
      vi.advanceTimersByTime(120);
    });

    expect(screen.getByTestId('surface').style.height).toBe('500px');
  });

  it('holds the reveal while an input method composes and applies it at compositionend', () => {
    render(<ChatSurface />);
    const composer = screen.getByLabelText('Composer');
    act(() => composer.focus());
    act(() => {
      composer.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
    });

    openKeyboard();
    expect(screen.getByTestId('surface').style.height).toBe('500px');
    expect(composer.scrollIntoView).not.toHaveBeenCalled();

    act(() => {
      composer.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '你好' }));
      vi.advanceTimersByTime(140);
    });
    expect(composer.scrollIntoView).toHaveBeenCalledTimes(1);
  });
});
