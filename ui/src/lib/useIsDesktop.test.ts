// @vitest-environment jsdom

import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { DESKTOP_MEDIA_QUERY, isDesktopViewport, useIsDesktop } from './useIsDesktop';

describe('isDesktopViewport', () => {
  it('uses the shell desktop breakpoint', () => {
    const targetWindow = {
      matchMedia: (query: string) => ({ matches: query === DESKTOP_MEDIA_QUERY }) as MediaQueryList,
    };

    expect(isDesktopViewport(targetWindow)).toBe(true);
    expect(isDesktopViewport(null)).toBe(false);
  });

  it('updates when the viewport crosses the breakpoint', () => {
    let matches = false;
    const listeners = new Set<(event: MediaQueryListEvent) => void>();
    const media = {
      get matches() {
        return matches;
      },
      addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
      removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    } as unknown as MediaQueryList;
    const matchMedia = vi.fn().mockReturnValue(media);
    Object.defineProperty(window, 'matchMedia', { configurable: true, value: matchMedia });

    try {
      const { result } = renderHook(() => useIsDesktop());
      expect(result.current).toBe(false);

      act(() => {
        matches = true;
        listeners.forEach((listener) => listener({ matches: true, media: DESKTOP_MEDIA_QUERY } as MediaQueryListEvent));
      });

      expect(result.current).toBe(true);
      expect(matchMedia).toHaveBeenCalledWith(DESKTOP_MEDIA_QUERY);
    } finally {
      delete (window as Window & { matchMedia?: typeof matchMedia }).matchMedia;
    }
  });
});
