import { useEffect, useState } from 'react';

export const DESKTOP_MEDIA_QUERY = '(min-width: 768px)';

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
