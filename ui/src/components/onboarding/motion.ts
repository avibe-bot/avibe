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
 * Whether this particular composition is on screen. A hidden tab is only half the
 * question: on a short window the diagram can sit entirely above the viewport while
 * the document is perfectly visible, and its clock and CSS effects would keep running
 * for nobody. `threshold: 0` is the same reading the incumbent observer in
 * `vault-chat-requests` takes — any intersecting pixel counts — so a composition that
 * is only partly scrolled in stays live rather than stuttering at the edge.
 *
 * Without an element or without the API (jsdom) this answers "on screen", so a
 * consumer that never attaches the ref keeps the behaviour it had before. Nothing
 * resets it when the effect cannot observe: an observer reports the element's current
 * intersection as soon as it starts watching, so the only unobserved state that
 * outlives the initial one is the element going away, and that is the unmount.
 */
function useElementOnScreen() {
  const [element, setElement] = useState<HTMLElement | null>(null);
  const [onScreen, setOnScreen] = useState(true);
  useEffect(() => {
    if (!element || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      ([entry]) => setOnScreen(entry.isIntersecting),
      { threshold: 0 },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);
  return [setElement, onScreen] as const;
}

/**
 * The one answer to "is this onboarding animation running", used by the story and the
 * access tiles so a reduced-motion preference, a hidden tab and being scrolled out of
 * sight all stop it the same way. Each consumer attaches the returned `ref` to the
 * element it draws in, so the two suspend independently: at desktop sizes both are
 * normally on screen together, but on a short or narrow window they can have different
 * viewport intersection, and each should answer for its own. There is no user-facing
 * control here on purpose: the
 * approved design has no playback toolbar, so this is lifecycle only. `running` also
 * drives the diagram's `data-motion` attribute, which is how the CSS-owned effects (the
 * skeleton shimmer and the test ticks) are held at their current frame rather than left
 * to play on against a stopped clock.
 */
export function useOnboardingMotion() {
  const reducedMotion = useReducedMotion() === true;
  const documentVisible = useDocumentVisible();
  const [ref, onScreen] = useElementOnScreen();
  return { ref, reducedMotion, running: !reducedMotion && documentVisible && onScreen };
}
