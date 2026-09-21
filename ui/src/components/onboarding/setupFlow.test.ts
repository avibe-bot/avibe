// The contract's own invariants, held before any lane builds against them. These are the
// two decisions a screen cannot make locally without the flow disagreeing with itself:
// which screens run, and where Back goes from each of them.
import { describe, expect, it } from 'vitest';

import {
  INITIAL_SETUP_FLOW_STATE,
  SETUP_SCREENS,
  setupBackTarget,
  setupCapability,
  setupNavigationReady,
  setupScreenSequence,
  type SetupCapability,
} from './setupFlow';

const CAPABILITIES: readonly SetupCapability[] = ['pending', 'enabled', 'disabled'];

describe('setup screen sequence', () => {
  it('keeps the flow a prefix-stable ordering of the declared screens', () => {
    // A property rather than three copied lists: whatever the capability says, the flow
    // opens on the introduction, closes on the assistants, and never reorders what is
    // between them — so a screen added later is covered without editing this test.
    for (const capability of CAPABILITIES) {
      const sequence = setupScreenSequence(capability);
      expect(sequence[0]).toBe('intro');
      expect(sequence[sequence.length - 1]).toBe('assistants');
      const declared = SETUP_SCREENS.filter((screen) => sequence.includes(screen));
      expect([...sequence]).toEqual([...declared]);
    }
  });

  it('drops only the providers screen when the capability is explicitly off', () => {
    expect(setupScreenSequence('disabled')).toEqual(['intro', 'assistants']);
    expect(setupScreenSequence('enabled')).toEqual([...SETUP_SCREENS]);
  });

  it('maps the capability read without collapsing "unread" into "off"', () => {
    expect(setupCapability(null)).toBe('pending');
    expect(setupCapability(true)).toBe('enabled');
    expect(setupCapability(false)).toBe('disabled');
  });
});

describe('setup navigation readiness', () => {
  it('holds the shell on the introduction until the capability read settles', () => {
    // A returned sequence says which screens exist, not that the shell may enter them:
    // entering the providers screen on a guess and removing it when the read resolves to
    // `disabled` recreates the jump the shorter sequence exists to prevent.
    expect(setupNavigationReady('pending')).toBe(false);
    for (const capability of CAPABILITIES.filter((value) => value !== 'pending')) {
      expect(setupNavigationReady(capability)).toBe(true);
    }
  });
});

describe('setup back target', () => {
  it('leaves to the previous screen of the sequence actually running', () => {
    expect(setupBackTarget(setupScreenSequence('enabled'), 'assistants')).toBe('providers');
    expect(setupBackTarget(setupScreenSequence('enabled'), 'providers')).toBe('intro');
    // The degraded flow has no providers screen, so Back from the assistants screen is the
    // introduction rather than a screen this instance never renders.
    expect(setupBackTarget(setupScreenSequence('disabled'), 'assistants')).toBe('intro');
  });

  it('has nowhere to go from the first screen', () => {
    expect(setupBackTarget(setupScreenSequence('enabled'), 'intro')).toBeNull();
    expect(setupBackTarget(setupScreenSequence('disabled'), 'intro')).toBeNull();
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
