import { useEffect, useState } from 'react';
import { useReducedMotion } from 'framer-motion';

/**
 * Whether the document is being presented at all. An interval keeps firing in a
 * background tab — throttled, so its deltas arrive in one lump — and a CSS
 * animation keeps its own timeline running there too. Reading visibility here is
 * what lets both stop together.
 */
function useDocumentVisible() {
  const [visible, setVisible] = useState(() => typeof document === 'undefined' || !document.hidden);
  useEffect(() => {
    const read = () => setVisible(!document.hidden);
    read();
    document.addEventListener('visibilitychange', read);
    return () => document.removeEventListener('visibilitychange', read);
  }, []);
  return visible;
}

/**
 * The one answer to "is the onboarding animation running", shared by the story and
 * the access tiles so a reduced-motion preference and a hidden tab stop every part
 * of it at the same moment. There is no user-facing control here on purpose: the
 * approved design has no playback toolbar, so this is lifecycle only. `running`
 * also drives the diagram's `data-motion` attribute, which is how the CSS-owned
 * effects (the skeleton shimmer and the test ticks) are held at their current frame
 * rather than left to play on against a stopped clock.
 */
export function useOnboardingMotion() {
  const reducedMotion = useReducedMotion() === true;
  const visible = useDocumentVisible();
  return { reducedMotion, running: !reducedMotion && visible };
}
