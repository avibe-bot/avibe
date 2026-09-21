// The contract's own invariants, held before any lane builds against them. These are the
// two decisions a screen cannot make locally without the flow disagreeing with itself:
// which screens run, and where Back goes from each of them.
import { describe, expect, it } from 'vitest';

import {
  INITIAL_SETUP_FLOW_STATE,
  SETUP_SCREENS,
  setupBackTarget,
  setupScreenSequence,
} from './setupFlow';

describe('setup screen sequence', () => {
  it('keeps the flow a prefix-stable ordering of the declared screens', () => {
    // A property rather than three copied lists: whatever the capability says, the flow
    // opens on the introduction, closes on the assistants, and never reorders what is
    // between them — so a screen added later is covered without editing this test.
    for (const enabled of [true, false, null]) {
      const sequence = setupScreenSequence(enabled);
      expect(sequence[0]).toBe('intro');
      expect(sequence[sequence.length - 1]).toBe('assistants');
      const declared = SETUP_SCREENS.filter((screen) => sequence.includes(screen));
      expect([...sequence]).toEqual([...declared]);
    }
  });

  it('drops only the providers screen when the capability is explicitly off', () => {
    expect(setupScreenSequence(false)).toEqual(['intro', 'assistants']);
    expect(setupScreenSequence(true)).toEqual([...SETUP_SCREENS]);
  });

  it('does not commit to the shorter flow while the capability is unread', () => {
    // An unread capability is not an absent one: resolving to two screens and then
    // discovering the gateway would move the screen the user is already looking at.
    expect(setupScreenSequence(null)).toEqual([...SETUP_SCREENS]);
  });
});

describe('setup back target', () => {
  it('leaves to the previous screen of the sequence actually running', () => {
    expect(setupBackTarget(setupScreenSequence(true), 'assistants')).toBe('providers');
    expect(setupBackTarget(setupScreenSequence(true), 'providers')).toBe('intro');
    // The degraded flow has no providers screen, so Back from the assistants screen is the
    // introduction rather than a screen this instance never renders.
    expect(setupBackTarget(setupScreenSequence(false), 'assistants')).toBe('intro');
  });

  it('has nowhere to go from the first screen', () => {
    expect(setupBackTarget(setupScreenSequence(true), 'intro')).toBeNull();
    expect(setupBackTarget(setupScreenSequence(false), 'intro')).toBeNull();
  });
});

describe('shell-owned flow state', () => {
  it('starts with nothing selected, imported, added or ordered', () => {
    expect(INITIAL_SETUP_FLOW_STATE).toEqual({
      providerSelection: [],
      importedCount: 0,
      addedThroughMore: [],
      routeOrder: [],
    });
  });
});
