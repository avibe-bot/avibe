import { describe, expect, it } from 'vitest';

import {
  createUnsavedChangesNavigationGate,
  getUnsavedChangesMessage,
  setUnsavedChangesRegistration,
  type UnsavedChangesRegistry,
} from './unsavedChangesRegistry';

describe('unsaved changes registry', () => {
  it('updates a stable registration without creating duplicate entries', () => {
    const registry: UnsavedChangesRegistry = new Map();

    setUnsavedChangesRegistration(registry, 'editor-route', 'first message');
    setUnsavedChangesRegistration(registry, 'editor-route', 'updated message');

    expect(registry.size).toBe(1);
    expect(getUnsavedChangesMessage(registry)).toBe('updated message');
  });

  it('falls back to another dirty surface when the active registration cleans up', () => {
    const registry: UnsavedChangesRegistry = new Map();

    setUnsavedChangesRegistration(registry, 'older-surface', 'older message');
    setUnsavedChangesRegistration(registry, 'newer-surface', 'newer message');
    expect(getUnsavedChangesMessage(registry)).toBe('newer message');

    setUnsavedChangesRegistration(registry, 'newer-surface', null);
    expect(getUnsavedChangesMessage(registry)).toBe('older message');

    setUnsavedChangesRegistration(registry, 'older-surface', null);
    expect(getUnsavedChangesMessage(registry)).toBeNull();
  });

  it('cancels a route action before mutation and leaves navigation blocked', () => {
    const gate = createUnsavedChangesNavigationGate();
    const authorization = gate.authorize('discard?', () => false);

    expect(authorization).toBeNull();
    expect(gate.shouldBlock(true)).toBe(true);
  });

  it('authorizes exactly one synchronous navigation after a confirmed action', () => {
    const gate = createUnsavedChangesNavigationGate();
    const authorization = gate.authorize('discard?', () => true);

    expect(authorization).not.toBeNull();
    authorization?.runNavigation(() => {
      expect(gate.shouldBlock(true)).toBe(false);
    });
    expect(gate.shouldBlock(true)).toBe(true);
  });

  it('does not retain an authorization when its callback never navigates', () => {
    const gate = createUnsavedChangesNavigationGate();
    const authorization = gate.authorize('discard?', () => true);

    authorization?.runNavigation(() => undefined);
    expect(gate.shouldBlock(true)).toBe(true);
  });
});
