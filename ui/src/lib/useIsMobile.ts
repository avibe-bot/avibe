import * as React from 'react';

// The phone breakpoint used across the app (Tailwind `md`), matching the
// `max-md:` bottom-sheet styling in ui/dialog.tsx so a sheet and a dialog never
// disagree about which side of the breakpoint they're on.
const PHONE_QUERY = '(max-width: 767px)';

/**
 * True while the viewport is phone-sized.
 *
 * Only for surfaces whose *structure* differs per breakpoint — an anchored
 * popover becoming a bottom sheet, say, where CSS alone can't re-parent the
 * content. Prefer Tailwind responsive classes for anything that is purely
 * visual; they need no JS and can't flash the wrong layout on first paint.
 */
export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = React.useState(
    () => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(PHONE_QUERY).matches,
  );

  React.useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const mql = window.matchMedia(PHONE_QUERY);
    const sync = () => setIsMobile(mql.matches);
    sync();
    mql.addEventListener('change', sync);
    return () => mql.removeEventListener('change', sync);
  }, []);

  return isMobile;
}
