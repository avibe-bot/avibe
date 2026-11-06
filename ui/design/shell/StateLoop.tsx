import { Fragment, useEffect, useState, type ReactNode } from 'react';

import { MotionPreferenceContext } from '@/lib/motion';

/**
 * Plays a change the product makes by itself over a shipped page (docs/plans/web-ui-motion.md): the page
 * mounts in the first state and each next state lands at its time, so the product's own motion runs as it
 * does in the app. Every `period` ms it starts over, remounted (a cut). `reduced` draws the moment's
 * reduced-motion form. Like `AfterMount`, the timing lives here so frames stay data; the screenshot, with
 * animations off, shows the state the frame's `wait` lands in, at rest.
 */
export function StateLoop({
  steps,
  period,
  reduced = false,
  children,
}: {
  steps: readonly { at: number; state: string }[];
  period: number;
  reduced?: boolean;
  children: (state: string) => ReactNode;
}) {
  const [round, setRound] = useState(0);
  const [index, setIndex] = useState(0);
  useEffect(() => {
    const timers = steps.slice(1).map((step, offset) => window.setTimeout(() => setIndex(offset + 1), step.at));
    timers.push(window.setTimeout(() => {
      setIndex(0);
      setRound((value) => value + 1);
    }, period));
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [period, round, steps]);
  return (
    <MotionPreferenceContext.Provider value={reduced ? true : null}>
      <Fragment key={round}>{children(steps[index].state)}</Fragment>
    </MotionPreferenceContext.Provider>
  );
}
