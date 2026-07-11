import { describe, expect, it } from 'vitest';

import {
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
});
