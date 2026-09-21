import { describe, expect, it } from 'vitest';

import { SETTINGS_LANDING_PATH } from './adminNavigation';
import {
  SETTINGS_LAST_SECTION_STORAGE_KEY,
  readLastSettingsSection,
  settingsResumePath,
  writeLastSettingsSection,
} from './settingsSectionMemory';

const storageHolding = (section?: string): Pick<Storage, 'getItem' | 'setItem'> => {
  const entries = new Map<string, string>();
  if (section) entries.set(SETTINGS_LAST_SECTION_STORAGE_KEY, section);
  return {
    getItem: (key) => entries.get(key) ?? null,
    setItem: (key, value) => { entries.set(key, value); },
  };
};

const blockedStorage: Pick<Storage, 'getItem' | 'setItem'> = {
  getItem: () => { throw new Error('storage blocked'); },
  setItem: () => { throw new Error('storage blocked'); },
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
  ])('falls back to General for %s', (_case, remembered, canManageInstance) => {
    expect(settingsResumePath(canManageInstance, storageHolding(remembered)))
      .toBe(SETTINGS_LANDING_PATH);
  });

  it('still resumes an owner-only section for the owner', () => {
    expect(settingsResumePath(true, storageHolding('/settings/service')))
      .toBe('/settings/service');
  });
});
