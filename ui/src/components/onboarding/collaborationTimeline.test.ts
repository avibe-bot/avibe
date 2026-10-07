import { describe, expect, it } from 'vitest';
import {
  COLLABORATION_DURATION, RETURN_WIRE, SETUP_LINEUP, WORK_LINES, collaborationFrame,
} from './collaborationTimeline';

const all = <T,>(value: T) => SETUP_LINEUP.map(() => value);

describe('one collaboration clock', () => {
  it('stands the built-in coordinator before the assistants it hands work to', () => {
    expect(SETUP_LINEUP).toEqual(['vibey', 'claude', 'codex', 'opencode']);
    expect(RETURN_WIRE).toBe(3);
  });
  it('keeps the source active until each pulse arrives', () => {
    for (const [start, end, source, destination, wire] of [
      [1500, 2250, 0, 1, 0], [3750, 4500, 1, 2, 1], [6000, 6750, 2, 3, 2],
    ]) {
      expect(collaborationFrame(start)).toMatchObject({ active: source, handoff: { wire, progress: 0 } });
      expect(collaborationFrame(end - 1).active).toBe(source);
      expect(collaborationFrame(end)).toMatchObject({ active: destination, handoff: null });
      expect(collaborationFrame(end).progress[destination]).toBe(0);
    }
  });
  it('travels one wire at a time and shows no pulse while a card works', () => {
    expect(collaborationFrame(2600).handoff).toBeNull();
    expect(collaborationFrame(1875).handoff).toEqual({ wire: 0, progress: 0.5 });
    expect(collaborationFrame(8750).handoff).toEqual({ wire: RETURN_WIRE, progress: 0.5 });
  });
  it('writes the skeleton line by line before the card reports complete', () => {
    expect(collaborationFrame(2250).written[1]).toBe(0);
    expect(collaborationFrame(2400).written[1]).toBe(2);
    expect(collaborationFrame(2900).written[1]).toBe(WORK_LINES);
    expect(collaborationFrame(2900).done[1]).toBe(false);
    expect(collaborationFrame(3100)).toMatchObject({ active: 1, done: [true, true, false, false] });
  });
  it('keeps finished work on screen while the result returns to the coordinator', () => {
    expect(collaborationFrame(8750)).toMatchObject({ done: all(true), written: all(WORK_LINES), returning: true, summary: false });
    expect(collaborationFrame(10000)).toMatchObject({ active: 0, progress: all(1), written: all(WORK_LINES), summary: true, returning: true });
  });
  it('finishes each card inside its own step and repeats the entire story together', () => {
    expect(collaborationFrame(1500).progress[0]).toBe(1);
    expect(collaborationFrame(3750).progress[1]).toBe(1);
    expect(collaborationFrame(8250).progress).toEqual(all(1));
    expect(COLLABORATION_DURATION).toBe(11150);
    expect(collaborationFrame(11150)).toEqual(collaborationFrame(0));
  });
  it('shows the completed collaboration with no pulse in reduced motion', () => {
    for (const elapsed of [0, 1750, 4000, 8750, 11150]) {
      expect(collaborationFrame(elapsed, true)).toEqual({
        active: 0,
        handoff: null,
        progress: all(1),
        done: all(true),
        written: all(WORK_LINES),
        summary: true,
        returning: true,
      });
    }
  });
});
