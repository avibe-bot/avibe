import * as React from 'react';

import {
  MotionPreferenceContext,
  canAnimate,
  motionTiming,
  playFlip,
  prefersReducedMotion,
  readMotionNumber,
  springTiming,
} from '@/lib/motion';

// A small thing that comes and goes without popping (docs/plans/web-ui-motion.md
// M7: "the count badge shrinks and fades"): an unread count, a button that only
// applies while something is unread. `show` decides; when it turns false the
// element stays, inert and still showing the last `value` it had, while it plays
// its exit, and the things beside it then slide into the room it leaves instead
// of jumping. It comes back the mirrored way. Shown on mount, it is simply there
// (cache first: nothing replays).

type Kind = 'morph' | 'fade';

/** Read once per document: the tokens never change at run time, and reading `:root`'s computed
 *  style inside a commit would force a style recalculation of the page. */
let cachedTokens: { morph: number; enter: ReturnType<typeof motionTiming>; exit: ReturnType<typeof motionTiming>; snappy: ReturnType<typeof springTiming> } | null = null;
const presenceTokens = () => (cachedTokens ??= {
  morph: readMotionNumber('--motion-scale-morph', 0.6),
  enter: motionTiming('fast', 'enter'),
  exit: motionTiming('fast', 'exit'),
  snappy: springTiming('snappy'),
});
type Child = React.ReactElement<{ ref?: React.Ref<HTMLElement>; inert?: boolean; 'aria-hidden'?: boolean }>;

export function Presence<T = undefined>({
  show,
  value,
  kind = 'morph',
  children,
}: {
  show: boolean;
  /** What it shows (a count), kept as it was while it leaves. */
  value?: T;
  /** `morph`: shrinks to `--motion-scale-morph` as it fades (a badge, an icon). `fade`: opacity only (a button). */
  kind?: Kind;
  /** One element that takes a ref. */
  children: (value: T) => Child;
}) {
  // Few hooks on purpose: an Inbox draws two of these per card. The motion preference is read
  // when it moves, not subscribed to.
  const forced = React.useContext(MotionPreferenceContext);
  const ref = React.useRef<HTMLElement | null>(null);
  const own = React.useRef<{
    shown: boolean;
    exit: Animation | null;
    /** The boxes of the things beside it, read just before it goes. */
    neighbours: { parent: Element; boxes: Map<Element, DOMRect> } | null;
  }>({ shown: show, exit: null, neighbours: null });
  const [state, setState] = React.useState({ present: show, held: value });
  if (show && (!state.present || !Object.is(state.held, value))) setState({ present: true, held: value });

  React.useLayoutEffect(() => {
    const mine = own.current;
    // Once it has gone, the things beside it slide into its room.
    if (!state.present && mine.neighbours) {
      const seen = mine.neighbours;
      mine.neighbours = null;
      for (const [sibling, box] of seen.boxes) {
        if (!sibling.isConnected || sibling.parentElement !== seen.parent) continue;
        const now = sibling.getBoundingClientRect();
        const dx = box.left - now.left;
        const dy = box.top - now.top;
        if (Math.abs(dx) >= 0.5 || Math.abs(dy) >= 0.5) playFlip(sibling, { x: dx, y: dy }, presenceTokens().snappy);
      }
    }
    if (mine.shown === show) return;
    mine.shown = show;
    const element = ref.current;
    const reduced = forced ?? prefersReducedMotion();
    if (show) {
      mine.exit?.cancel();
      mine.exit = null;
      if (!canAnimate(element)) return;
      const tokens = presenceTokens();
      if (kind === 'morph' && !reduced) element.animate([{ transform: `scale(${tokens.morph})` }, { transform: 'scale(1)' }], tokens.snappy);
      element.animate([{ opacity: 0 }, { opacity: 1 }], tokens.enter);
      return;
    }
    if (!canAnimate(element)) {
      setState((current) => ({ ...current, present: false }));
      return;
    }
    const tokens = presenceTokens();
    const morph = kind === 'morph' && !reduced ? tokens.morph : 1;
    const frames = morph !== 1
      ? [{ opacity: 1, transform: 'scale(1)' }, { opacity: 0, transform: `scale(${morph})` }]
      : [{ opacity: 1 }, { opacity: 0 }];
    const animation = element.animate(frames, { ...tokens.exit, fill: 'forwards' });
    mine.exit = animation;
    animation.finished.then(() => {
      if (mine.exit !== animation) return;
      mine.exit = null;
      const parent = element.parentElement;
      if (parent && !reduced) {
        const boxes = new Map<Element, DOMRect>();
        for (const sibling of parent.children) if (sibling !== element) boxes.set(sibling, sibling.getBoundingClientRect());
        mine.neighbours = { parent, boxes };
      }
      setState((current) => ({ ...current, present: false }));
    }, () => {});
  }, [forced, kind, show, state.present]);

  if (!show && !state.present) return null;
  const leaving = !show;
  return React.cloneElement(children(leaving ? (state.held as T) : (value as T)), {
    ref,
    ...(leaving ? { inert: true, 'aria-hidden': true } : {}),
  });
}
