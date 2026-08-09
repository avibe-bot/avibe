import { describe, expect, it } from 'vitest';

import { DESKTOP_MEDIA_QUERY, isDesktopViewport } from './useIsDesktop';

describe('isDesktopViewport', () => {
  it('uses the shell desktop breakpoint', () => {
    const targetWindow = {
      matchMedia: (query: string) => ({ matches: query === DESKTOP_MEDIA_QUERY }) as MediaQueryList,
    };

    expect(isDesktopViewport(targetWindow)).toBe(true);
    expect(isDesktopViewport(null)).toBe(false);
  });
});
