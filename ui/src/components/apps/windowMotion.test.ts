import { describe, expect, it } from 'vitest';

import { appWindowMotionClass, type AppWindowExitKind } from './windowMotion';

describe('appWindowMotionClass', () => {
  it('close always wins; skipEntrance is the only way a live window skips the in-keyframe', () => {
    const exitKinds: AppWindowExitKind[] = ['close', null];
    const skipFlags = [false, true];
    for (const exitKind of exitKinds) {
      for (const skipEntrance of skipFlags) {
        const cls = appWindowMotionClass({ exitKind, skipEntrance });
        if (exitKind === 'close') {
          expect(cls).toBe('animate-appwindow-out');
        } else if (skipEntrance) {
          expect(cls).toBeUndefined();
        } else {
          expect(cls).toBe('animate-appwindow-in');
        }
      }
    }
  });
});
