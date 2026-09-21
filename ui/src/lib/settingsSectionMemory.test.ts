import { describe, expect, it, vi } from 'vitest';

import { SETTINGS_LANDING_PATH } from './adminNavigation';
import {
  SETTINGS_LAST_SECTION_STORAGE_KEY,
  forgetLastSettingsSection,
  readLastSettingsSection,
  settingsResumePath,
  writeLastSettingsSection,
} from './settingsSectionMemory';

type SectionStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

const storageHolding = (section?: string): SectionStorage => {
  const entries = new Map<string, string>();
  if (section) entries.set(SETTINGS_LAST_SECTION_STORAGE_KEY, section);
  return {
    getItem: (key) => entries.get(key) ?? null,
    setItem: vi.fn((key, value) => { entries.set(key, value); }),
    removeItem: vi.fn((key) => { entries.delete(key); }),
  };
};

const blockedStorage: SectionStorage = {
  getItem: () => { throw new Error('storage blocked'); },
  setItem: () => { throw new Error('storage blocked'); },
  removeItem: () => { throw new Error('storage blocked'); },
};

describe('settings section memory', () => {
  it('reads back the section it recorded', () => {
    const storage = storageHolding();
    writeLastSettingsSection('/settings/backends', storage);
    expect(readLastSettingsSection(storage)).toBe('/settings/backends');
  });

  it('resumes the remembered section', () => {
    expect(settingsResumePath(true, storageHolding('/settings/backends')))
      .toBe('/settings/backends');
  });

  it('lands on General when there is nothing to resume', () => {
    expect(settingsResumePath(true, storageHolding())).toBe(SETTINGS_LANDING_PATH);
  });

  it('degrades to General instead of throwing when storage is blocked', () => {
    expect(() => writeLastSettingsSection('/settings/backends', blockedStorage)).not.toThrow();
    expect(settingsResumePath(true, blockedStorage)).toBe(SETTINGS_LANDING_PATH);
  });

  it.each([
    ['a section this release no longer routes', '/settings/retired-section', true],
    ['a path outside Settings', '/projects', true],
    ["an owner's section an ordinary member cannot open", '/settings/service', false],
    // Route matching strips a query or fragment and the capability check does
    // not, so a hand-edited value could otherwise carry an owner-only section
    // past the guard on the strength of its suffix alone.
    ['an owner-only section wearing a query string', '/settings/service?tab=status', false],
    ['a section wearing a fragment', '/settings/backends#claude', true],
  ])('falls back to General for %s', (_case, remembered, canManageInstance) => {
    expect(settingsResumePath(canManageInstance, storageHolding(remembered)))
      .toBe(SETTINGS_LANDING_PATH);
  });

  it('forgets only the record that names the section the rail dropped', () => {
    const dropped = storageHolding('/settings/memory');
    forgetLastSettingsSection('/settings/memory', dropped);
    expect(readLastSettingsSection(dropped)).toBeNull();

    // A section whose row is merely still loading must not clear a memory of a
    // different one: the two states are indistinguishable for a moment.
    const other = storageHolding('/settings/backends');
    forgetLastSettingsSection('/settings/memory', other);
    expect(readLastSettingsSection(other)).toBe('/settings/backends');
    expect(other.removeItem).not.toHaveBeenCalled();
  });

  it('does not rewrite the section it already holds', () => {
    const storage = storageHolding('/settings/backends');
    writeLastSettingsSection('/settings/backends', storage);
    expect(storage.setItem).not.toHaveBeenCalled();
  });

  it('still resumes an owner-only section for the owner', () => {
    expect(settingsResumePath(true, storageHolding('/settings/service')))
      .toBe('/settings/service');
  });
});
