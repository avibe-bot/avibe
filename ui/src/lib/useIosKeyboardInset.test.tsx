/** @vitest-environment jsdom */

import { cleanup, render } from '@testing-library/react';
import { useRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const native = vi.hoisted(() => ({ shell: false }));
vi.mock('./native', () => ({ isNativeShell: () => native.shell }));

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
  native.shell = false;
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
  it('lets the native shell own keyboard layout', () => {
    native.shell = true;
    render(<Harness />);

    expect(window.matchMedia).not.toHaveBeenCalled();
    expect(viewport.addEventListener).not.toHaveBeenCalled();
  });

  it('retains the browser correction outside the native shell', () => {
    render(<Harness />);

    expect(window.matchMedia).toHaveBeenCalledWith('(max-width: 767px)');
    expect(viewport.addEventListener).toHaveBeenCalledWith('resize', expect.any(Function));
    expect(viewport.addEventListener).toHaveBeenCalledWith('scroll', expect.any(Function));
  });
});
