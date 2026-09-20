import { describe, expect, it } from 'vitest';
import { COLLABORATION_DURATION, WORK_LINES, collaborationFrame } from './collaborationTimeline';

describe('one collaboration clock', () => {
  it('keeps the source active until each pulse arrives', () => {
    for (const [start, end, source, destination, wire] of [[1500, 2250, 0, 1, 0], [3750, 4500, 1, 2, 1]]) {
      expect(collaborationFrame(start)).toMatchObject({ active: source, handoff: { wire, progress: 0 } });
      expect(collaborationFrame(end - 1).active).toBe(source);
      expect(collaborationFrame(end)).toMatchObject({ active: destination, handoff: null });
      expect(collaborationFrame(end).progress[destination]).toBe(0);
    }
  });
  it('travels one wire at a time and shows no pulse while a card works', () => {
    expect(collaborationFrame(2600).handoff).toBeNull();
    expect(collaborationFrame(1875).handoff).toEqual({ wire: 0, progress: 0.5 });
    expect(collaborationFrame(6500).handoff).toEqual({ wire: 2, progress: 0.5 });
  });
  it('writes the skeleton line by line before the card reports complete', () => {
    expect(collaborationFrame(2250).written[1]).toBe(0);
    expect(collaborationFrame(2400).written[1]).toBe(2);
    expect(collaborationFrame(2900).written[1]).toBe(WORK_LINES);
    expect(collaborationFrame(2900).done[1]).toBe(false);
    expect(collaborationFrame(3100)).toMatchObject({ active: 1, done: [true, true, false] });
  });
  it('keeps finished work on screen while the result returns to the PM', () => {
    expect(collaborationFrame(6500)).toMatchObject({ done: [true, true, true], written: [WORK_LINES, WORK_LINES, WORK_LINES], returning: true, summary: false });
    expect(collaborationFrame(8500)).toMatchObject({ active: 0, progress: [1, 1, 1], written: [WORK_LINES, WORK_LINES, WORK_LINES], summary: true, returning: true });
  });
  it('finishes each card inside its own step and repeats the entire story together', () => {
    expect(collaborationFrame(1500).progress[0]).toBe(1);
    expect(collaborationFrame(3750).progress[1]).toBe(1);
    expect(collaborationFrame(6000).progress).toEqual([1, 1, 1]);
    expect(COLLABORATION_DURATION).toBe(8900);
    expect(collaborationFrame(8900)).toEqual(collaborationFrame(0));
  });
  it('shows the completed collaboration with no pulse in reduced motion', () => {
    for (const elapsed of [0, 1750, 4000, 6500, 8900]) {
      expect(collaborationFrame(elapsed, true)).toEqual({
        active: 0,
        handoff: null,
        progress: [1, 1, 1],
        done: [true, true, true],
        written: [WORK_LINES, WORK_LINES, WORK_LINES],
        summary: true,
        returning: true,
      });
    }
  });
});
