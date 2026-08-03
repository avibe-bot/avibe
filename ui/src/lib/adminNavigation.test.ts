import { describe, expect, it } from 'vitest';

import { isAdvancedSettingsPath, isMemorySettingsPath } from './adminNavigation';

describe('isAdvancedSettingsPath', () => {
  it('defers to the standalone Memory item when that item is visible', () => {
    expect(isAdvancedSettingsPath('/admin/settings/memory', true)).toBe(false);
    expect(isAdvancedSettingsPath('/admin/settings/memory/', true)).toBe(false);
  });

  it('keeps Memory setup under Advanced Settings when the standalone item is hidden', () => {
    expect(isAdvancedSettingsPath('/admin/settings/memory', false)).toBe(true);
    expect(isAdvancedSettingsPath('/admin/settings/memory/', false)).toBe(true);
  });

  it('keeps the remaining settings pages grouped under Advanced Settings', () => {
    expect(isAdvancedSettingsPath('/admin/settings/messaging', true)).toBe(true);
    expect(isAdvancedSettingsPath('/admin/settings/service', true)).toBe(true);
    expect(isAdvancedSettingsPath('/admin/settings/dependencies', true)).toBe(true);
    expect(isAdvancedSettingsPath('/admin/settings/diagnostics', true)).toBe(true);
  });

  it('leaves other standalone settings destinations inactive', () => {
    expect(isAdvancedSettingsPath('/admin/settings/platforms', true)).toBe(false);
    expect(isAdvancedSettingsPath('/admin/settings/backends', true)).toBe(false);
    expect(isAdvancedSettingsPath('/admin/settings/models', true)).toBe(false);
  });
});

describe('isMemorySettingsPath', () => {
  it('matches the Memory route and nested path boundaries', () => {
    expect(isMemorySettingsPath('/admin/settings/memory')).toBe(true);
    expect(isMemorySettingsPath('/admin/settings/memory/')).toBe(true);
    expect(isMemorySettingsPath('/admin/settings/memory/details')).toBe(true);
  });

  it('does not match a route that only shares the Memory prefix', () => {
    expect(isMemorySettingsPath('/admin/settings/memory-tools')).toBe(false);
  });
});
