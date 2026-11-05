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
