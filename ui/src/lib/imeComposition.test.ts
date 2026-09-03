import { describe, expect, it } from 'vitest';

import { isComposingKey, type ComposingKeyEvent } from './imeComposition';

/**
 * One entry per channel a browser announces composition through, each carrying
 * only that signal. Seeded rather than enumerated as cases: a channel the guard
 * learns to read is added here once and every property below covers it in every
 * combination, so no combination can be the one nobody thought of.
 */
const SIGNALS: Record<string, ComposingKeyEvent> = {
  reactSynthetic: { nativeEvent: { isComposing: true } },
  nativeEvent: { isComposing: true },
  legacyKeyCode: { keyCode: 229 },
};

/** Every subset of the signals, the empty one first. */
const subsets = (): { names: string[]; event: ComposingKeyEvent }[] => {
  const names = Object.keys(SIGNALS);
  return Array.from({ length: 2 ** names.length }, (_, mask) => {
    const chosen = names.filter((_name, index) => (mask & (1 << index)) !== 0);
    return {
      names: chosen,
      event: chosen.reduce<ComposingKeyEvent>(
        (event, name) => ({ ...event, ...SIGNALS[name] }),
        {},
      ),
    };
  });
};

describe('a keystroke that belongs to the input method', () => {
  it('is recognised through any signal a browser announces it with', () => {
    const announced = subsets().filter(({ names }) => names.length > 0);
    expect(announced.length).toBeGreaterThan(0);
    expect(announced.filter(({ event }) => !isComposingKey(event))).toEqual([]);
  });

  it('is the only kind recognised: one announcing nothing belongs to the person typing', () => {
    const [silent] = subsets();
    expect(silent.names).toEqual([]);
    expect(isComposingKey(silent.event)).toBe(false);
  });

  it('is not read into an event that answers every signal in the negative', () => {
    expect(isComposingKey({
      nativeEvent: { isComposing: false },
      isComposing: false,
      keyCode: 13,
    })).toBe(false);
  });
});
