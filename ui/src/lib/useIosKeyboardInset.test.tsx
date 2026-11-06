/** @vitest-environment jsdom */

import { cleanup, render } from '@testing-library/react';
import { useRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const native = vi.hoisted(() => ({ keyboardResizesViewport: false }));
vi.mock('./native', () => ({
  hasNativeCapability: (capability: string) =>
    capability === 'keyboardResizesViewport' && native.keyboardResizesViewport,
}));

import { useIosKeyboardInset } from './useIosKeyboardInset';

const viewport = {
  offsetTop: 0,
  height: 600,
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
};
const media = {
  matches: true,
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
};

function Harness() {
  const ref = useRef<HTMLDivElement | null>(null);
  useIosKeyboardInset(ref);
  return <div ref={ref} />;
}

beforeEach(() => {
  native.keyboardResizesViewport = false;
  vi.clearAllMocks();
  Object.defineProperty(window, 'visualViewport', {
    configurable: true,
    value: viewport,
  });
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn(() => media),
  });
});

afterEach(() => cleanup());

describe('useIosKeyboardInset', () => {
  it('lets a native viewport-resize capability own keyboard layout', () => {
    native.keyboardResizesViewport = true;
    render(<Harness />);

    expect(window.matchMedia).not.toHaveBeenCalled();
    expect(viewport.addEventListener).not.toHaveBeenCalled();
  });

  it('retains the browser correction without the explicit capability', () => {
    render(<Harness />);

    expect(window.matchMedia).toHaveBeenCalledWith('(max-width: 767px)');
    expect(viewport.addEventListener).toHaveBeenCalledWith('resize', expect.any(Function));
    expect(viewport.addEventListener).toHaveBeenCalledWith('scroll', expect.any(Function));
  });
});

// The surface follows the keyboard as iOS reports it, measured on the owner's
// iPhone (Home Screen app): the visual viewport says the keyboard's final size
// once, as it starts to open or close; a tap our code turns into focus gets no
// pan from iOS; and the chat composer is a contenteditable editor.
describe('useIosKeyboardInset on the keyboard as iOS reports it', () => {
  class Viewport extends EventTarget {
    offsetTop = 0;
    height = 812;
  }
  let vv: Viewport;

  function Surface() {
    const ref = useRef<HTMLDivElement | null>(null);
    useIosKeyboardInset(ref);
    return (
      <div ref={ref} data-testid="surface">
        <div contentEditable suppressContentEditableWarning data-testid="editor" />
        <div tabIndex={0} data-testid="transcript" />
      </div>
    );
  }
  const surface = () => document.querySelector<HTMLElement>('[data-testid="surface"]')!;
  const editor = () => document.querySelector<HTMLElement>('[data-testid="editor"]')!;
  const keyboard = (height: number, offsetTop: number) => {
    vv.height = height;
    vv.offsetTop = offsetTop;
    vv.dispatchEvent(new Event('resize'));
  };

  beforeEach(() => {
    vi.useFakeTimers();
    vv = new Viewport();
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });
    // jsdom has no editability of its own.
    Object.defineProperty(HTMLElement.prototype, 'isContentEditable', {
      configurable: true,
      get(this: HTMLElement) {
        return this.getAttribute('contenteditable') === 'true';
      },
    });
  });
  afterEach(() => {
    vi.useRealTimers();
    Reflect.deleteProperty(HTMLElement.prototype, 'isContentEditable');
  });

  it('lifts the composer as the keyboard starts to open, with no pan from iOS', () => {
    render(<Surface />);
    editor().focus();

    keyboard(415, 0);

    // At once, not after the viewport settles.
    expect(surface().style.height).toBe('415px');
  });

  it('lets the surface go as the keyboard starts to close', () => {
    render(<Surface />);
    editor().focus();
    keyboard(415, 0);
    editor().blur();

    keyboard(812, 0);

    expect(surface().style.height).toBe('');
  });

  it('keeps the surface above the keyboard when focus comes back to the editor', () => {
    render(<Surface />);
    editor().focus();
    keyboard(415, 0);
    // The editor gives focus up and takes it straight back (as after a send).
    document.querySelector<HTMLElement>('[data-testid="transcript"]')!.focus();
    editor().focus();

    vi.advanceTimersByTime(200);

    expect(surface().style.height).toBe('415px');
  });

  it('reads a stale offset again once the keyboard has closed', () => {
    const scrollBy = vi.spyOn(window, 'scrollBy').mockImplementation(() => {});
    render(<Surface />);
    editor().focus();
    keyboard(415, 397);
    editor().blur();

    // iOS 26: the offset stays after the keyboard closed.
    keyboard(812, 397);

    expect(surface().style.height).toBe('');
    expect(scrollBy).toHaveBeenCalledWith(0, -1);
    expect(scrollBy).toHaveBeenCalledWith(0, 1);
    scrollBy.mockRestore();
  });

  it('leaves a pan of the viewport alone when no keyboard was up', () => {
    const scrollBy = vi.spyOn(window, 'scrollBy').mockImplementation(() => {});
    render(<Surface />);

    // A zoomed page panned by the finger: no field, nothing held.
    vv.offsetTop = 200;
    vv.dispatchEvent(new Event('scroll'));
    vi.advanceTimersByTime(200);

    expect(scrollBy).not.toHaveBeenCalled();
    scrollBy.mockRestore();
  });
});
