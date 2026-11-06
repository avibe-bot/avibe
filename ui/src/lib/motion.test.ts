// Pure helpers of the motion tokens (docs/plans/web-ui-motion.md §4).
import { describe, expect, it } from 'vitest';

import { timeToProgress } from './motion';

describe('timeToProgress', () => {
  it('finds when an accelerating exit has faded a copy to 30%', () => {
    // `--motion-ease-exit` on `instant`: slow at first, so the copy is still above 30% at 70 ms.
    const at = timeToProgress({ duration: 100, easing: 'cubic-bezier(0.4, 0, 1, 1)' }, 0.7);
    expect(at).toBeGreaterThan(78);
    expect(at).toBeLessThan(82);
  });

  it('is early on a decelerating curve, and proportional on any other easing', () => {
    expect(timeToProgress({ duration: 200, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' }, 0.7)).toBeLessThan(50);
    expect(timeToProgress({ duration: 200, easing: 'linear' }, 0.7)).toBe(140);
    expect(timeToProgress({ duration: 200, easing: 'ease-out' }, 0.5)).toBe(100);
  });
});
