import { useEffect, useState } from 'react';

/** The shell's `md` breakpoint. Exported as a number too, because layout
 *  budgeting has to do arithmetic with it and a second literal would drift. */
export const DESKTOP_MIN_WIDTH = 768;
export const DESKTOP_MEDIA_QUERY = `(min-width: ${DESKTOP_MIN_WIDTH}px)`;

type MatchMediaOwner = Pick<Window, 'matchMedia'>;

export function isDesktopViewport(
  targetWindow: MatchMediaOwner | null = typeof window === 'undefined' ? null : window,
): boolean {
  return Boolean(targetWindow?.matchMedia?.(DESKTOP_MEDIA_QUERY).matches);
}

/** Reactive counterpart to the shell's `md` desktop breakpoint. */
export function useIsDesktop(): boolean {
  const [isDesktop, setIsDesktop] = useState(isDesktopViewport);

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const media = window.matchMedia(DESKTOP_MEDIA_QUERY);
    const sync = () => setIsDesktop(media.matches);
    sync();
    media.addEventListener('change', sync);
    return () => media.removeEventListener('change', sync);
  }, []);

  return isDesktop;
}
