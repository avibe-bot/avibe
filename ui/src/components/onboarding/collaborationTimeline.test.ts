import { describe, expect, it } from 'vitest';
import { collaborationFrame } from './collaborationTimeline';

describe('one collaboration clock', () => {
  it('keeps the source active until each pulse arrives', () => {
    for (const [start, end, source, destination, path] of [[1500, 2250, 0, 1, 0], [3750, 4500, 1, 2, 1], [6000, 7000, 2, 0, 2]]) {
      expect(collaborationFrame(start)).toMatchObject({ active: source, handoff: { path, progress: 0 } });
      expect(collaborationFrame(end - 1).active).toBe(source);
      expect(collaborationFrame(end)).toMatchObject({ active: destination, handoff: null });
      expect(collaborationFrame(end).progress[destination]).toBe(0);
    }
  });
  it('finishes each card in 1.5 seconds and repeats the entire story together', () => {
    expect(collaborationFrame(1500).progress[0]).toBe(1);
    expect(collaborationFrame(3750).progress[1]).toBe(1);
    expect(collaborationFrame(6000).progress).toEqual([1, 1, 1]);
    expect(collaborationFrame(8500)).toMatchObject({ progress: [1, 1, 1], summary: true });
    expect(collaborationFrame(8900)).toEqual(collaborationFrame(0));
  });
  it('shows a completed collaboration with no pulse or active card in reduced motion', () => {
    for (const elapsed of [0, 1750, 4000, 6500, 8900]) {
      expect(collaborationFrame(elapsed, true)).toEqual({ active: null, handoff: null, progress: [1, 1, 1], summary: true });
    }
  });
});
