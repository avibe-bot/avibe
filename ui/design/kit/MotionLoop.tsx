// The motion demo wrapper (kit/motion.ts writes its keyframes): `display: contents`, so it adds no box.
import type { ReactNode } from 'react';

import { loopRules, type Track } from './motion';

/** A moment as a loop of `period` ms over `children` (see `loopRules`). */
export function MotionLoop({
  children,
  ...loop
}: {
  id: string;
  period: number;
  tracks: Track[];
  reset?: number;
  freeze?: number;
  rest?: number;
  delay?: number;
  children: ReactNode;
}) {
  return (
    <div data-motion-loop={loop.id} className="contents">
      {children}
      <style>{loopRules(loop)}</style>
    </div>
  );
}
