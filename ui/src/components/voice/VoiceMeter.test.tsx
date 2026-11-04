/* @vitest-environment jsdom */

import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createVoiceLevel, stillVoiceLevel, voiceLevelHeight } from '@/lib/voiceLevel';
import { VOICE_METER_BARS, VoiceMeter } from './VoiceMeter';

let frames: FrameRequestCallback[] = [];
let reduced = false;
const runFrame = (at: number) => {
  const pending = frames;
  frames = [];
  for (const callback of pending) callback(at);
};
const scales = (container: HTMLElement) => Array.from(container.querySelectorAll<HTMLElement>('[data-voice-meter] > span'))
  .map((bar) => Number(/scaleY\(([\d.]+)\)/.exec(bar.style.transform)?.[1] ?? NaN));

beforeEach(() => {
  frames = [];
  reduced = false;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
  vi.stubGlobal('cancelAnimationFrame', () => {
    frames = [];
  });
  vi.spyOn(performance, 'now').mockReturnValue(0);
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('reduce') && reduced,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('VoiceMeter', () => {
  it('draws the microphone level, newest on the right, and keeps it as it moves left', () => {
    const level = createVoiceLevel();
    const { container } = render(<VoiceMeter level={level} mode="live" />);
    expect(scales(container)).toHaveLength(VOICE_METER_BARS);

    // Quiet: every bar stays low, breathing.
    for (let at = 16; at <= 400; at += 16) runFrame(at);
    expect(Math.max(...scales(container))).toBeLessThan(0.2);

    // A loud syllable lifts the newest bar within a few frames.
    vi.mocked(performance.now).mockReturnValue(400);
    level.set(0.2);
    for (let at = 416; at <= 520; at += 16) runFrame(at);
    const loud = scales(container);
    expect(loud.at(-1)).toBeGreaterThan(0.6);
    expect(loud[0]).toBeLessThan(0.2);

    // Then the meter moves on: the loud column has moved left of the newest.
    vi.mocked(performance.now).mockReturnValue(520);
    level.set(0);
    for (let at = 536; at <= 1_000; at += 16) runFrame(at);
    const later = scales(container);
    const peak = later.indexOf(Math.max(...later));
    expect(peak).toBeLessThan(VOICE_METER_BARS - 3);
    expect(later[peak]).toBeGreaterThan(0.5);
  });

  it('goes flat while a release would cancel, whatever is heard', () => {
    const level = createVoiceLevel();
    const { container, rerender } = render(<VoiceMeter level={level} mode="live" />);
    level.set(0.3);
    for (let at = 16; at <= 200; at += 16) runFrame(at);
    rerender(<VoiceMeter level={level} mode="armed" />);
    for (let at = 216; at <= 800; at += 16) runFrame(at);
    expect(Math.max(...scales(container))).toBeLessThan(0.1);
    expect(container.querySelector('[data-voice-meter="armed"] > span')?.className).toContain('bg-destructive');
  });

  it('sweeps while the take is being finished', () => {
    const { container } = render(<VoiceMeter mode="settling" />);
    for (let at = 16; at <= 500; at += 16) runFrame(at);
    const first = scales(container).indexOf(Math.max(...scales(container)));
    for (let at = 516; at <= 800; at += 16) runFrame(at);
    const second = scales(container).indexOf(Math.max(...scales(container)));
    expect(second).toBeGreaterThan(first);
  });

  it('shows one static volume bar, and no frames, with reduced motion', () => {
    vi.useFakeTimers();
    reduced = true;
    const level = createVoiceLevel();
    level.set(0.05);
    const { container } = render(<VoiceMeter level={level} mode="live" />);
    expect(frames).toHaveLength(0);
    const fill = container.querySelector<HTMLElement>('[data-voice-meter="live"] span span');
    expect(fill?.style.width).toBe(`${Math.round(voiceLevelHeight(0.05) * 100)}%`);
    expect(fill?.className).not.toContain('transition');
  });

  it('draws a still shape without frames', () => {
    const { container } = render(<VoiceMeter level={stillVoiceLevel([0.2, 0.9, 0.5])} mode="live" />);
    expect(scales(container)).toEqual([0.2, 0.9, 0.5]);
    expect(frames).toHaveLength(0);
  });
});
