import { describe, expect, it } from 'vitest';

import { isAdvancedSettingsPath } from './adminNavigation';

describe('isAdvancedSettingsPath', () => {
  it('does not activate Advanced Settings on the standalone Memory page', () => {
    expect(isAdvancedSettingsPath('/admin/settings/memory')).toBe(false);
  });

  it('keeps the remaining settings pages grouped under Advanced Settings', () => {
    expect(isAdvancedSettingsPath('/admin/settings/messaging')).toBe(true);
    expect(isAdvancedSettingsPath('/admin/settings/service')).toBe(true);
    expect(isAdvancedSettingsPath('/admin/settings/dependencies')).toBe(true);
    expect(isAdvancedSettingsPath('/admin/settings/diagnostics')).toBe(true);
  });

  it('leaves other standalone settings destinations inactive', () => {
    expect(isAdvancedSettingsPath('/admin/settings/platforms')).toBe(false);
    expect(isAdvancedSettingsPath('/admin/settings/backends')).toBe(false);
    expect(isAdvancedSettingsPath('/admin/settings/models')).toBe(false);
  });
});
